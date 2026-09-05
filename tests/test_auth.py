"""Tests for the session-collision fix (GitHub issue #5).

Login previously registered the app token under `com.huami.midong` -- the
real Zepp Android app's package id -- with a freshly randomized device_id on
every call. Huami's backend appears to key an active session by
`(user, app_name)`, so this looked like the phone's own app logging in from
a new device each time, and evicted the phone's session. These tests pin
the two properties that avoid that collision: a non-colliding app_name, and
a device_id that stays stable across repeated logins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from zepp_mcp import auth


class _FakeResponse:
    def __init__(self, headers: dict[str, str] | None = None,
                 status_code: int = 200, json_data: dict[str, Any] | None = None):
        self.headers = headers or {}
        self.status_code = status_code
        self._json = json_data

    def json(self) -> dict[str, Any]:
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeClient:
    """Stands in for httpx.Client; records every POST for inspection."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


def _step1_ok() -> _FakeResponse:
    return _FakeResponse(headers={
        "location": "https://s3-us-west-2.amazonaws.com/hm-registration/"
                     "successsignin.html?access=one-time-code&state=REDIRECTION",
    })


def _step2_ok(app_token: str = "tok-1", user_id: str = "42") -> _FakeResponse:
    return _FakeResponse(json_data={
        "token_info": {"app_token": app_token, "user_id": user_id, "app_ttl": 100},
        "regist_info": {"country_code": "US"},
        "domains": [],
    })


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZEPP_TOKEN_CACHE", str(tmp_path / "token.json"))


def test_login_does_not_use_the_phone_apps_identity():
    client = _FakeClient([_step1_ok(), _step2_ok()])

    auth.login(client, "e@example.com", "pw")

    _, step2_kwargs = client.calls[1]
    assert step2_kwargs["data"]["app_name"] != "com.huami.midong"
    assert step2_kwargs["data"]["app_name"] == auth._CLIENT_APP_NAME


def test_device_id_is_stable_across_separate_logins():
    first = _FakeClient([_step1_ok(), _step2_ok()])
    auth.login(first, "e@example.com", "pw")
    first_device_id = first.calls[1][1]["data"]["device_id"]

    second = _FakeClient([_step1_ok(), _step2_ok()])
    auth.login(second, "e@example.com", "pw")
    second_device_id = second.calls[1][1]["data"]["device_id"]

    assert first_device_id == second_device_id


def test_device_id_persists_to_disk_next_to_the_token_cache():
    first_call = auth.device_id()
    second_call = auth.device_id()

    assert first_call == second_call
    assert auth.device_id_path().read_text().strip() == first_call


def test_device_id_is_ephemeral_when_caching_is_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZEPP_TOKEN_CACHE", "off")

    assert auth.device_id() != auth.device_id()
