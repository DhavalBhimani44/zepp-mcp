"""HTTP client for the Zepp mobile API.

Two behaviours matter more than the plumbing:

* An empty 200 is not the same as "you have no data". Zepp returns HTTP 200
  with an empty payload both when a day genuinely has no records and when a
  request is subtly malformed. The two are indistinguishable from a single
  response, so this client reports `no_data` with that ambiguity attached
  rather than asserting absence. An MCP tool that renders an empty 200 as
  "you didn't exercise that week" is stating a fault as a fact.
* A 401 means the app_token expired. The client re-authenticates once, then
  gives up -- it never loops against the auth endpoint.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

from .auth import (Credential, LoginError, clear_cached, load_cached, login,
                   save_cached)


@dataclass
class Outcome:
    """A response plus an explicit verdict about what it means."""
    status: str          # "ok" | "no_data" | "error"
    http_status: int
    data: Any = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status,
                               "http_status": self.http_status}
        if self.note:
            out["note"] = self.note
        if self.data is not None:
            out["data"] = self.data
        return out


def _is_empty(payload: Any) -> bool:
    if payload is None or payload == [] or payload == {}:
        return True
    if isinstance(payload, dict):
        inner = payload.get("data")
        if inner in (None, [], {}, ""):
            return True
        if isinstance(inner, dict):
            meaningful = {k: v for k, v in inner.items() if k != "next"}
            return not meaningful or all(v in (None, [], {}, "")
                                         for v in meaningful.values())
    return False


class ZeppClient:
    def __init__(self, email: str, password: str, country: str = "US") -> None:
        self._email = email
        self._password = password
        self._country = country
        self._client = httpx.Client(timeout=30)
        self._credential: Credential | None = None

    # -- auth ------------------------------------------------------------
    def credential(self) -> Credential:
        if self._credential and not self._credential.expired:
            return self._credential
        cached = load_cached()
        if cached:
            self._credential = cached
            return cached
        self._credential = login(
            self._client, self._email, self._password, self._country
        )
        save_cached(self._credential)
        return self._credential

    def _reauthenticate(self) -> Credential:
        clear_cached()
        self._credential = None
        return self.credential()

    # -- requests --------------------------------------------------------
    def get(self, path: str, params: dict[str, Any] | None = None) -> Outcome:
        credential = self.credential()
        outcome = self._request(credential, path, params)
        if outcome.http_status == 401:
            try:
                credential = self._reauthenticate()
            except LoginError as exc:
                return Outcome("error", 401, note=str(exc))
            outcome = self._request(credential, path, params)
        return outcome

    def _request(self, credential: Credential, path: str,
                 params: dict[str, Any] | None) -> Outcome:
        url = f"https://{credential.region_host}{path}"
        try:
            response = self._client.get(
                url, headers={"apptoken": credential.app_token},
                params=params or {},
            )
        except httpx.HTTPError as exc:
            return Outcome("error", 0, note=f"network error: {exc}")

        if response.status_code >= 400:
            return Outcome(
                "error", response.status_code,
                note=f"Zepp returned HTTP {response.status_code} for {path}. "
                     f"Body starts: {response.text[:200]!r}",
            )

        try:
            payload = response.json()
        except ValueError:
            return Outcome("error", response.status_code,
                           note="response was not JSON")

        if _is_empty(payload):
            return Outcome(
                "no_data", response.status_code, data=payload,
                note="Zepp returned HTTP 200 with an empty payload. This "
                     "means either there is genuinely no data for this range, "
                     "or the request was rejected in a way the API does not "
                     "report. These are not distinguishable from this "
                     "response alone -- do not report it as confirmed absence.",
            )
        return Outcome("ok", response.status_code, data=payload)

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

def band_data(client: ZeppClient, from_date: str, to_date: str) -> Outcome:
    """Daily steps / sleep / heart rate. `detail` is a superset of `summary`."""
    return client.get("/v1/data/band_data.json", {
        "query_type": "detail", "device_type": "android_phone",
        "userid": client.credential().user_id,
        "from_date": from_date, "to_date": to_date,
    })


def workout_history(client: ZeppClient, from_date: str, to_date: str) -> Outcome:
    """All workouts, every sport.

    The `run` path segment is a fixed route name, not a filter: this one call
    returns every activity type. Every other slug 404s.
    """
    return client.get("/v1/sport/run/history.json", {
        "from_date": from_date, "to_date": to_date,
        "userid": client.credential().user_id,
    })


def workout_detail(client: ZeppClient, track_id: str, source: str) -> Outcome:
    return client.get("/v1/sport/run/detail.json", {
        "trackid": track_id, "source": source,
        "userid": client.credential().user_id,
    })


def from_env() -> ZeppClient:
    email = os.environ.get("ZEPP_EMAIL")
    password = os.environ.get("ZEPP_PASSWORD")
    if not email or not password:
        raise LoginError(
            "ZEPP_EMAIL and ZEPP_PASSWORD are not set. Put them in .env next "
            "to pyproject.toml, or in the server's environment."
        )
    return ZeppClient(email, password, os.environ.get("ZEPP_COUNTRY", "US"))


def load_dotenv(path: str | os.PathLike[str]) -> None:
    """Read KEY=VALUE lines into the environment. Real env vars win.

    Never logs values -- this file holds a password.
    """
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def default_range(days: int = 7) -> tuple[str, str]:
    today = dt.date.today()
    return (today - dt.timedelta(days=days)).isoformat(), today.isoformat()


def parse_rows(payload: Any) -> list[dict[str, Any]]:
    """Workout index rows arrive as a JSON string nested inside JSON."""
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("summary") if isinstance(data, dict) else data
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except ValueError:
            return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
