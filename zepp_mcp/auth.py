"""Zepp password login, plus a token cache.

Differences from the recon spike, all of them forced by what the spike found:

* One login attempt, never a loop. The step-1 redirect carries
  `attempts=N&max_attempts=10`, and that counter is shared across every host
  and region -- so a retry loop walks the account toward a lockout. The spike
  also showed the `region` parameter is ignored at step 1, which made the
  five-variant matrix five identical requests.
* The region host is read from the login response's `domains` block rather
  than probed. The server returns the account's real region; we never guess.
* The token is cached, so a server restart does not mean a fresh login. Only
  the credential is cached. No health data is ever written to disk.

Step 2's `app_name` is deliberately NOT `com.huami.midong` (the real Zepp
Android app's package id). Huami's backend appears to key a session by
`(user, app_name)`: logging in under the phone app's own identity evicts the
phone's active session (see GitHub issue #5). We instead identify as the
retired Mi Fit client id, `com.xiaomi.hm.health`, which other unofficial
Huami API clients (e.g. micw/hacking-mifit-api) use successfully without
this collision. `device_id` is likewise held stable across logins rather
than randomized, so we don't look like a new device registering every time
the cached token expires.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

_AES_KEY = b"xeNtBVqzDc6tuNTh"
_AES_IV = b"MAAAYAAAAAAAAABg"
_REDIRECT = "https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html"

_AUTH_HOST = "api-user.zepp.com"
_DEFAULT_DATA_HOST = "api-mifit.zepp.com"

# The app identity we register the token under at step 2. Must NOT be
# "com.huami.midong" (the real Zepp app) -- see module docstring.
_CLIENT_APP_NAME = "com.xiaomi.hm.health"

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


class LoginError(RuntimeError):
    """Login failed. Message is safe to surface; it never contains secrets."""


@dataclass
class Credential:
    app_token: str
    user_id: str
    region_host: str
    country_code: str
    expires_at: float
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        # Refresh an hour early rather than discover expiry mid-request.
        return time.time() >= self.expires_at - 3600

    def to_json(self) -> dict[str, Any]:
        return {
            "app_token": self.app_token, "user_id": self.user_id,
            "region_host": self.region_host, "country_code": self.country_code,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_json(cls, blob: dict[str, Any]) -> "Credential":
        return cls(
            app_token=blob["app_token"], user_id=blob["user_id"],
            region_host=blob["region_host"],
            country_code=blob.get("country_code", ""),
            expires_at=float(blob["expires_at"]),
        )


def _build_payload(email: str, password: str, country: str) -> bytes:
    body = urllib.parse.urlencode({
        "emailOrPhone": email, "password": password,
        "state": "REDIRECTION", "client_id": "HuaMi",
        "redirect_uri": _REDIRECT,
        "region": "us-west-2", "token": ["access", "refresh"],
        "country_code": country,
    }, doseq=True).encode()
    return AES.new(_AES_KEY, AES.MODE_CBC, iv=_AES_IV).encrypt(pad(body, AES.block_size))


def _region_host(payload: dict[str, Any]) -> str:
    """Pick the regional CNAME for the data host out of the login response.

    The response carries a `domains` list mapping each logical host to its
    regional alias, e.g. api-mifit.zepp.com -> api-mifit-us3.zepp.com.
    """
    for entry in payload.get("domains") or []:
        if not isinstance(entry, dict) or entry.get("host") != _DEFAULT_DATA_HOST:
            continue
        cnames = entry.get("cnames") or []
        if cnames and isinstance(cnames[0], str):
            return cnames[0]
    return _DEFAULT_DATA_HOST


def login(client: httpx.Client, email: str, password: str,
          country: str = "US") -> Credential:
    """Two-step password login. Exactly one attempt at each step."""
    response = client.post(
        f"https://{_AUTH_HOST}/v2/registrations/tokens",
        content=_build_payload(email, password, country),
        headers=_STEP1_HEADERS, follow_redirects=False, timeout=20,
    )
    location = response.headers.get("location", "")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    access_code = (query.get("access") or [None])[0]

    if not access_code:
        error = (query.get("error") or ["no access code returned"])[0]
        attempts = (query.get("attempts") or [None])[0]
        maximum = (query.get("max_attempts") or [None])[0]
        detail = f" (failed attempt {attempts} of {maximum})" if attempts else ""
        raise LoginError(
            f"Zepp login rejected the credentials: {error}{detail}. "
            "Check ZEPP_EMAIL and ZEPP_PASSWORD. This is not retried "
            "automatically -- repeated failures lock the account."
        )

    # Step 2: exchange the one-time code for a long-lived app token.
    response = client.post(
        f"https://{_DEFAULT_DATA_HOST}/v2/client/login",
        timeout=20, headers=_STEP2_HEADERS, data={
            "code": access_code, "device_id": device_id(),
            "grant_type": "access_token", "third_name": "huami",
            "app_name": _CLIENT_APP_NAME, "country_code": country,
            "device_model": "android_phone", "app_version": "9.12.5",
            "allow_registration": "false", "lang": "en",
            "dn": "api-mifit.zepp.com,api-user.zepp.com,"
                  "api-watch.zepp.com,auth.zepp.com",
            "source": "com.huami.watch.hmwatchmanager:9.12.5:151689",
        },
    )
    try:
        payload = response.json()
    except ValueError:
        raise LoginError(
            f"Token exchange returned HTTP {response.status_code} with a "
            "non-JSON body."
        ) from None

    token_info = payload.get("token_info") or {}
    app_token = token_info.get("app_token")
    if not app_token:
        raise LoginError(
            f"Token exchange returned HTTP {response.status_code} but no "
            f"app_token (result={payload.get('result')!r})."
        )

    # app_ttl is the app_token's lifetime in seconds; observed at 2592000 (30
    # days). `ttl` is the longer-lived login_token and is not what we hold.
    ttl = int(token_info.get("app_ttl") or 0) or 2592000
    regist = payload.get("regist_info") or {}

    return Credential(
        app_token=app_token,
        user_id=str(token_info.get("user_id", "")),
        region_host=_region_host(payload),
        country_code=regist.get("country_code") or country,
        expires_at=time.time() + ttl,
    )


# --------------------------------------------------------------------------
# Device identity
# --------------------------------------------------------------------------

def device_id_path() -> Path:
    return cache_path().with_name("device_id")


def device_id() -> str:
    """A stable per-install device id, persisted next to the token cache.

    Not a secret, just an opaque identifier -- kept stable across logins so
    a re-login (e.g. after the cached token expires or a 401) doesn't look
    like a new device registering under the account. See the module
    docstring for why that matters.
    """
    if not cache_enabled():
        return str(uuid.uuid4())
    path = device_id_path()
    if path.is_file():
        cached = path.read_text().strip()
        if cached:
            return cached
    new_id = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id)
        path.chmod(0o600)
    except OSError:
        pass  # Falls back to a fresh id next call; not a secret, not fatal.
    return new_id


# --------------------------------------------------------------------------
# Token cache
# --------------------------------------------------------------------------

def cache_path() -> Path:
    override = os.environ.get("ZEPP_TOKEN_CACHE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".zepp-mcp" / "token.json"


def cache_enabled() -> bool:
    return os.environ.get("ZEPP_TOKEN_CACHE", "").lower() not in ("0", "off", "none")


def load_cached() -> Credential | None:
    if not cache_enabled():
        return None
    path = cache_path()
    if not path.is_file():
        return None
    try:
        credential = Credential.from_json(json.loads(path.read_text()))
    except (ValueError, KeyError, OSError):
        return None
    return None if credential.expired else credential


def save_cached(credential: Credential) -> None:
    if not cache_enabled():
        return
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(credential.to_json(), indent=2))
        path.chmod(0o600)
    except OSError:
        pass  # A cache miss is recoverable; a crashed server is not.


def clear_cached() -> None:
    try:
        cache_path().unlink(missing_ok=True)
    except OSError:
        pass


def describe_expiry(credential: Credential) -> str:
    when = dt.datetime.fromtimestamp(credential.expires_at, dt.UTC)
    return when.isoformat(timespec="seconds")
