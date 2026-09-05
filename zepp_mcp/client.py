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
    """A response is empty when its data container holds nothing.

    Zepp uses two different container keys across API generations: v1
    endpoints (band_data, workout history, profile) wrap results in `data`,
    while v2 events (`/v2/users/me/events` -- readiness, HRV, DailyHealth,
    RespiratoryRate, LactateThreshold) wrap them in `items`.

    Checking only `data` meant every v2 response was reported empty
    regardless of content, because `payload.get("data")` is None whether or
    not `items` holds real records. This was found live: `hrv_sdnn` and
    `readiness` each returned 20 real samples and were both reported
    `no_data` -- which the server then instructed the model to describe as
    "the query came back empty", a fault stated as a fact and the exact
    failure this classifier exists to prevent.
    """
    if payload is None or payload == [] or payload == {}:
        return True
    if not isinstance(payload, dict):
        return False

    if "items" in payload:
        return payload.get("items") in (None, [], {}, "")

    if "data" in payload:
        inner = payload.get("data")
        if inner in (None, [], {}, ""):
            return True
        if isinstance(inner, dict):
            meaningful = {k: v for k, v in inner.items() if k != "next"}
            return not meaningful or all(v in (None, [], {}, "")
                                         for v in meaningful.values())
        return False

    # Neither recognised container is present. This is an unfamiliar shape,
    # not a known-empty one -- treat it as non-empty so the caller can
    # inspect the payload rather than have it silently disappear as
    # "no data".
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


def lactate_threshold_history(client: ZeppClient, from_date: str,
                              to_date: str) -> Outcome:
    """The watch's own lactate threshold estimate log.

    This is the AUTHORITATIVE source, not the workout row. `lthr` also
    appears on individual run rows (see workouts.py), but this endpoint
    carries the full history with its own `dateString` per estimate, so it
    stands on its own without joining against workout history -- and it
    survives even if the originating run later drops out of the retention
    window `zepp_list_workouts` queries.

    Found via `/v2/users/me/events?eventType=LactateThreshold&subType=
    summary`, the same v2 events family that also carries readiness, HRV,
    DailyHealth and RespiratoryRate. It returns real data (verified: two
    estimates, 166 and 173 bpm) and was masked entirely by the `_is_empty`
    bug that only checked for a `data` key -- this endpoint's response uses
    `items`, so every call here was reported `no_data` until that fix.
    """
    start = dt.datetime.strptime(from_date, "%Y-%m-%d")
    end = dt.datetime.strptime(to_date, "%Y-%m-%d") + dt.timedelta(days=1)
    return client.get("/v2/users/me/events", {
        "from": str(int(start.timestamp() * 1000)),
        "to": str(int(end.timestamp() * 1000)),
        "eventType": "LactateThreshold", "subType": "summary", "limit": 50,
    })


def weight_records(client: ZeppClient, limit: int = 20,
                   before: str | None = None) -> Outcome:
    """Body-composition scale readings for the account holder (member -1).

    Neither the endpoint nor its schema is in Zepp's own API surface docs --
    found via a sibling open-source Zepp API client, AlexxIT/
    SmartScaleConnect, which documents a real Mi Body Composition Scale 2
    capture (see docs/api-findings.md). Family members other than the
    account holder are not modelled. `before` walks backward through
    history via the `toTime` cursor this endpoint actually takes; unlike
    band_data / workout_history, it has no from/to range parameter.
    """
    params: dict[str, Any] = {"limit": limit}
    if before:
        cutoff = dt.datetime.strptime(before, "%Y-%m-%d") + dt.timedelta(days=1)
        params["toTime"] = int(cutoff.timestamp())
    user_id = client.credential().user_id
    return client.get(f"/users/{user_id}/members/-1/weightRecords", params)


def parse_weight_items(payload: Any) -> list[dict[str, Any]]:
    """weightRecords wraps results in `items`, like the v2 events family."""
    items = payload.get("items") if isinstance(payload, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def parse_lactate_threshold_events(payload: Any) -> list[dict[str, Any]]:
    """Flatten the LactateThreshold events payload into one row per estimate.

    Shape: {"items": [{"value": {"samples": [{"dateString", "lactate
    ThresholdHr", "lactateThresholdPace"}, ...]}}, ...]}. One item can carry
    more than one sample; both levels are walked.
    """
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []

    out: list[dict[str, Any]] = []
    for item in items:
        value = item.get("value") if isinstance(item, dict) else None
        samples = value.get("samples") if isinstance(value, dict) else None
        if not isinstance(samples, list):
            continue
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            hr = sample.get("lactateThresholdHr")
            if hr is None:
                continue
            out.append({
                "date": sample.get("dateString"),
                "lactate_threshold_hr_bpm": hr,
                "lactate_threshold_pace_sec_per_km":
                    sample.get("lactateThresholdPace"),
            })
    return out


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
