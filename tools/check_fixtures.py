# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Fail the build if personal data reaches the fixture corpus.

The corpus is captured from real Zepp accounts, so every fixture is one
mistake away from publishing someone's identity, biometrics, home location or
credentials. Sanitising once is not enough -- the next contributor to add a
capture will not know what to strip.

This runs in CI on every push and pull request, in three layers:

  1. STRUCTURAL   shapes that are sensitive regardless of whose they are:
                  emails, MAC addresses, coordinate streams, tokens.
  2. PLACEHOLDER  fields the anonymiser rewrites must actually hold the
                  expected fake value. Catches a capture added without
                  running the anonymiser at all.
  3. LOCAL        if tools/identities.local.json exists, assert that none of
                  its original values survive. This is the strongest check
                  and only the person holding that file can run it; CI skips
                  it, which is why layers 1 and 2 exist.

**This file contains no real identifiers.** Publishing a checker that greps
for a maintainer's serial number would publish the serial number.

Run locally the same way CI does:  uv run tools/check_fixtures.py
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "zepp"
IDENTITIES = Path(__file__).parent / "identities.local.json"

# Layer 1: shapes that are sensitive no matter whose data they are.
STRUCTURAL: list[tuple[str, re.Pattern[str], str]] = [
    ("email address",
     re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
     "identifies the account holder"),

    ("MAC address",
     re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"),
     "uniquely identifies a physical device across datasets"),

    ("JWT-shaped token",
     re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
     "may be a live credential"),

    ("latitude/longitude stream",
     re.compile(r'"longitude_latitude"\s*:\s*"\s*-?\d'),
     "a route starts where the athlete lives"),

    ("home directory path",
     # Case-SENSITIVE: the Zepp API has its own /users/me/ route, and a
     # case-insensitive match flags every fixture that contains it.
     re.compile(r"/(?:Users|home)/[a-z][a-z0-9_-]*/"),
     "leaks a username and local filesystem layout"),
]

# Layer 2: what the anonymiser guarantees. Each entry is a JSON key and the
# only values considered clean. All of these are obvious fakes.
EXPECTED_PLACEHOLDERS: dict[str, set[str]] = {
    "nickname": {"example_user", "<nickname>"},
    "nick_name": {"Alex", "<nick_name>"},
    "third_id": {"THIRD_PARTY_ID_REDACTED_0000000000", "<third_id>"},
    "avatar": {"<avatar_url>", ""},
    "birthday": {"1990-01", ""},
    "deviceMac": {"AABBCC001122", "<deviceMac>", ""},
    "sn": {"00000000000000", "<sn>", ""},
    "deviceSn": {"00000000000000", "<deviceSn>", ""},
}

# `summary` is base64-wrapped JSON and hides text from a plain grep.
B64_JSON_KEYS = ("summary",)


def _b64_payloads(obj: object) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key) in B64_JSON_KEYS and isinstance(value, str):
                try:
                    out.append(
                        base64.b64decode(value, validate=True).decode("utf-8", "ignore")
                    )
                except (ValueError, binascii.Error):
                    pass
            out.extend(_b64_payloads(value))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_b64_payloads(item))
    return out


def _placeholder_violations(obj: object, where: str) -> list[str]:
    """Walk parsed JSON asserting the anonymiser's guarantees hold."""
    problems: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            name = str(key)
            allowed = EXPECTED_PLACEHOLDERS.get(name)
            if allowed is not None and isinstance(value, (str, int, float)):
                if str(value) not in allowed:
                    problems.append(
                        f"{where}: `{name}` holds {str(value)[:40]!r}, expected "
                        f"one of {sorted(allowed)}"
                    )
            problems.extend(_placeholder_violations(value, where))
    elif isinstance(obj, list):
        for item in obj:
            problems.extend(_placeholder_violations(item, where))
    return problems


def _load_local_originals() -> list[str]:
    if not IDENTITIES.is_file():
        return []
    config = json.loads(IDENTITIES.read_text())
    values = [str(k) for k in (config.get("strings") or {}) if not k.startswith("_")]
    values += [str(k) for k in (config.get("integers") or {}) if not str(k).startswith("_")]
    return [v for v in values if len(v) >= 6]


def scan(path: Path, originals: list[str]) -> list[str]:
    text = path.read_text()
    haystacks = [("", text)]

    parsed = None
    try:
        parsed = json.loads(text)
    except ValueError:
        pass
    if parsed is not None:
        haystacks.extend(
            (" (inside base64)", blob) for blob in _b64_payloads(parsed)
        )

    problems: list[str] = []
    for suffix, haystack in haystacks:
        for label, pattern, why in STRUCTURAL:
            match = pattern.search(haystack)
            if match:
                problems.append(
                    f"{path.name}{suffix}: {label} -- {why}\n"
                    f"        found: {match.group(0)[:60]!r}"
                )
        for original in originals:
            if original in haystack:
                problems.append(
                    f"{path.name}{suffix}: a value from identities.local.json "
                    f"survived -- run `uv run tools/anonymize_fixtures.py`"
                )

    if parsed is not None:
        problems.extend(_placeholder_violations(parsed, path.name))
        for blob in _b64_payloads(parsed):
            try:
                problems.extend(
                    _placeholder_violations(
                        json.loads(blob), f"{path.name} (inside base64)"
                    )
                )
            except ValueError:
                pass
    return problems


def main() -> int:
    if not FIXTURES.is_dir():
        print(f"No fixture corpus at {FIXTURES}", file=sys.stderr)
        return 1

    originals = _load_local_originals()
    paths = sorted(FIXTURES.glob("*.json"))

    problems: list[str] = []
    for path in paths:
        problems.extend(scan(path, originals))

    if problems:
        print(f"PRIVACY CHECK FAILED -- {len(problems)} issue(s)\n", file=sys.stderr)
        for problem in sorted(set(problems)):
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nDo not commit these. Fixtures are public the moment they are\n"
            "pushed, and git history is not undoable.",
            file=sys.stderr,
        )
        return 1

    layers = f"{len(STRUCTURAL)} structural, {len(EXPECTED_PLACEHOLDERS)} placeholder"
    if originals:
        layers += f", {len(originals)} local"
    else:
        layers += ", local skipped (no identities.local.json)"
    print(f"privacy check passed: {len(paths)} fixtures ({layers})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
