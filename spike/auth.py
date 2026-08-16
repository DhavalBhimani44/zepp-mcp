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
