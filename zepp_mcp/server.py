"""MCP server exposing Zepp/Amazfit data.

Nothing here writes health data to disk. The only thing cached is the API
token, so a restart does not trigger a fresh login.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import Any, Literal

from mcp.server import MCPServer

from . import body, decode, workouts
from . import client as api
from .auth import describe_expiry
from .codes import SPORT_CODES

_ROOT = Path(__file__).resolve().parent.parent
api.load_dotenv(_ROOT / ".env")

# httpx logs every request line at INFO, including the full query string with
# the user id in it. Quiet by default; ZEPP_DEBUG=1 brings it back.
if os.environ.get("ZEPP_DEBUG", "").lower() not in ("1", "true", "yes"):
    logging.getLogger("httpx").setLevel(logging.WARNING)

server = MCPServer(
    name="zepp",
    version="0.1.0",
    instructions=(
        "Reads health and workout data from a Zepp/Amazfit account.\n\n"
        "Two rules when reporting results:\n"
        "1. A `no_data` status means Zepp returned an empty success response. "
        "That is NOT proof the user has no data for that period -- the API "
        "returns the same empty response for malformed requests. Say the "
        "query came back empty, not that the activity did not happen.\n"
        "2. Fields marked `unit_verified: false` have an unconfirmed unit. "
        "Report the number, name the field, and do not attach a unit to it."
    ),
)

_client: api.ZeppClient | None = None


def _get_client() -> api.ZeppClient:
    global _client
    if _client is None:
        _client = api.from_env()
    return _client


def _fail(exc: Exception) -> dict[str, Any]:
    return {"status": "error", "error": str(exc)}


def _range(from_date: str | None, to_date: str | None,
           days: int = 7) -> tuple[str, str]:
    if from_date and to_date:
        return from_date, to_date
    start, end = api.default_range(days)
    return from_date or start, to_date or end


@server.tool(
    description="Daily steps, distance, calories and sleep for a date range. "
                "Sleep is broken into light/deep/REM/awake minutes."
)
def zepp_daily_summary(
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """Args: from_date/to_date as YYYY-MM-DD. Defaults to the last 7 days."""
    try:
        start, end = _range(from_date, to_date)
        outcome = api.band_data(_get_client(), start, end)
    except Exception as exc:
        return _fail(exc)

    if outcome.status != "ok":
        return outcome.as_dict()

    rows = outcome.data.get("data") or []
    days = [decode.summarise_day(r) for r in rows if isinstance(r, dict)]
    return {"status": "ok", "range": {"from": start, "to": end},
            "day_count": len(days), "days": days}


@server.tool(
    description="Per-minute heart rate for a single day, with a summary. "
                "Minutes with no reading are returned as null."
)
def zepp_heart_rate(
    date: str | None = None,
    include_series: bool = False,
) -> dict[str, Any]:
    """Args: date as YYYY-MM-DD (default today). Set include_series for all
    1440 per-minute values; otherwise only statistics are returned."""
    try:
        day = date or dt.date.today().isoformat()
        outcome = api.band_data(_get_client(), day, day)
    except Exception as exc:
        return _fail(exc)

    if outcome.status != "ok":
        return outcome.as_dict()

    rows = [r for r in (outcome.data.get("data") or []) if isinstance(r, dict)]
    if not rows:
        return {"status": "no_data", "date": day,
                "note": "No band_data row returned for this date."}

    minutes = decode.decode_hr_minutes(rows[0].get("data_hr") or "")
    present = [(i, v) for i, v in enumerate(minutes) if v is not None]
    if not present:
        return {"status": "no_data", "date": day,
                "note": "The day's row carried no usable heart rate samples."}

    values = [v for _, v in present]
    result: dict[str, Any] = {
        "status": "ok", "date": day, "unit": "bpm",
        "samples": len(values), "minutes_covered": len(values),
        "min": min(values), "max": max(values),
        "avg": round(sum(values) / len(values), 1),
        "first_reading_minute": present[0][0],
        "last_reading_minute": present[-1][0],
    }
    if include_series:
        result["per_minute"] = minutes
    return result


@server.tool(
    description="Sleep breakdown for a single night: light, deep, REM and "
                "awake minutes, sleep score and resting heart rate."
)
def zepp_sleep(date: str | None = None) -> dict[str, Any]:
    """Args: date as YYYY-MM-DD, the morning the sleep ended."""
    try:
        day = date or dt.date.today().isoformat()
        outcome = api.band_data(_get_client(), day, day)
    except Exception as exc:
        return _fail(exc)

    if outcome.status != "ok":
        return outcome.as_dict()

    rows = [r for r in (outcome.data.get("data") or []) if isinstance(r, dict)]
    for row in rows:
        summary = decode.summarise_day(row)
        sleep = summary.get("sleep")
        if not sleep:
            continue
        # A night the watch missed still produces a sleep block, just an
        # empty one. Report that as no_data so it is never read as a night
        # of zero sleep with a resting heart rate of zero.
        status = "ok" if sleep.get("main_sleep_recorded", True) else "no_data"
        return {"status": status, "date": summary["date"], **sleep}
    return {"status": "no_data", "date": day,
            "note": "No sleep block in this date's record."}


@server.tool(
    description="List workouts across all sports, with sport-specific "
                "metrics (SWOLF and stroke counts for swims, pace and "
                "cadence for runs, set counts for strength work)."
)
def zepp_list_workouts(
    from_date: str | None = None,
    to_date: str | None = None,
    sport: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Args: from_date/to_date as YYYY-MM-DD (default last 90 days), an
    optional sport name to filter by, and a result limit."""
    try:
        start, end = _range(from_date, to_date, days=90)
        outcome = api.workout_history(_get_client(), start, end)
    except Exception as exc:
        return _fail(exc)

    if outcome.status != "ok":
        return outcome.as_dict()

    rows = api.parse_rows(outcome.data)
    items = [workouts.normalise(r) for r in rows]
    if sport:
        wanted = sport.strip().lower()
        items = [i for i in items if wanted in i["sport"].lower()]
    items.sort(key=lambda i: i.get("start_local") or "", reverse=True)

    return {
        "status": "ok", "range": {"from": start, "to": end},
        "total": len(items), "workouts": items[:limit],
        "sports_present": sorted({i["sport"] for i in items}),
    }


