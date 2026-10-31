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
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx

from .auth import Credential, LoginError, clear_cached, load_cached, login, save_cached


@dataclass
class Outcome:
    """A response plus an explicit verdict about what it means."""
    status: str          # "ok" | "no_data" | "error"
    http_status: int
    data: Any = None
    note: str | None = None
    # For errors: a machine-readable class (see _ERROR_KINDS). None otherwise.
    error_kind: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status,
                               "http_status": self.http_status}
        if self.error_kind:
            out["error_kind"] = self.error_kind
        if self.note:
            out["note"] = self.note
        if self.data is not None:
            out["data"] = self.data
        return out


# What an HTTP error status means on THIS API, as established by probing
# with controls (docs/api-findings.md). These are interpretations for the
# caller, not certainties: a 404 says the route is wrong, not that the
# feature does not exist anywhere.
_ERROR_KINDS: dict[int, tuple[str, str]] = {
    400: ("bad_request",
          "Zepp rejected the parameters. On WatchSportStatistics a 400 also "
          "means the statistic name is unknown (a fabricated name returns the "
          "same 400)."),
    401: ("unauthorized", "The app token was rejected."),
    403: ("forbidden", "The account is not permitted to read this route."),
    404: ("not_found",
          "No such route. This says the path is wrong, not that the data "
          "does not exist."),
    429: ("rate_limited",
          "Zepp is rate limiting this client. Not retried automatically -- "
          "wait before asking again."),
}


def _classify(status: int) -> tuple[str, str]:
    if status in _ERROR_KINDS:
        return _ERROR_KINDS[status]
    if status >= 500:
        return ("server_error",
                "Zepp failed server-side. On WatchSportStatistics a 500 means "
                "the name IS recognised but the request failed.")
    return ("http_error", "Unexpected HTTP status.")


@dataclass
class Collected:
    """The result of a multi-request fetch: pages walked, items gathered, and
    an honest account of anything that did not come back."""
    status: str                              # "ok" | "no_data" | "partial" | "error"
    items: list[Any] = field(default_factory=list)
    requests: int = 0
    notes: list[str] = field(default_factory=list)
    error: Outcome | None = None

    def coverage(self) -> dict[str, Any]:
        out: dict[str, Any] = {"requests": self.requests,
                               "items": len(self.items)}
        if self.notes:
            out["notes"] = self.notes
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


# In-memory response cache. Process lifetime only -- nothing touches disk.
# Ranges that reach today (or are open-ended) are still accumulating on the
# watch, so they expire quickly; anything strictly in the past cannot change
# and is held until the process exits or the LRU evicts it.
_CACHE_TTL_CURRENT = 300.0
_CACHE_MAX_ENTRIES = 256


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _range_end(params: dict[str, Any]) -> dt.date | None:
    """The last calendar day a request's parameters cover, if they say."""
    for key in ("to_date", "to"):
        value = params.get(key)
        if value is None:
            continue
        text = str(value)
        try:
            if text.isdigit() and len(text) >= 12:
                return dt.datetime.fromtimestamp(int(text) / 1000, dt.UTC).date()
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _cache_ttl(params: dict[str, Any]) -> float:
    end = _range_end(params)
    # A day either side of "today" because the range end is a UTC date and
    # the watch's local day can be ahead or behind it.
    if end is None or end >= dt.date.today() - dt.timedelta(days=1):
        return _CACHE_TTL_CURRENT
    return float("inf")


