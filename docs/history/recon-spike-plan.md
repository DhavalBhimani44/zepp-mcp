# Zepp Recon Spike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture real responses from the maintainer's Zepp account so every subsequent design decision and decoder is written against observed bytes rather than third-party reverse-engineering.

**Architecture:** A single-purpose, throwaway probe script run once from the scratchpad. It authenticates via the AES password flow, discovers the account's region binding by trial, then sweeps every endpoint and event family in the spec, writing each raw response to disk through a redaction layer. Output is a JSON capture corpus plus a findings report answering the twelve open questions in spec §11.

**Tech Stack:** Python 3.13, `uv` with PEP 723 inline script dependencies, `httpx`, `pycryptodome`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-08-15-zepp-mcp-design.md` (§4 defines the probes; §11 lists the questions this answers)

## Global Constraints

- **Python 3.11+.** Local interpreter is 3.13.13.
- **Location.** Spike code lives in the repo at `spike/`, committed on `main`. No worktree, no branch — the project is being built from scratch and `main` is the trunk.
- **Raw captures are git-ignored.** `spike/out/` and `spike/FINDINGS.md` are excluded by `.gitignore`. Nothing captured from the live API becomes trackable until the redaction verification in Task 7 step 4 passes and the promotion step copies verified files into `tests/fixtures/zepp/`. This is the gate that keeps a credential out of git history; do not remove those ignore rules.
- **Credentials.** `ZEPP_EMAIL` and `ZEPP_PASSWORD` come from `spike/.env` or from the real environment, which wins when both are set. `.env` is git-ignored and mode `0600`; `.env.example` is the tracked template holding placeholders only. Credentials are never echoed to stdout, never embedded in source, and never written into a capture — `load_dotenv` returns key names with the literal value `"set"`, never the secrets.
- **Every write passes through the redactor.** No exceptions. The corpus becomes committed test fixtures in Plan 2, so an unredacted token is a credential leak into a repository intended for open-sourcing.
- **Rate limiting.** `time.sleep(1.5)` between every network call. No retries against the auth endpoint under any circumstance — quotas are unknown and spec §4 mandates conservatism.
- **Non-goals.** No decoding, no storage schema, no MCP server. The spike records what happened; it does not interpret it.
- **Dependencies are declared inline** via PEP 723 so nothing installs globally. Run everything with `uv run`.

---

### Task 1: Redactor

The safety-critical component. Everything else writes through it.

Two mechanisms, because either alone is insufficient. **Substring replacement** catches a token wherever it appears — including inside a URL, and including inside a double-encoded JSON string, which spec §5 warns is common in Zepp event payloads and which a naive recursive walk over parsed JSON would miss entirely. **Key-name replacement** catches secrets in fields never registered.

Substring replacement carries a minimum-length guard. Without it, a numeric `user_id` of `1234567` would be substring-replaced everywhere it appeared, corrupting step counts and timestamps that happen to contain those digits. Short values are handled by key name only.

**Files:**
- Create: `spike/redact.py`
- Test: `spike/test_redact.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Redactor` class with `register(value: str | None, label: str) -> None` and `scrub(obj: Any) -> Any`; module constant `SENSITIVE_KEYS: frozenset[str]`; module constant `MIN_SUBSTRING_LEN: int = 8`

- [ ] **Step 1: Create the spike directory and write the failing test**

```bash
mkdir -p spike
```

Write `spike/test_redact.py`:

```python
import json

from redact import Redactor


def test_registered_value_redacted_in_plain_field():
    r = Redactor()
    r.register("abcdef0123456789", "app_token")
    assert r.scrub({"tok": "abcdef0123456789"}) == {"tok": "<app_token>"}


def test_registered_value_redacted_inside_url():
    r = Redactor()
    r.register("abcdef0123456789", "app_token")
    out = r.scrub({"url": "https://x.zepp.com/a?apptoken=abcdef0123456789&b=1"})
    assert out == {"url": "https://x.zepp.com/a?apptoken=<app_token>&b=1"}


def test_registered_value_redacted_inside_double_encoded_json():
    """Zepp nests JSON-encoded strings inside JSON (spec section 5)."""
    r = Redactor()
    r.register("abcdef0123456789", "app_token")
    inner = json.dumps({"session": "abcdef0123456789"})
    out = r.scrub({"data": inner})
    assert "abcdef0123456789" not in out["data"]
    assert "<app_token>" in out["data"]


def test_sensitive_key_redacted_even_when_value_short():
    r = Redactor()
    assert r.scrub({"user_id": "1234567"}) == {"user_id": "<user_id>"}


def test_sensitive_key_redacted_when_value_is_an_integer():
    """Zepp returns user_id as a JSON integer, not a string. An int that
    skips key-name redaction reaches disk verbatim, because scrub() passes
    non-str/dict/list values straight through."""
    r = Redactor()
    assert r.scrub({"user_id": 12345678}) == {"user_id": "<user_id>"}


def test_zepp_status_envelope_survives_redaction():
    """{"code": 1, "data": [...]} is Zepp's standard envelope. Treating
    "code" as sensitive would blank the status field in every capture."""
    r = Redactor()
    assert r.scrub({"code": 1, "data": [{"a": 2}]}) == {"code": 1, "data": [{"a": 2}]}


def test_short_registered_value_is_not_substring_replaced():
    """Guards against corrupting unrelated numeric data."""
    r = Redactor()
    r.register("1234567", "user_id")
    assert r.scrub({"steps": "1234567 steps"}) == {"steps": "1234567 steps"}


def test_scrubs_through_nested_lists_and_dicts():
    r = Redactor()
    r.register("abcdef0123456789", "app_token")
    out = r.scrub({"items": [{"a": ["abcdef0123456789"]}]})
    assert out == {"items": [{"a": ["<app_token>"]}]}