@server.tool(
    description="Full detail for one workout: laps, time-series streams and "
                "GPS. Get the track_id and source from zepp_list_workouts."
)
def zepp_workout_detail(
    track_id: str,
    source: str,
    include: list[Literal["summary", "laps", "streams", "gps"]] | None = None,
) -> dict[str, Any]:
    """Args: track_id and source, both from a zepp_list_workouts result.
    `include` selects which sections to decode; default is summary and laps,
    because the stream sections can be very large."""
    try:
        outcome = api.workout_detail(_get_client(), track_id, source)
    except Exception as exc:
        return _fail(exc)

    if outcome.status != "ok":
        return outcome.as_dict()

    sections = set(include or ["summary", "laps"])
    data = outcome.data.get("data") if isinstance(outcome.data, dict) else None
    if not isinstance(data, dict):
        return {"status": "error", "note": "Detail payload had no data object."}

    result: dict[str, Any] = {"status": "ok", "track_id": track_id}
    result.update(decode.decode_detail(data, sections))
    return result


@server.tool(
    description="Body composition scale readings: weight, height, BMI, and "
                "-- when synced from a real bio-impedance scale rather than "
                "a manual entry -- body fat %, body water %, muscle, bone "
                "mass, visceral fat rating and basal metabolism (BMR)."
)
def zepp_body_composition(
    limit: int = 20,
    before: str | None = None,
) -> dict[str, Any]:
    """Args: limit caps how many readings to return, most recent first.
    `before` (YYYY-MM-DD) pages backward from that date; default is now.

    Only weight/height/bmi are independently verified. Everything under
    `body_composition` uses a schema sourced from a different open-source
    Zepp API client's documented capture, not from this project's own
    verified data -- see zepp_describe_schema known_gaps. `bmi_consistent`
    is False when a record's own weight/height/bmi do not reconcile, which
    happens for manual/HealthKit-linked entries as opposed to a real scale
    sync; treat body_composition on such a record with extra caution."""
    try:
        outcome = api.weight_records(_get_client(), limit=limit, before=before)
    except Exception as exc:
        return _fail(exc)

    if outcome.status != "ok":
        return outcome.as_dict()

    items = api.parse_weight_items(outcome.data)
    readings = [body.normalise(item) for item in items]
    readings.sort(key=lambda r: r.get("date") or "", reverse=True)
    return {"status": "ok", "reading_count": len(readings),
            "readings": readings[:limit]}