class ZeppClient:
    def __init__(self, email: str, password: str, country: str = "US",
                 http: httpx.Client | None = None,
                 min_interval: float | None = None,
                 cache: bool | None = None) -> None:
        self._email = email
        self._password = password
        self._country = country
        self._client = http or httpx.Client(timeout=30)
        self._credential: Credential | None = None
        # Conservative pacing between outbound requests. Zepp's limits are
        # undocumented; one call per fetch page is the norm, and a multi-year
        # query walks many pages, so the gap is enforced here once rather
        # than trusted to every caller.
        self._min_interval = (min_interval if min_interval is not None
                              else _env_float("ZEPP_MIN_REQUEST_INTERVAL", 0.35))
        self._last_request = 0.0
        self._lock = threading.Lock()
        enabled = cache if cache is not None else (
            os.environ.get("ZEPP_RESPONSE_CACHE", "").lower()
            not in ("0", "off", "none"))
        self._cache: OrderedDict[tuple[Any, ...], tuple[float, Outcome]] | None = (
            OrderedDict() if enabled else None)
        self.requests_sent = 0

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
    def get(self, path: str, params: dict[str, Any] | None = None,
            use_cache: bool = True) -> Outcome:
        params = params or {}
        key = (path, tuple(sorted((k, str(v)) for k, v in params.items())))
        cached = self._cache_get(key) if use_cache else None
        if cached is not None:
            return cached

        credential = self.credential()
        outcome = self._request(credential, path, params)
        if outcome.http_status == 401:
            try:
                credential = self._reauthenticate()
            except LoginError as exc:
                return Outcome("error", 401, note=str(exc),
                               error_kind="unauthorized")
            outcome = self._request(credential, path, params)

        # Errors are never cached: a transient 500 must not stick.
        if outcome.status in ("ok", "no_data"):
            self._cache_put(key, outcome, _cache_ttl(params))
        return outcome

    def _cache_get(self, key: tuple[Any, ...]) -> Outcome | None:
        if self._cache is None:
            return None
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires, outcome = entry
            if time.monotonic() >= expires:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return outcome

    def _cache_put(self, key: tuple[Any, ...], outcome: Outcome, ttl: float) -> None:
        if self._cache is None:
            return
        with self._lock:
            self._cache[key] = (time.monotonic() + ttl, outcome)
            self._cache.move_to_end(key)
            while len(self._cache) > _CACHE_MAX_ENTRIES:
                self._cache.popitem(last=False)

    def clear_cache(self) -> None:
        if self._cache is not None:
            with self._lock:
                self._cache.clear()

    def _pace(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            wait = self._last_request + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _request(self, credential: Credential, path: str,
                 params: dict[str, Any] | None) -> Outcome:
        url = f"https://{credential.region_host}{path}"
        self._pace()
        self.requests_sent += 1
        try:
            response = self._client.get(
                url, headers={"apptoken": credential.app_token},
                params=params or {},
            )
        except httpx.HTTPError as exc:
            return Outcome("error", 0, note=f"network error: {exc}",
                           error_kind="network")

        # The user id appears in some paths; it is not echoed back to the
        # model, which may restate it in a transcript.
        uid = credential.user_id
        shown = path.replace(uid, "{uid}") if uid else path
        if response.status_code >= 400:
            kind, meaning = _classify(response.status_code)
            # The body is echoed (truncated) because Zepp's error bodies carry
            # the route and a code, never the token; the request line with the
            # user id is NOT echoed.
            return Outcome(
                "error", response.status_code, error_kind=kind,
                note=f"Zepp returned HTTP {response.status_code} for {shown}. "
                     f"{meaning} Body starts: "
                     f"{response.text[:200].replace(uid or chr(0), '{uid}')!r}",
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


# --------------------------------------------------------------------------
# Multi-request fetches: date-range chunking and cursor pagination
# --------------------------------------------------------------------------

# Guards against walking an unbounded history on one tool call. A request
# that reaches the cap returns what it gathered, marked partial.
MAX_PAGES = 40
BAND_DATA_CHUNK_DAYS = 31


def _day_bounds_ms(from_date: str, to_date: str) -> tuple[int, int]:
    """Epoch-ms bounds for an inclusive local-date range.

    Uses the process's local timezone for the day boundaries, the same way
    the Zepp app asks for "a day" -- and pads by a day each side, because the
    event families key items by UTC midnight while the user means local days.
    Callers filter decoded records back to the requested dates.
    """
    start = dt.datetime.combine(dt.date.fromisoformat(from_date), dt.time.min)
    end = dt.datetime.combine(dt.date.fromisoformat(to_date), dt.time.min)
    start -= dt.timedelta(days=1)
    end += dt.timedelta(days=2)
    return (int(start.astimezone(dt.UTC).timestamp() * 1000),
            int(end.astimezone(dt.UTC).timestamp() * 1000))


def _walk_events(client: ZeppClient, path: str, base: dict[str, Any],
                 from_ms: int, to_ms: int, limit: int) -> Collected:
    """Page through an events route by advancing `from` past the last item.

    Neither events family documents a cursor. Items come back in ascending
    timestamp order capped at `limit` (a 30-day Body Charge request returned
    exactly 20 days, oldest first), so a full page means "there may be more"
    and the next request starts one millisecond after the last item seen.
    Items are de-duplicated on (eventType, subType, timestamp) in case the
    server treats `from` as inclusive.
    """
    from . import events as ev

    collected = Collected(status="no_data")
    cursor = from_ms
    while collected.requests < MAX_PAGES:
        outcome = client.get(path, {**base, "from": str(cursor), "to": str(to_ms),
                                    "limit": limit})
        collected.requests += 1
        if outcome.status == "error":
            collected.error = outcome
            collected.status = "partial" if collected.items else "error"
            if collected.items:
                collected.notes.append(
                    f"A later page failed (HTTP {outcome.http_status}); the "
                    "items before it are returned.")
            return collected
        page = ev._items(outcome.data)
        if not page:
            break
        collected.items.extend(page)
        stamps = [ev._number(i.get("timestamp")) for i in page]
        last = max((t for t in stamps if t is not None), default=None)
        if len(page) < limit or last is None or last + 1 <= cursor:
            break
        cursor = int(last) + 1
    else:
        collected.notes.append(
            f"Stopped after {MAX_PAGES} pages; narrow the date range for the rest.")
        collected.status = "partial"

    collected.items = ev.dedupe(collected.items)
    if collected.status != "partial":
        collected.status = "ok" if collected.items else "no_data"
    return collected


def events_v2(client: ZeppClient, event_type: str, sub_type: str,
              from_date: str, to_date: str, limit: int = 20) -> Collected:
    """`/v2/users/me/events` -- readiness, HRV, respiratory rate, Body Charge,
    DailyHealth, LactateThreshold."""
    start, end = _day_bounds_ms(from_date, to_date)
    return _walk_events(client, "/v2/users/me/events",
                        {"eventType": event_type, "subType": sub_type},
                        start, end, limit)


def events_v1(client: ZeppClient, event_type: str, from_date: str, to_date: str,
              sub_type: str | None = None, limit: int = 20) -> Collected:
    """`/users/{uid}/events` -- all-day stress, PAI, SpO2 spot checks, single
    stress. `reverse=false` gives ascending order, which paging relies on."""
    start, end = _day_bounds_ms(from_date, to_date)
    base: dict[str, Any] = {"eventType": event_type, "reverse": "false"}
    if sub_type:
        base["subType"] = sub_type
    return _walk_events(client, f"/users/{client.credential().user_id}/events",
                        base, start, end, limit)


def events_by_date(client: ZeppClient, event_type: str, sub_type: str,
                   from_date: str, to_date: str, timezone: str,
                   limit: int = 31) -> Collected:
    """`/users/{uid}/events/dateString` -- keyed by local date strings rather
    than epochs (overnight SpO2 / ODI). Chunked by `limit` days, since this
    route takes no cursor."""
    from . import events as ev

    collected = Collected(status="no_data")
    day = dt.date.fromisoformat(from_date)
    last = dt.date.fromisoformat(to_date)
    path = f"/users/{client.credential().user_id}/events/dateString"
    while day <= last and collected.requests < MAX_PAGES:
        chunk_end = min(last, day + dt.timedelta(days=limit - 1))
        outcome = client.get(path, {
            "eventType": event_type, "subType": sub_type,
            "from": day.isoformat(), "to": chunk_end.isoformat(),
            "timeZone": timezone, "limit": limit,
        })
        collected.requests += 1
        if outcome.status == "error":
            collected.error = outcome
            collected.notes.append(
                f"{day}..{chunk_end} failed (HTTP {outcome.http_status}).")
        else:
            collected.items.extend(ev._items(outcome.data))
        day = chunk_end + dt.timedelta(days=1)
    collected.items = ev.dedupe(collected.items)
    if collected.error and not collected.items:
        collected.status = "error"
    elif collected.error or day <= last:
        collected.status = "partial"
    else:
        collected.status = "ok" if collected.items else "no_data"
    return collected


def band_data_range(client: ZeppClient, from_date: str, to_date: str) -> Collected:
    """band_data over an arbitrary range, in 31-day chunks.

    The recon spike only ever asked for a week at a time; nothing documents
    how large a single request may be, and a multi-year range in one call is
    both a large response and an unknown. Chunks are de-duplicated on
    `date_time` and returned oldest first. A failed chunk does not discard
    the others -- the result is marked partial and names the gap.
    """
    collected = Collected(status="no_data")
    day = dt.date.fromisoformat(from_date)
    last = dt.date.fromisoformat(to_date)
    seen: set[str] = set()
    while day <= last and collected.requests < MAX_PAGES:
        chunk_end = min(last, day + dt.timedelta(days=BAND_DATA_CHUNK_DAYS - 1))
        outcome = band_data(client, day.isoformat(), chunk_end.isoformat())
        collected.requests += 1
        if outcome.status == "error":
            collected.error = outcome
            collected.notes.append(
                f"{day}..{chunk_end} failed (HTTP {outcome.http_status}); "
                "days in that span are missing, not empty.")
        elif outcome.status == "ok":
            for row in outcome.data.get("data") or []:
                if isinstance(row, dict) and row.get("date_time") not in seen:
                    seen.add(row.get("date_time"))
                    collected.items.append(row)
        day = chunk_end + dt.timedelta(days=1)
    collected.items.sort(key=lambda r: r.get("date_time") or "")
    if day <= last:
        collected.notes.append(
            f"Stopped at {MAX_PAGES} requests; {day}..{last} not fetched.")
    if collected.error and not collected.items:
        collected.status = "error"
    elif collected.error or day <= last:
        collected.status = "partial"
    else:
        collected.status = "ok" if collected.items else "no_data"
    return collected


# The history route returns `data.next`, an epoch-seconds trackid to resume
# from, or -1 when exhausted. Every capture in the corpus fit on one page
# (next == -1), so the resume parameter is taken from prior art -- see
# docs/api-findings.md -- and guarded: a page that adds no new trackid stops
# the walk instead of looping.
HISTORY_CURSOR_PARAM = "trackid"


def workout_history_all(client: ZeppClient, from_date: str, to_date: str) -> Collected:
    """Every workout row in the range, following `data.next`."""
    collected = Collected(status="no_data")
    seen: set[str] = set()
    params: dict[str, Any] = {}
    while collected.requests < MAX_PAGES:
        outcome = client.get("/v1/sport/run/history.json", {
            "from_date": from_date, "to_date": to_date,
            "userid": client.credential().user_id, **params,
        })
        collected.requests += 1
        if outcome.status == "error":
            collected.error = outcome
            if collected.items:
                collected.notes.append(
                    f"A later page failed (HTTP {outcome.http_status}); older "
                    "workouts in the range may be missing.")
            break
        if outcome.status == "no_data":
            break
        added = 0
        for row in parse_rows(outcome.data):
            key = str(row.get("trackid"))
            if key not in seen:
                seen.add(key)
                collected.items.append(row)
                added += 1
        data = outcome.data.get("data") if isinstance(outcome.data, dict) else None
        cursor = data.get("next") if isinstance(data, dict) else None
        try:
            cursor_value = int(cursor) if cursor is not None else -1
        except (TypeError, ValueError):
            cursor_value = -1
        if cursor_value <= 0:
            break
        if not added:
            collected.notes.append(
                "Zepp reported more pages but the next page repeated rows "
                "already seen; stopped. Older workouts may be missing.")
            break
        params = {HISTORY_CURSOR_PARAM: str(cursor_value)}
    else:
        collected.notes.append(f"Stopped after {MAX_PAGES} pages.")

    if collected.error and not collected.items:
        collected.status = "error"
    elif collected.notes:
        collected.status = "partial"
    else:
        collected.status = "ok" if collected.items else "no_data"
    return collected


def watch_sport_statistics(client: ZeppClient, name: str, from_date: str,
                           to_date: str) -> Outcome:
    """`WatchSportStatistics/{SPORT_LOAD,VO2_MAX}` with the parameters four
    independent open-source clients send (startDay/endDay as dates, limit,
    isReverse). The recon spike called these routes with NO parameters and
    got HTTP 500 -- which, against a 400 for a fabricated name, already
    showed the names are real. Whether these parameters fix the 500 is
    unverified on this project's account (verification level 3)."""
    return client.get(
        f"/v2/watch/users/{client.credential().user_id}/WatchSportStatistics/{name}",
        {"startDay": from_date, "endDay": to_date, "limit": 900, "isReverse": "true"})


def devices(client: ZeppClient) -> Outcome:
    """Paired devices, from the route other clients use. Items carry
    `additionalInfo.auth_key` -- the Bluetooth pairing secret -- so callers
    must only ever read fields through `body.device_summary`'s allowlist."""
    return client.get(f"/users/{client.credential().user_id}/devices",
                      {"enableMultiDevice": "true", "device_type": "android_phone"})


def profile(client: ZeppClient) -> Outcome:
    """`huami.health.getUserInfo.json` -- height, weight, birth month, gender,
    wear hand. Verified in the recon spike (fixture 015), which sent no query
    parameters at all -- the app token identifies the user."""
    return client.get("/huami.health.getUserInfo.json")


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