def test_longer_secret_replaced_before_shorter_overlapping_one():
    r = Redactor()
    r.register("abcdef0123", "short_tok")
    r.register("abcdef0123456789", "long_tok")
    assert r.scrub({"t": "abcdef0123456789"}) == {"t": "<long_tok>"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd spike && uv run --with pytest pytest test_redact.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'redact'`

- [ ] **Step 3: Write the implementation**

Write `spike/redact.py`:

```python
"""Strip credentials from captured responses before they touch disk."""

from __future__ import annotations

from typing import Any

# NOTE: "code" is deliberately absent. Zepp's standard response envelope is
# {"code": 1, "data": [...]} where code is an HTTP-ish status, not a secret —
# redacting it would destroy the status field in every single capture. The
# OAuth access code never reaches a capture under a bare "code" key; it is
# registered as a substring in auth.py before its response is recorded.
SENSITIVE_KEYS = frozenset({
    "app_token", "apptoken", "login_token", "access", "refresh",
    "token", "password", "emailorphone", "email",
    "user_id", "userid", "uid", "device_id",
})

MIN_SUBSTRING_LEN = 8


class Redactor:
    def __init__(self) -> None:
        self._substrings: list[tuple[str, str]] = []

    def register(self, value: str | None, label: str) -> None:
        """Register a secret for substring replacement everywhere it appears.

        Values shorter than MIN_SUBSTRING_LEN are ignored to avoid corrupting
        unrelated data that happens to contain the same characters. Those are
        still caught by key name if they appear under a SENSITIVE_KEYS field.
        """
        if not value or len(str(value)) < MIN_SUBSTRING_LEN:
            return
        self._substrings.append((str(value), f"<{label}>"))
        # Longest first, so an overlapping longer secret wins.
        self._substrings.sort(key=lambda pair: len(pair[0]), reverse=True)

    def scrub(self, obj: Any) -> Any:
        if isinstance(obj, str):
            for actual, placeholder in self._substrings:
                obj = obj.replace(actual, placeholder)
            return obj
        if isinstance(obj, dict):
            out: dict[Any, Any] = {}
            for key, value in obj.items():
                if str(key).lower() in SENSITIVE_KEYS and isinstance(value, (str, int)):
                    out[key] = f"<{key}>"
                else:
                    out[key] = self.scrub(value)
            return out
        if isinstance(obj, list):
            return [self.scrub(item) for item in obj]
        return obj
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd spike && uv run --with pytest pytest test_redact.py -v`
Expected: PASS, 9 passed

- [ ] **Step 5: Commit**

The spike is not in the project repo. Initialise a throwaway repo inside the scratchpad so each task is revertible:

```bash
cd spike && git init -q 2>/dev/null; git add redact.py test_redact.py && git commit -q -m "spike: redactor with substring and key-name mechanisms"
```

---

### Task 2: Capture harness

Writes one JSON file per request and maintains an index. Every response body is stored twice — parsed when it is valid JSON, and always as raw text — because spec §5 warns that bodies may be double-encoded or not JSON at all, and the whole point of the corpus is that nothing observed is lost.

**Files:**
- Create: `spike/capture.py`
- Test: `spike/test_capture.py`

**Interfaces:**
- Consumes: `Redactor` from Task 1
- Produces: `Capture` class with `__init__(out_dir: Path, redactor: Redactor)`, `record(name: str, method: str, url: str, params: dict | None, status: int, body_text: str, elapsed_ms: int) -> dict`, and `write_index() -> Path`. `record` returns the capture dict it wrote.

- [ ] **Step 1: Write the failing test**

Write `spike/test_capture.py`:

```python
import json

from capture import Capture
from redact import Redactor


def test_record_writes_redacted_file_and_returns_summary(tmp_path):
    r = Redactor()
    r.register("abcdef0123456789", "app_token")
    cap = Capture(tmp_path, r)

    result = cap.record(
        name="band_data",
        method="GET",
        url="https://api-mifit.zepp.com/v1/data/band_data.json",
        params={"apptoken": "abcdef0123456789", "from_date": "2026-08-01"},
        status=200,
        body_text='{"code":1,"data":[]}',
        elapsed_ms=412,
    )

    assert result["status"] == 200
    assert result["name"] == "band_data"
    written = sorted(tmp_path.glob("*.json"))
    assert len(written) == 1
    on_disk = json.loads(written[0].read_text())
    assert "abcdef0123456789" not in written[0].read_text()
    assert on_disk["params"]["apptoken"] == "<apptoken>"
    assert on_disk["body_parsed"] == {"code": 1, "data": []}


def test_record_keeps_raw_text_when_body_is_not_json(tmp_path):
    cap = Capture(tmp_path, Redactor())
    cap.record(
        name="weird",
        method="GET",
        url="https://x.zepp.com/y",
        params=None,
        status=403,
        body_text="<html>denied</html>",
        elapsed_ms=10,
    )
    on_disk = json.loads(sorted(tmp_path.glob("*.json"))[0].read_text())
    assert on_disk["body_parsed"] is None
    assert on_disk["body_text"] == "<html>denied</html>"


def test_flags_empty_200_as_suspicious(tmp_path):
    """The failure mode spec section 6 is built to defend against."""
    cap = Capture(tmp_path, Redactor())
    result = cap.record(
        name="empty", method="GET", url="https://x.zepp.com/y",
        params=None, status=200, body_text='{"code":1,"data":[]}', elapsed_ms=5,
    )
    assert result["empty_200"] is True


def test_non_empty_200_not_flagged(tmp_path):
    cap = Capture(tmp_path, Redactor())
    result = cap.record(
        name="ok", method="GET", url="https://x.zepp.com/y",
        params=None, status=200, body_text='{"data":[{"a":1}]}', elapsed_ms=5,
    )
    assert result["empty_200"] is False


def test_index_lists_every_capture_in_order(tmp_path):
    cap = Capture(tmp_path, Redactor())
    for n in ("one", "two"):
        cap.record(name=n, method="GET", url="https://x/y", params=None,
                   status=200, body_text='{"data":[1]}', elapsed_ms=1)
    index_path = cap.write_index()
    index = json.loads(index_path.read_text())
    assert [entry["name"] for entry in index] == ["one", "two"]


def test_filenames_are_ordered_and_unique(tmp_path):
    cap = Capture(tmp_path, Redactor())
    for _ in range(3):
        cap.record(name="same", method="GET", url="https://x/y", params=None,
                   status=200, body_text='{"data":[1]}', elapsed_ms=1)
    names = sorted(p.name for p in tmp_path.glob("*same.json"))
    assert names == ["000_same.json", "001_same.json", "002_same.json"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd spike && uv run --with pytest pytest test_capture.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'capture'`

- [ ] **Step 3: Write the implementation**

Write `spike/capture.py`:

```python
"""Write every probe response to disk, redacted, with an index."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from redact import Redactor


def _looks_empty(parsed: Any) -> bool:
    """An empty-200: HTTP success carrying no actual records."""
    if parsed is None:
        return False
    if isinstance(parsed, dict):
        for key in ("data", "items", "value", "result"):
            if key in parsed:
                return not parsed[key]
        return not parsed
    return not parsed


class Capture:
    def __init__(self, out_dir: Path, redactor: Redactor) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self.entries: list[dict[str, Any]] = []

    def record(
        self,
        name: str,
        method: str,
        url: str,
        params: dict | None,
        status: int,
        body_text: str,
        elapsed_ms: int,
    ) -> dict[str, Any]:
        try:
            parsed = json.loads(body_text)
        except (ValueError, TypeError):
            parsed = None

        empty_200 = status == 200 and _looks_empty(parsed)

        record = {
            "seq": len(self.entries),
            "name": name,
            "method": method,
            "url": self.redactor.scrub(url),
            "params": self.redactor.scrub(params),
            "status": status,
            "elapsed_ms": elapsed_ms,
            "body_bytes": len(body_text),
            "empty_200": empty_200,
            "body_parsed": self.redactor.scrub(parsed),
            "body_text": self.redactor.scrub(body_text),
        }

        path = self.out_dir / f"{len(self.entries):03d}_{name}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        self.entries.append(record)
        return record

    def write_index(self) -> Path:
        index = [
            {k: entry[k] for k in
             ("seq", "name", "url", "status", "body_bytes", "empty_200", "elapsed_ms")}
            for entry in self.entries
        ]
        path = self.out_dir / "index.json"
        path.write_text(json.dumps(index, indent=2))
        return path
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd spike && uv run --with pytest pytest test_capture.py test_redact.py -v`
Expected: PASS, 15 passed

- [ ] **Step 5: Commit**

```bash
cd spike && git add capture.py test_capture.py && git commit -q -m "spike: capture harness with empty-200 flagging"
```

---

### Task 3: Login and region discovery

Answers spec §11 questions 1 and 3. The reference implementation hardcodes `api-user-us2.zepp.com`, `region=us-west-2` and `country_code=US`; this task instead *discovers* the working combination by trial and records every attempt, including the failures — the failures are data.

The full `token_info` object is retained rather than the two fields the reference extracts, because it likely carries the region routing that makes `Credential.region_host` populatable in Plan 2.

**Files:**
- Create: `spike/auth.py`
- Test: `spike/test_auth.py`

**Interfaces:**
- Consumes: `Capture` from Task 2, `Redactor` from Task 1
- Produces: `build_login_payload(email: str, password: str, region: str, country: str) -> bytes` (AES-encrypted body); `LOGIN_VARIANTS: list[tuple[str, str, str]]`; `login(client, cap, redactor, email, password) -> dict` returning the full `token_info` plus a `_variant` key naming the combination that worked

- [ ] **Step 1: Write the failing test**

Only the payload construction is unit-testable — the discovery loop is network I/O and is verified by running it in Task 4. Write `spike/test_auth.py`:

```python
import urllib.parse

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from auth import _AES_IV, _AES_KEY, LOGIN_VARIANTS, build_login_payload


def test_payload_round_trips_to_expected_fields():
    blob = build_login_payload("a@b.com", "hunter2", "us-west-2", "US")
    clear = unpad(
        AES.new(_AES_KEY, AES.MODE_CBC, iv=_AES_IV).decrypt(blob),
        AES.block_size,
    ).decode()
    fields = urllib.parse.parse_qs(clear)
    assert fields["emailOrPhone"] == ["a@b.com"]
    assert fields["password"] == ["hunter2"]
    assert fields["region"] == ["us-west-2"]
    assert fields["country_code"] == ["US"]
    assert fields["client_id"] == ["HuaMi"]
    assert fields["state"] == ["REDIRECTION"]


def test_payload_requests_both_token_types():
    blob = build_login_payload("a@b.com", "hunter2", "us-west-2", "US")
    clear = unpad(
        AES.new(_AES_KEY, AES.MODE_CBC, iv=_AES_IV).decrypt(blob),
        AES.block_size,
    ).decode()
    assert urllib.parse.parse_qs(clear)["token"] == ["access", "refresh"]


def test_payload_is_block_aligned():
    blob = build_login_payload("a@b.com", "hunter2", "us-west-2", "US")
    assert len(blob) % AES.block_size == 0


def test_variants_cover_multiple_regions():
    hosts = {host for host, _, _ in LOGIN_VARIANTS}
    assert len(hosts) > 1, "region discovery needs more than one candidate host"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd spike && uv run --with pytest --with pycryptodome pytest test_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'auth'`

- [ ] **Step 3: Write the implementation**

Write `spike/auth.py`:

```python
"""AES password login with region discovery by trial."""

from __future__ import annotations

import time
import urllib.parse
import uuid
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

_AES_KEY = b"xeNtBVqzDc6tuNTh"
_AES_IV = b"MAAAYAAAAAAAAABg"

_REDIRECT = "https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html"

# (auth_host, region, country_code). Tried in order until one returns 303.
LOGIN_VARIANTS: list[tuple[str, str, str]] = [
    ("api-user.zepp.com", "us-west-2", "US"),
    ("api-user-us2.zepp.com", "us-west-2", "US"),
    ("api-user.zepp.com", "eu-central-1", "IN"),
    ("api-user.zepp.com", "us-west-2", "IN"),
    ("api-user-de2.zepp.com", "eu-central-1", "DE"),
]

_STEP1_HEADERS = {
    "app_name": "com.huami.midong", "appname": "com.huami.midong",
    "cv": "151689_9.12.5", "v": "2.0", "appplatform": "android_phone",
    "vb": "202509151347", "vn": "9.12.5", "x-hm-ekv": "1",
    "user-agent": "Zepp/9.12.5 (Pixel 4; Android 12; Density/2.75)",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
}

_STEP2_HEADERS = {
    "app_name": "com.huami.webapp", "appname": "com.huami.webapp",
    "origin": "https://user.zepp.com", "referer": "https://user.zepp.com/",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) "
                  "Gecko/20100101 Firefox/133.0",
}


def build_login_payload(email: str, password: str, region: str, country: str) -> bytes:
    body = urllib.parse.urlencode({
        "emailOrPhone": email, "password": password,
        "state": "REDIRECTION", "client_id": "HuaMi",
        "redirect_uri": _REDIRECT,
        "region": region, "token": ["access", "refresh"],
        "country_code": country,
    }, doseq=True).encode()
    cipher = AES.new(_AES_KEY, AES.MODE_CBC, iv=_AES_IV)
    return cipher.encrypt(pad(body, AES.block_size))


def login(client, cap, redactor, email: str, password: str) -> dict[str, Any]:
    """Try each variant until one yields an app_token. Records every attempt.

    Never retries a variant. Auth endpoints are not hammered (spec section 4).
    """
    access_code = None
    used: tuple[str, str, str] | None = None

    for host, region, country in LOGIN_VARIANTS:
        url = f"https://{host}/v2/registrations/tokens"
        started = time.monotonic()
        response = client.post(
            url,
            content=build_login_payload(email, password, region, country),
            headers=_STEP1_HEADERS, follow_redirects=False, timeout=20,
        )
        elapsed = int((time.monotonic() - started) * 1000)
        location = response.headers.get("location", "")

        # Register the access code BEFORE capturing. The redirect URL carries
        # it as a query parameter, so recording first would write a live
        # credential to disk in plaintext — and this corpus is promoted to
        # committed fixtures in Task 7.
        candidate = None
        if location:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
            candidate = (query.get("access") or [None])[0]
            refresh = (query.get("refresh") or [None])[0]
            if candidate:
                redactor.register(candidate, "access_code")
            if refresh:
                redactor.register(refresh, "refresh_code")

        cap.record(
            name=f"login_step1_{host}_{region}_{country}",
            method="POST", url=url,
            params={"region": region, "country_code": country,
                    "location": location},
            status=response.status_code,
            body_text=response.text, elapsed_ms=elapsed,
        )
        time.sleep(1.5)

        if response.status_code == 303 and candidate:
            access_code, used = candidate, (host, region, country)
            break

    if not access_code or used is None:
        raise RuntimeError(
            "No login variant returned an access code. Inspect the "
            "login_step1_* captures; step 1 is where region binding fails."
        )

    _, region, country = used

    url = "https://api-mifit.zepp.com/v2/client/login"
    started = time.monotonic()
    response = client.post(url, timeout=20, headers=_STEP2_HEADERS, data={
        "code": access_code, "device_id": str(uuid.uuid4()),
        "grant_type": "access_token", "third_name": "huami",
        "app_name": "com.huami.midong", "country_code": country,
        "device_model": "android_phone", "app_version": "9.12.5",
        "allow_registration": "false", "lang": "en",
        "dn": "api-mifit.zepp.com,api-user.zepp.com,"
              "api-watch.zepp.com,auth.zepp.com",
        "source": "com.huami.watch.hmwatchmanager:9.12.5:151689",
    })
    elapsed = int((time.monotonic() - started) * 1000)

    payload = {}
    try:
        payload = response.json()
    except ValueError:
        pass
    token_info = payload.get("token_info", {}) if isinstance(payload, dict) else {}

    # Register secrets BEFORE capturing, so this response is written redacted.
    redactor.register(token_info.get("app_token"), "app_token")
    redactor.register(token_info.get("login_token"), "login_token")
    redactor.register(str(token_info.get("user_id", "")), "user_id")

    cap.record(
        name="login_step2", method="POST", url=url,
        params={"country_code": country, "region": region},
        status=response.status_code, body_text=response.text, elapsed_ms=elapsed,
    )
    time.sleep(1.5)

    if not token_info.get("app_token"):
        raise RuntimeError(
            f"Step 2 returned no app_token (HTTP {response.status_code}). "
            "See the login_step2 capture."
        )

    result = dict(token_info)
    result["_variant"] = {"host": used[0], "region": region, "country": country}
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd spike && uv run --with pytest --with pycryptodome pytest -v`
Expected: PASS, 19 passed

- [ ] **Step 5: Commit**

```bash
cd spike && git add auth.py test_auth.py && git commit -q -m "spike: AES login with region discovery by trial"
```

---

### Task 4: Probe runner and core data sweep

The first task that touches the live API. Answers spec §11 questions 1, 2, 3, 5 and 11.

Three probes here. **Region confirmation** issues the same `band_data.json` request against every candidate data host, so the empty-200 failure mode is observed and captured rather than theorised. **Retention** walks backwards by year to find the earliest date returning data, settling the §2 correction empirically. **`query_type`** captures `summary` and `detail` side by side so Plan 2's decoder knows which one carries `data_hr`.

**Files:**
- Create: `spike/probe.py`
- Modify: none

**Interfaces:**
- Consumes: `login` from Task 3, `Capture` from Task 2, `Redactor` from Task 1
- Produces: `DATA_HOSTS: list[str]`; `Session` dataclass with fields `client`, `cap`, `redactor`, `token`, `uid`, `host`; `get(session, name, path, params, host=None) -> dict`; `probe_region(session) -> str`; `probe_retention(session) -> str | None`; `probe_query_type(session) -> None`; `main() -> None`

- [ ] **Step 1: Write the probe runner**

Write `spike/probe.py`:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "pycryptodome"]
# ///
"""Zepp recon spike. Run once. Throwaway.

    export ZEPP_EMAIL=you@example.com
    export ZEPP_PASSWORD=...
    uv run probe.py

Writes redacted captures to ./out/ and a summary to ./FINDINGS.md.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from auth import login
from capture import Capture
from redact import Redactor

OUT = Path(__file__).parent / "out"
PAUSE = 1.5

DATA_HOSTS = [
    "api-mifit.zepp.com",
    "api-mifit-us2.zepp.com",
    "api-mifit-us3.zepp.com",
    "api-mifit-de2.zepp.com",
    "api-mifit-cn.zepp.com",
]


@dataclass
class Session:
    client: httpx.Client
    cap: Capture
    redactor: Redactor
    token: str
    uid: str
    host: str = "api-mifit.zepp.com"
    notes: dict[str, Any] = field(default_factory=dict)


def get(session: Session, name: str, path: str,
        params: dict | None = None, host: str | None = None) -> dict:
    url = f"https://{host or session.host}{path}"
    started = time.monotonic()
    try:
        response = session.client.get(
            url, headers={"apptoken": session.token},
            params=params or {}, timeout=25,
        )
        status, text = response.status_code, response.text
    except httpx.HTTPError as exc:
        status, text = 0, f"TRANSPORT ERROR: {exc}"
    elapsed = int((time.monotonic() - started) * 1000)
    record = session.cap.record(
        name=name, method="GET", url=url, params=params,
        status=status, body_text=text, elapsed_ms=elapsed,
    )
    time.sleep(PAUSE)
    return record


def probe_region(session: Session) -> str:
    """Same request against every host. Spec question 1."""
    today = dt.date.today()
    week_ago = today - dt.timedelta(days=7)
    winner = session.host
    for host in DATA_HOSTS:
        record = get(
            session, f"region_{host}", "/v1/data/band_data.json",
            {"query_type": "detail", "device_type": "android_phone",
             "userid": session.uid,
             "from_date": week_ago.isoformat(), "to_date": today.isoformat()},
            host=host,
        )
        if record["status"] == 200 and not record["empty_200"]:
            winner = host
            break
    session.notes["region_host"] = winner
    return winner


def probe_retention(session: Session) -> str | None:
    """Walk back by year to find the earliest date with data. Spec question 2."""
    today = dt.date.today()
    earliest = None
    for years_back in range(1, 11):
        start = today.replace(year=today.year - years_back)
        end = start + dt.timedelta(days=30)
        record = get(
            session, f"retention_{start.year}", "/v1/data/band_data.json",
            {"query_type": "detail", "device_type": "android_phone",
             "userid": session.uid,
             "from_date": start.isoformat(), "to_date": end.isoformat()},
        )
        if record["status"] == 200 and not record["empty_200"]:
            earliest = start.isoformat()
        elif earliest:
            break
    session.notes["earliest_data"] = earliest
    return earliest


def probe_query_type(session: Session) -> None:
    """summary vs detail, side by side. Spec question 5."""
    today = dt.date.today()
    week_ago = today - dt.timedelta(days=7)
    for query_type in ("summary", "detail"):
        get(session, f"query_type_{query_type}", "/v1/data/band_data.json",
            {"query_type": query_type, "device_type": "android_phone",
             "userid": session.uid,
             "from_date": week_ago.isoformat(), "to_date": today.isoformat()})


def main() -> None:
    email = os.environ.get("ZEPP_EMAIL")
    password = os.environ.get("ZEPP_PASSWORD")
    if not email or not password:
        sys.exit("Set ZEPP_EMAIL and ZEPP_PASSWORD in the environment.")

    redactor = Redactor()
    redactor.register(email, "email")
    redactor.register(password, "password")
    cap = Capture(OUT, redactor)

    with httpx.Client() as client:
        token_info = login(client, cap, redactor, email, password)
        session = Session(
            client=client, cap=cap, redactor=redactor,
            token=token_info["app_token"], uid=str(token_info["user_id"]),
        )
        session.notes["login_variant"] = token_info["_variant"]
        session.notes["token_info_keys"] = sorted(token_info.keys())

        session.host = probe_region(session)
        probe_retention(session)
        probe_query_type(session)

        cap.write_index()
        print(f"Captured {len(cap.entries)} responses to {OUT}")
        print(f"Region host: {session.notes['region_host']}")
        print(f"Earliest data: {session.notes['earliest_data']}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it imports and the module wiring is sound**

Run: `cd spike && uv run --with httpx --with pycryptodome python -c "import probe; print(len(probe.DATA_HOSTS), 'hosts')"`
Expected: `5 hosts` with no import error

- [ ] **Step 3: Run the live probe**

The maintainer runs this so credentials stay in his shell:

```bash
export ZEPP_EMAIL='...'
export ZEPP_PASSWORD='...'
cd spike && uv run probe.py
```

Expected: a printed region host, an earliest-data date, and populated `out/`.

**If login fails at step 1 for every variant:** the AES flow has been changed by Zepp since June 2026. Stop. This is spec §11 question 3 answering *no*, and it promotes `ManualTokenProvider` to the primary path in Plan 2. Record it in FINDINGS and do not keep retrying.

- [ ] **Step 4: Confirm no credentials reached disk**

```bash
cd spike && grep -ril -e "$ZEPP_PASSWORD" -e "$ZEPP_EMAIL" out/ && echo "LEAK — STOP" || echo "clean"
```
Expected: `clean`

- [ ] **Step 5: Commit**

```bash
cd spike && git add probe.py && git commit -q -m "spike: probe runner, region/retention/query_type sweep"
```

---

### Task 5: Endpoint and event family sweep

Answers spec §11 questions 4 and 12. Every endpoint in spec §4 and every `(eventType, subType)` pair across all three event families, one request each. Several are expected to fail; the failures establish which metrics this watch does not produce, which is the real input to the tool-surface scoping in spec §7.

**Files:**
- Modify: `spike/probe.py` (add `probe_endpoints`, `probe_events`; call both from `main`)

**Interfaces:**
- Consumes: `get`, `Session` from Task 4
- Produces: `probe_endpoints(session) -> None`; `probe_events(session) -> None`; `EVENTS_V1`, `EVENTS_DATESTRING`, `EVENTS_V2` list constants

- [ ] **Step 1: Add the sweep functions**

Insert into `spike/probe.py` above `main`:

```python
EVENTS_V1 = [
    ("all_day_stress", None), ("PaiHealthInfo", None),
    ("blood_oxygen", "click"), ("single_stress", None),
    ("health_data", "blood_pressure"),
]

EVENTS_DATESTRING = [
    ("blood_oxygen", "odi"), ("blood_oxygen", "osa_event"),
]

EVENTS_V2 = [
    ("readiness", "watch_score"), ("DailyHealth", "summary"),
    ("Charge", "real_data"), ("Charge", "stress_data"),
    ("hrv_sdnn", "real_data"), ("HRVRMSSD", "real_data"),
    ("RespiratoryRate", "real_data"), ("blood_pressure", "real_data"),
    ("Emotion", "real_data"), ("LactateThreshold", "summary"),
]


def probe_endpoints(session: Session) -> None:
    """One request per spec section 4 endpoint. Spec question 4."""
    uid = session.uid
    today = dt.date.today()
    month_ago = today - dt.timedelta(days=30)
    for name, path, params in [
        ("profile", "/huami.health.getUserInfo.json", None),
        ("manual_data", "/v1/user/manualData.json", None),
        ("weight", f"/users/{uid}/members/-1/weightRecords", None),
        ("blood_pressure", "/users/me/bloodPressure", None),
        ("heart_rate", f"/users/{uid}/heartRate", None),
        ("sport_load", f"/v2/watch/users/{uid}/WatchSportStatistics/SPORT_LOAD", None),
        ("vo2_max", f"/v2/watch/users/{uid}/WatchSportStatistics/VO2_MAX", None),
        ("second_hr_index", "/users/me/fileInfo/events",
         {"eventType": "second_heart_rate", "subType": "real_data",
          "from": _ms(month_ago), "to": _ms(today), "limit": 50}),
    ]:
        get(session, name, path, params)


def _ms(day: dt.date) -> str:
    return str(int(dt.datetime.combine(day, dt.time.min).timestamp() * 1000))


def probe_events(session: Session) -> None:
    """Every event family and pair. Spec questions 4 and 12."""
    uid = session.uid
    today = dt.date.today()
    month_ago = today - dt.timedelta(days=30)
    from_ms, to_ms = _ms(month_ago), _ms(today)

    for event_type, sub_type in EVENTS_V1:
        params = {"from": from_ms, "to": to_ms,
                  "eventType": event_type, "limit": 20, "reverse": "false"}
        if sub_type:
            params["subType"] = sub_type
        get(session, f"ev1_{event_type}_{sub_type or 'none'}",
            f"/users/{uid}/events", params)

    for event_type, sub_type in EVENTS_DATESTRING:
        get(session, f"evdate_{event_type}_{sub_type}",
            f"/users/{uid}/events/dateString",
            {"from": month_ago.isoformat(), "to": today.isoformat(),
             "eventType": event_type, "subType": sub_type,
             "timeZone": "Asia/Kolkata", "limit": 20})

    for event_type, sub_type in EVENTS_V2:
        get(session, f"ev2_{event_type}_{sub_type}", "/v2/users/me/events",
            {"from": from_ms, "to": to_ms, "eventType": event_type,
             "subType": sub_type, "limit": 20})
```

- [ ] **Step 2: Wire them into `main`**

In `spike/probe.py`, after the `probe_query_type(session)` line, add:

```python
        probe_endpoints(session)
        probe_events(session)
```

- [ ] **Step 3: Verify the sweep is complete against the spec**

Run: `cd spike && uv run --with httpx --with pycryptodome python -c "
import probe
total = len(probe.EVENTS_V1) + len(probe.EVENTS_DATESTRING) + len(probe.EVENTS_V2)
print('event pairs:', total)
assert total == 17, total
print('ok')
"`
Expected: `event pairs: 17` then `ok`

- [ ] **Step 4: Run the live sweep**

```bash
cd spike && uv run probe.py
```
Expected: ~30 captures in `out/`. Failures are expected and are data.

If `timeZone` is rejected on the dateString family, note the error in FINDINGS and move on — do not iterate on it here.

- [ ] **Step 5: Commit**

```bash
cd spike && git add probe.py && git commit -q -m "spike: endpoint and event family sweep"
```

---

### Task 6: Workout probes

Answers spec §11 questions 6, 7, 8, 9 and 10 — the gaps §4 of the spec identifies in the original inventory. Four unknowns: whether `history.json` returns all sports or only the one named in the path, what the detail endpoint and TrackID format are, whether the index carries sport-specific summary fields, and whether any multisport activity exists.

**Files:**
- Modify: `spike/probe.py` (add `probe_workouts`; call from `main`)

**Interfaces:**
- Consumes: `get`, `Session` from Task 4
- Produces: `probe_workouts(session) -> None`; `_extract_workouts(records: list[dict]) -> list[dict]` (each row carries added `_track_id: str` and `_duration: int` keys); `SPORT_SLUGS: list[str]`

- [ ] **Step 1: Add the workout probe**

Insert into `spike/probe.py` above `main`:

```python
SPORT_SLUGS = [
    "run", "walking", "cycling", "swimming",
    "indoor_swimming", "strength", "football",
]


def probe_workouts(session: Session) -> None:
    """Spec questions 6, 7, 8, 10."""
    today = dt.date.today()
    far_back = today.replace(year=today.year - 5)

    # Does the sport path parameter partition the results, or is it ignored?
    for slug in SPORT_SLUGS:
        get(session, f"workout_history_{slug}", f"/v1/sport/{slug}/history.json",
            {"from_date": far_back.isoformat(), "to_date": today.isoformat(),
             "userid": session.uid})

    # Fetch one detail response PER SPORT TYPE, not merely the first few.
    # Spec section 9 requires every workout decoder to be tested against a real
    # activity of that sport; three consecutive runs would leave the swim,
    # cycling and football decoders with no fixture at all.
    index_records = [
        entry for entry in session.cap.entries
        if entry["name"].startswith("workout_history_")
        and entry["status"] == 200 and not entry["empty_200"]
    ]
    workouts = _extract_workouts(index_records)
    session.notes["workout_count_seen"] = len(workouts)

    by_type: dict[str, dict] = {}
    for row in workouts:
        key = str(row.get("type", "unknown"))
        by_type.setdefault(key, row)
    session.notes["workout_types_seen"] = sorted(by_type)

    for type_key, row in by_type.items():
        track_id = row["_track_id"]
        get(session, f"workout_detail_type{type_key}_{track_id}",
            "/v1/sport/run/detail.json",
            {"trackid": track_id, "source": row.get("source", "run.mobile"),
             "userid": session.uid})

    # Longest activity overall is the best multisport candidate. Spec question 10.
    if workouts:
        longest = max(workouts, key=lambda r: int(r.get("_duration", 0) or 0))
        if longest["_track_id"] not in {r["_track_id"] for r in by_type.values()}:
            get(session, f"workout_detail_longest_{longest['_track_id']}",
                "/v1/sport/run/detail.json",
                {"trackid": longest["_track_id"],
                 "source": longest.get("source", "run.mobile"),
                 "userid": session.uid})


def _extract_workouts(records: list[dict]) -> list[dict]:
    """Flatten index rows. TrackIDs are UNIX timestamps; field naming is
    unverified, so probe several spellings and keep the whole row."""
    found: list[dict] = []
    seen: set[str] = set()
    for record in records:
        body = record.get("body_parsed")
        if not isinstance(body, dict):
            continue
        payload = body.get("data")
        rows = payload.get("summary") if isinstance(payload, dict) else payload
        if isinstance(rows, str):
            import json as _json
            try:
                rows = _json.loads(rows)
            except ValueError:
                continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            track_id = row.get("trackid") or row.get("trackId") or row.get("id")
            if track_id is None or str(track_id) in seen:
                continue
            seen.add(str(track_id))
            enriched = dict(row)
            enriched["_track_id"] = str(track_id)
            enriched["_duration"] = (
                row.get("endtime", 0) or 0) - (row.get("trackid", 0) or 0)
            found.append(enriched)
    return found
```

- [ ] **Step 2: Wire it into `main`**

In `spike/probe.py`, after `probe_events(session)`, add:

```python
        probe_workouts(session)
```

- [ ] **Step 3: Verify it imports**

Run: `cd spike && uv run --with httpx --with pycryptodome python -c "import probe; print(len(probe.SPORT_SLUGS), 'sports')"`
Expected: `7 sports`

- [ ] **Step 4: Run and inspect**

```bash
cd spike && uv run probe.py
cd spike && uv run --with httpx --with pycryptodome python -c "
import json, pathlib
idx = json.loads((pathlib.Path('out')/'index.json').read_text())
for e in idx:
    if e['name'].startswith('workout'):
        print(f\"{e['status']:>4}  {e['body_bytes']:>8}b  {e['name']}\")
"
```

Expected: a status and body size per sport slug, plus one `workout_detail_type*` capture per distinct activity type found.

**If every slug returns the same body size, the path parameter is ignored and one call covers all sports** — that is spec question 6 answered, and it means `zepp_list_workouts` needs no per-sport fan-out.

Then confirm the per-sport fixture coverage that spec §9 requires:

```bash
cd spike && ls out/ | grep -c workout_detail_type
```

Expected: at least 4, covering swim, run, ride and one other. **If it returns 0 or 1, the type field is spelled differently than `type` in the index rows.** Inspect one index capture, find the actual field, and correct the `row.get("type", ...)` key in `probe_workouts` before rerunning — a corpus without a real swim activity cannot support the SWOLF and stroke decoders in Plan 3.

If `detail.json` 404s, the detail endpoint has a different path. Grep an index capture for the field names around TrackIDs, record what you find in FINDINGS, and leave it for Plan 2 rather than iterating here.

- [ ] **Step 5: Commit**

```bash
cd spike && git add probe.py && git commit -q -m "spike: workout history, sport partitioning, detail probes"
```

---

### Task 7: Findings report and fixture promotion

Turns the corpus into the two artefacts Plan 2 consumes: a written answer to each of the twelve questions in spec §11, and a redacted fixture set committed to the project repository.

**Files:**
- Create: `spike/report.py`
- Create: `docs/superpowers/findings/2026-08-15-spike-findings.md` (in the project repo)
- Create: `tests/fixtures/zepp/` (in the project repo)

**Interfaces:**
- Consumes: `out/index.json` and the capture files from Tasks 4–6
- Produces: `FINDINGS.md`; the promoted fixture corpus

- [ ] **Step 1: Write the report generator**

Write `spike/report.py`:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Summarise the capture corpus into FINDINGS.md."""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "out"


def main() -> None:
    index = json.loads((OUT / "index.json").read_text())

    ok = [e for e in index if e["status"] == 200 and not e["empty_200"]]
    empty = [e for e in index if e["empty_200"]]
    failed = [e for e in index if e["status"] not in (200, 303)]

    lines = [
        "# Zepp Recon Spike — Findings",
        "",
        f"**Date:** 2026-08-15 · **Captures:** {len(index)}",
        f"**With data:** {len(ok)} · **Empty-200:** {len(empty)} · "
        f"**Failed:** {len(failed)}",
        "",
        "## Endpoints returning data",
        "",
        "| Status | Bytes | Name |",
        "|---|---|---|",
    ]
    lines += [f"| {e['status']} | {e['body_bytes']} | `{e['name']}` |" for e in ok]

    lines += ["", "## Empty-200 (present but no records)", ""]
    lines += [f"- `{e['name']}`" for e in empty] or ["- none"]

    lines += ["", "## Failed", "", "| Status | Name |", "|---|---|"]
    lines += [f"| {e['status']} | `{e['name']}` |" for e in failed] or ["| — | none |"]

    lines += [
        "", "## Answers to spec section 11", "",
        "Fill each in from the captures above before writing Plan 2.", "",
        "1. **Region host:** ",
        "2. **Earliest data served:** ",
        "3. **Password flow works (Aug 2026):** ",
        "4. **Metrics this watch produces:** ",
        "5. **query_type summary vs detail:** ",
        "6. **history.json covers all sports:** ",
        "7. **Detail endpoint / TrackID format:** ",
        "8. **Index carries sport-specific summaries:** ",
        "9. **VO2 max global or per-discipline:** ",
        "10. **Multisport activities present:** ",
        "11. **Devices paired / stream collision:** ",
        "12. **Event data/extra payload shape:** ",
        "",
    ]

    path = Path(__file__).parent / "FINDINGS.md"
    path.write_text("\n".join(lines))
    print(f"Wrote {path} — {len(ok)} endpoints with data")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Generate the report**

Run: `cd spike && uv run report.py`
Expected: `Wrote .../FINDINGS.md — N endpoints with data`

- [ ] **Step 3: Answer the twelve questions by hand**

Open `spike/FINDINGS.md` and fill each of the twelve blanks from the captures. This is the deliverable — the tables are supporting evidence, the answers are what Plan 2 is written from.

- [ ] **Step 4: Verify redaction, then promote the fixtures**

Redaction is verified before anything enters the repository, because this corpus becomes committed test fixtures:

```bash
cd spike && grep -ril -e "$ZEPP_PASSWORD" -e "$ZEPP_EMAIL" out/ && echo "LEAK — STOP" || echo "clean"
cd spike && grep -rl '"app_token": "[^<]' out/ && echo "LEAK — STOP" || echo "clean"
```
Expected: `clean` twice. **If either reports a leak, stop and fix `redact.py` before continuing.**

Then promote:

```bash
PROJ=/path/to/zepp-app-mcp
mkdir -p "$PROJ/tests/fixtures/zepp" "$PROJ/docs/superpowers/findings"
cp spike/out/*.json "$PROJ/tests/fixtures/zepp/"
cp spike/FINDINGS.md "$PROJ/docs/superpowers/findings/2026-08-15-spike-findings.md"
```

- [ ] **Step 5: Commit to the project repository**

```bash
cd /path/to/zepp-app-mcp
git add tests/fixtures/zepp docs/superpowers/findings
git commit -m "Add recon spike fixture corpus and findings

Captured from a live Zepp account and redacted at write time. Answers the
twelve open questions in the design spec section 11 and provides the fixture
corpus every decoder in Plan 2 is tested against.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## What Plan 2 needs from this

Plan 2 (server foundations: `auth.py`, `client.py`, scaffold) cannot be written until Tasks 3 and 4 land, because `Credential.region_host` extraction depends on the observed shape of `token_info`, and the empty-200 classifier needs a real empty-200 to test against.

Plan 3 (decoders and the eight tools) cannot be written until Task 7 lands, because every decoder test asserts against a fixture in `tests/fixtures/zepp/`.

Both are written after this plan executes, from the findings — not before.