def _lactate_threshold_estimates(client: api.ZeppClient, start: str,
                                 end: str) -> tuple[list[dict[str, Any]], str]:
    """Estimate history plus which source produced it.

    The events endpoint is authoritative and carries its own date per
    estimate. It can fail independently of the workout-history call (a
    different route, a different response shape), so this falls back to
    deriving estimates from workout rows -- the same thing the tool did
    before this endpoint was found -- rather than returning nothing.
    """
    outcome = api.lactate_threshold_history(client, start, end)
    if outcome.status == "ok":
        events = api.parse_lactate_threshold_events(outcome.data)
        if events:
            for entry in events:
                pace = entry.get("lactate_threshold_pace_sec_per_km")
                if pace:
                    entry["lactate_threshold_pace"] = (
                        f"{int(pace) // 60}:{int(pace) % 60:02d}/km")
            events.sort(key=lambda e: e.get("date") or "")
            return events, "lactate_threshold_events"

    history = api.workout_history(client, start, end)
    if history.status != "ok":
        return [], "workout_rows"
    estimates = []
    for row in api.parse_rows(history.data):
        item = workouts.normalise(row)
        foot = item.get("foot") or {}
        lthr = foot.get("lactate_threshold_hr_bpm")
        if lthr is None:
            continue
        entry = {
            "date": (item["start_local"] or "")[:10],
            "track_id": item["track_id"],
            "lactate_threshold_hr_bpm": lthr,
            "lactate_threshold_pace_sec_per_km":
                foot.get("lactate_threshold_pace_sec_per_km"),
        }
        pace = entry["lactate_threshold_pace_sec_per_km"]
        if pace:
            entry["lactate_threshold_pace"] = f"{int(pace) // 60}:{int(pace) % 60:02d}/km"
        estimates.append(entry)
    estimates.sort(key=lambda e: e["date"])
    return estimates, "workout_rows"


@server.tool(
    description="Lactate threshold heart rate and pace, with how they have "
                "changed over time, plus the current VO2 max estimate. "
                "LTHR anchors every training zone, so it matters more than "
                "any single session."
)
def zepp_training_thresholds(
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """Args: from_date/to_date as YYYY-MM-DD. Defaults to the last 180 days.

    The watch estimates lactate threshold from qualifying runs only. Runs
    where it did not produce an estimate carry no threshold fields at all,
    and are counted separately rather than as a zero."""
    client = _get_client()
    try:
        start, end = _range(from_date, to_date, days=180)
        estimates, source = _lactate_threshold_estimates(client, start, end)
        vo2_max, without = None, 0
        history = api.workout_history(client, start, end)
        if history.status == "ok":
            runs = [workouts.normalise(r) for r in api.parse_rows(history.data)]
            runs = [r for r in runs if r["sport"] == "outdoor_running"]
            runs.sort(key=lambda r: r.get("start_local") or "")
            for run in runs:
                vo2 = run["summary"].get("VO2_max")
                if vo2:
                    vo2_max = vo2  # last one wins -- most recent estimate
                if run.get("foot", {}).get("lactate_threshold_hr_bpm") is None:
                    without += 1
    except Exception as exc:
        return _fail(exc)

    if not estimates:
        return {
            "status": "no_data",
            "range": {"from": start, "to": end},
            "source": source,
            "runs_without_an_estimate": without,
            "note": "No lactate threshold estimate in this range, from "
                    "either the event log or workout rows. The watch only "
                    "produces one from qualifying runs -- typically a "
                    "sustained hard effort. This is absence of an estimate, "
                    "not a threshold of zero.",
        }

    latest = estimates[-1]
    current = dict(latest)
    if vo2_max:
        # VO2_max is a rolling athlete-level estimate, not per-session: it
        # read the same value (49) across a 20-minute and a 65-minute run.
        # Attached to `current`, not to each history entry, for that reason.
        current["vo2_max"] = vo2_max
    result: dict[str, Any] = {
        "status": "ok",
        "range": {"from": start, "to": end},
        "source": source,
        "current": current,
        "estimate_count": len(estimates),
        "runs_without_an_estimate": without,
        "history": estimates,
        "training_load": {
            "note": "Only a per-session exercise_load is available (see "
                    "zepp_list_workouts summary.exercise_load). The rolling "
                    "cumulative training-load figure is not: the dedicated "
                    "WatchSportStatistics/SPORT_LOAD endpoint returns HTTP "
                    "500 server-side.",
        },
        "note": "Zone boundaries in zepp_list_workouts are the watch's own "
                "personalised ones. They are reported as measured and are "
                "not recomputed from this threshold.",
    }
    if len(estimates) > 1:
        first = estimates[0]
        result["change"] = {
            "hr_bpm": latest["lactate_threshold_hr_bpm"]
                      - first["lactate_threshold_hr_bpm"],
            "from_date": first["date"],
            "to_date": latest["date"],
        }
    return result


@server.tool(
    description="Explain what this server knows: which sport codes are "
                "identified, which stream units are verified, and where the "
                "decoding is still uncertain."
)
def zepp_describe_schema() -> dict[str, Any]:
    return {
        "sport_codes": {
            "identified": SPORT_CODES,
            "note": "Every sport code seen on this account is identified and "
                    "confirmed against the Zepp app. Codes absent from this "
                    "map are reported as unknown_sport_<code>: they are real "
                    "activities, only the name is unknown.",
        },
        "stream_units": {
            name: {"unit": spec.unit, "encoding": spec.encoding,
                   "verified": spec.verified}
            for name, spec in decode.STREAM_SPECS.items()
        },
        "known_gaps": [
            "RTPC (avg_rtpc_unverified and friends) is present on every "
            "sport and reads a constant 21 outside running, which is "
            "evidence it is a sentinel -- but its actual meaning is "
            "unconfirmed. Report the number, not what it measures.",
            "Lap columns are recovered by inference, not documentation. "
            "Columns 1, 13 and 14 (duration, strokes, SWOLF) ARE confirmed "
            "-- SWOLF is self-checking as duration + strokes, matches the "
            "published SWOLF definition, and its components reconstruct "
            "the workout summary's own swolf figure. Everything outside "
            "that set is inferred or raw.",
            "pool_swim_pace has an unconfirmed unit.",
            "Cumulative training load is unavailable -- "
            "WatchSportStatistics/SPORT_LOAD returns HTTP 500. Per-session "
            "exercise_load and VO2 max (from zepp_training_thresholds) are "
            "both available; only the rolling cumulative figure is not.",
            "No dedicated Zepp Coach endpoint exists, across 22 probed "
            "routes with controls. Plan progress (dailyScore, "
            "dailyPlanFinished, running_program_id) IS available, exposed "
            "as training_plan in zepp_list_workouts once a plan is active.",
            "zepp_body_composition: weight_kg/height_cm/bmi are verified "
            "(bmi reproduces weight / (height/100)**2). Everything under "
            "body_composition (fat/water/muscle %, bone mass, visceral "
            "fat, BMR, body score) uses a schema sourced from a different "
            "open-source Zepp API client's documented real-scale capture "
            "(AlexxIT/SmartScaleConnect), not from an account this project "
            "holds -- see docs/api-findings.md. bmi_consistent flags "
            "records (typically manual/HealthKit entries, not a real "
            "scale sync) whose own weight/height/bmi do not reconcile.",
            "An empty 200 cannot always be distinguished from a rejected "
            "request. This does not apply to the v2 events family "
            "(readiness, HRV, DailyHealth, RespiratoryRate, "
            "LactateThreshold) any more than to v1 endpoints -- an earlier "
            "version of this classifier reported every v2 response empty "
            "regardless of content, which has been fixed.",
        ],
    }


@server.tool(
    description="Send an arbitrary GET to the Zepp API. Use this to reach "
                "endpoints this server does not model yet."
)
def zepp_raw_request(
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Args: path such as /v1/data/band_data.json, plus query params. The
    user id and auth header are supplied automatically."""
    if not path.startswith("/"):
        return {"status": "error", "error": "path must start with '/'"}
    try:
        return _get_client().get(path, params or {}).as_dict()
    except Exception as exc:
        return _fail(exc)


@server.tool(description="Check the stored Zepp credential and connection.")
def zepp_auth_status() -> dict[str, Any]:
    try:
        credential = _get_client().credential()
    except Exception as exc:
        return _fail(exc)
    return {
        "status": "ok",
        "region_host": credential.region_host,
        "country_code": credential.country_code,
        "token_expires_at": describe_expiry(credential),
        "note": "Only the API token is cached on disk. No health data is "
                "stored by this server.",
    }


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
