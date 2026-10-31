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

from . import analysis, body, decode, events, metrics, sports, workouts
from . import client as api
from .auth import describe_expiry

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
        "2. Fields marked `unit_verified: false`, or whose name ends in "
        "`_unverified`, have an unconfirmed unit or meaning. Report the "
        "number, name the field, and do not attach a unit to it.\n"
        "3. Values whose `method` says `derived` were computed by this server "
        "from Zepp's numbers (aggregate pace, weighted averages, trends). "
        "Say so when it matters; they are not readings from the watch.\n"
        "4. A `partial` status means some requests failed or a page cap was "
        "hit; `coverage.notes` names what is missing. Do not treat missing "
        "spans as zero.\n\n"
        "Start with zepp_period_summary for broad questions ('how have the "
        "last 30 days gone'), zepp_workout_stats for training volume, "
        "records and per-workout trends, and zepp_health_metrics for "
        "readiness, HRV, resting HR, stress, SpO2, respiratory rate, PAI and "
        "Body Charge."
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


class _BadInput(ValueError):
    """A caller error worth explaining, never a server fault."""


def _range(from_date: str | None, to_date: str | None,
           days: int = 7) -> tuple[str, str]:
    if from_date and not to_date:
        to_date = dt.date.today().isoformat()
    if to_date and not from_date:
        from_date = (dt.date.fromisoformat(to_date)
                     - dt.timedelta(days=days)).isoformat()
    if not from_date or not to_date:
        from_date, to_date = api.default_range(days)
    try:
        start = dt.date.fromisoformat(from_date)
        end = dt.date.fromisoformat(to_date)
    except ValueError:
        raise _BadInput(
            f"Dates must be YYYY-MM-DD; got from_date={from_date!r}, "
            f"to_date={to_date!r}.") from None
    if start > end:
        raise _BadInput(f"from_date {from_date} is after to_date {to_date}.")
    return start.isoformat(), end.isoformat()


def _user_tz() -> dt.tzinfo:
    """The account holder's timezone for data that does not carry one.

    v1 events (stress, PAI) carry no zone of their own. ZEPP_TIMEZONE (an
    IANA name) wins; otherwise the machine's local zone, since this server
    runs on the user's own machine.
    """
    name = os.environ.get("ZEPP_TIMEZONE")
    if name:
        resolved = events.zone(name, dt.UTC)
        if resolved is not dt.UTC or name.upper() == "UTC":
            return resolved
    return dt.datetime.now().astimezone().tzinfo or dt.UTC


def _tz_name(tzinfo: dt.tzinfo) -> str:
    key = getattr(tzinfo, "key", None)
    return key or dt.datetime.now(tzinfo).strftime("%z") or "UTC"


def _in_range(records: list[dict[str, Any]], start: str, end: str,
              key: str = "date") -> list[dict[str, Any]]:
    return [r for r in records if start <= str(r.get(key) or "")[:10] <= end]


def _collected_status(collected: api.Collected) -> dict[str, Any]:
    """Status, coverage and (if any) the error for a multi-request fetch."""
    out: dict[str, Any] = {"status": collected.status,
                           "coverage": collected.coverage()}
    if collected.error is not None:
        out["error"] = {k: v for k, v in collected.error.as_dict().items()
                        if k != "data"}
    if collected.status == "no_data":
        out["note"] = ("Zepp returned no records for this range. That is not "
                       "proof nothing happened -- the API answers malformed "
                       "or unsupported requests the same way.")
    return out


@server.tool(
    description="Daily steps, distance, active calories, activity periods "
                "and sleep for any date range, optionally with hourly steps. "
                "Sleep is broken into light/deep/REM/awake minutes."
)
def zepp_daily_summary(
    from_date: str | None = None,
    to_date: str | None = None,
    include_hourly: bool = False,
) -> dict[str, Any]:
    """Args: from_date/to_date as YYYY-MM-DD (default the last 7 days; any
    length -- long ranges are fetched in 31-day chunks). include_hourly adds
    24 local-hour step totals per day, decoded from the per-minute record.

    Each day also carries `activity_periods`: minutes, steps and distance by
    activity mode (walking / running / light activity), with steps-per-minute
    so the mode label can be checked against the cadence."""
    try:
        start, end = _range(from_date, to_date)
        collected = api.band_data_range(_get_client(), start, end)
    except Exception as exc:
        return _fail(exc)

    if collected.status == "error":
        return _collected_status(collected)

    days = []
    for row in collected.items:
        day = decode.summarise_day(row)
        if include_hourly:
            hours = decode.hourly_steps(row.get("data") or "")
            if hours:
                day["hourly_steps"] = hours
        days.append(day)
    result = {**_collected_status(collected), "range": {"from": start, "to": end},
              "day_count": len(days), "days": days}
    if days:
        result["totals"] = {
            "steps": sum(d.get("steps") or 0 for d in days),
            "distance_km": round(
                sum(d.get("distance_metres") or 0 for d in days) / 1000, 2),
            "avg_daily_steps": round(
                sum(d.get("steps") or 0 for d in days) / len(days)),
            "method": "derived: sums and means over the days returned",
        }
    return result


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


def _night(row: dict[str, Any]) -> dict[str, Any] | None:
    summary = decode.summarise_day(row)
    sleep = summary.get("sleep")
    if not sleep:
        return None
    # A night the watch missed still produces a sleep block, just an empty
    # one. Report that as no_data so it is never read as a night of zero
    # sleep with a resting heart rate of zero.
    status = "ok" if sleep.get("main_sleep_recorded", True) else "no_data"
    return {"status": status, "date": summary["date"], **sleep}


def _enrich_nights(client: api.ZeppClient, nights: list[dict[str, Any]],
                   rows: dict[str, dict[str, Any]], start: str, end: str) -> list[str]:
    """Attach readiness, sleep-window respiratory rate and overnight SpO2.

    Each source is fetched once for the whole range and joined by date, so
    a month of nights costs a handful of requests, not three per night. A
    failed source is named in the returned notes; the nights keep whatever
    the other sources supplied.
    """
    notes: list[str] = []
    tz = _user_tz()

    readiness = api.events_v2(client, "readiness", "watch_score", start, end)
    by_date = {r["date"]: r for r in events.readiness(
        {"items": readiness.items}, tz)}
    if readiness.status in ("error", "partial"):
        notes.append("readiness: " + "; ".join(readiness.coverage().get("notes", [])
                                              or ["request failed"]))

    rr = api.events_v2(client, "RespiratoryRate", "real_data", start, end)
    breaths = events.respiratory_rate({"items": rr.items}, tz)
    if rr.status in ("error", "partial"):
        notes.append("respiratory rate: request failed or incomplete")

    odi = api.events_by_date(client, "blood_oxygen", "odi", start, end, _tz_name(tz))
    by_odi = {r["date"]: r for r in events.overnight_spo2({"items": odi.items}, tz)}
    if odi.status in ("error", "partial"):
        notes.append("overnight SpO2: request failed or incomplete")

    for night in nights:
        if night.get("status") != "ok":
            continue
        day = night["date"]
        ready = by_date.get(day)
        if ready:
            night["readiness"] = {k: v for k, v in ready.items()
                                  if k not in ("date", "computed_at")}
        slp = (decode.decode_band_summary(rows[day].get("summary") or "")
               .get("slp") or {})
        st, ed = slp.get("st"), slp.get("ed")
        if isinstance(st, int) and isinstance(ed, int) and ed > st:
            window = events.within(breaths, st, ed, "breaths_per_minute")
            if window.get("samples"):
                night["respiratory_rate_breaths_per_minute"] = {
                    **window, "method": "derived: per-minute respiratory rate "
                                        "samples inside the sleep window"}
        if day in by_odi:
            night["overnight_spo2"] = by_odi[day]
    return notes


@server.tool(
    description="Sleep for one night or a range of nights: light/deep/REM/"
                "awake minutes, sleep score, latency, time in bed, "
                "efficiency, resting HR, naps -- plus, by default, that "
                "morning's readiness (overnight HRV, readiness score), "
                "overnight respiratory rate and overnight SpO2 (ODI)."
)
def zepp_sleep(
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    enrich: bool = True,
) -> dict[str, Any]:
    """Args: date as YYYY-MM-DD, the morning the sleep ended (default
    today). Or from_date/to_date for a range of nights, which adds averages
    across the recorded nights. enrich=False skips the readiness /
    respiratory-rate / SpO2 joins (three extra requests)."""
    try:
        if from_date or to_date:
            start, end = _range(from_date, to_date)
        else:
            day = date or dt.date.today().isoformat()
            start, end = _range(day, day)
        client = _get_client()
        collected = api.band_data_range(client, start, end)
        if collected.status == "error":
            return _collected_status(collected)
        rows = {r.get("date_time"): r for r in collected.items}
        nights = [n for n in (_night(r) for r in collected.items) if n]
        notes = (_enrich_nights(client, nights, rows, start, end)
                 if enrich and nights else [])
    except _BadInput as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        return _fail(exc)

    single = not (from_date or to_date)
    if single:
        if not nights:
            return {"status": "no_data", "date": start,
                    "note": "No sleep block in this date's record."}
        night = nights[0]
        if notes:
            night["coverage_notes"] = notes
        return night

    recorded = [n for n in nights if n.get("status") == "ok"]
    result: dict[str, Any] = {
        **_collected_status(collected),
        "range": {"from": start, "to": end},
        "nights_recorded": len(recorded),
        "nights_missing": len(nights) - len(recorded),
        "nights": nights,
    }
    if notes:
        result["coverage"]["notes"] = result["coverage"].get("notes", []) + notes
    if recorded:
        result["averages"] = {
            key: analysis.mean_of(recorded, key)
            for key in ("total_asleep_minutes", "deep_minutes", "rem_minutes",
                        "light_minutes", "awake_minutes", "sleep_score",
                        "resting_heart_rate_bpm", "sleep_efficiency_percent",
                        "sleep_latency_minutes")
        }
        result["averages"]["method"] = "derived: mean over recorded nights only"
        result["totals"] = {
            "deep_minutes": sum(n.get("deep_minutes") or 0 for n in recorded),
            "rem_minutes": sum(n.get("rem_minutes") or 0 for n in recorded),
            "total_asleep_minutes": sum(n.get("total_asleep_minutes") or 0
                                        for n in recorded),
        }
    return result


def _sports_seen(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each sport code in a result, with how sure its name is -- so a
    catalogued or contested name is visibly not a confirmed one."""
    seen: dict[str, dict[str, Any]] = {}
    for item in items:
        code = str(item.get("sport_code"))
        entry = seen.setdefault(code, {
            "code": item.get("sport_code"), "sport": item.get("sport"),
            "family": item.get("sport_family"),
            "confidence": item.get("sport_name_confidence"), "workouts": 0,
        })
        entry["workouts"] += 1
        if item.get("sport_candidates"):
            entry["candidates"] = item["sport_candidates"]
    return sorted(seen.values(), key=lambda e: -e["workouts"])


def _workouts(client: api.ZeppClient, start: str, end: str,
              sport: str | None = None) -> tuple[list[dict[str, Any]], api.Collected]:
    collected = api.workout_history_all(client, start, end)
    items = [workouts.normalise(r) for r in collected.items]
    if sport:
        items = [i for i in items if sports.matches(i, sport)]
    items.sort(key=lambda i: i.get("start_local") or "", reverse=True)
    return items, collected


@server.tool(
    description="List workouts across all sports, newest first, with "
                "sport-specific metrics (SWOLF and stroke counts for swims, "
                "pace, cadence and running dynamics for runs, sets and reps "
                "for strength work), moving time, derived pace, heart-rate "
                "zones and Zepp's personal-best flags. Paginate with offset."
)
def zepp_list_workouts(
    from_date: str | None = None,
    to_date: str | None = None,
    sport: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Args: from_date/to_date as YYYY-MM-DD (default last 90 days); sport
    as a name fragment ("swim", "badminton"), a sport family ("cycling",
    "team_sport", "racket_sport" -- see zepp_describe_schema) or a numeric
    sport code; limit/offset page through the result (newest first).

    Every workout carries sport_name_confidence. Only "confirmed" names are
    verified on real data; "catalogued" names come from the watch's sport
    list and "contested" codes stay unknown_sport_<code> with candidates.
    Say which when it matters."""
    try:
        start, end = _range(from_date, to_date, days=90)
        items, collected = _workouts(_get_client(), start, end, sport)
    except _BadInput as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        return _fail(exc)

    if collected.status == "error":
        return _collected_status(collected)

    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    page = items[offset:offset + limit]
    result = {
        **_collected_status(collected),
        "range": {"from": start, "to": end},
        "total": len(items), "offset": offset, "returned": len(page),
        "workouts": page,
        "sports_present": sorted({i["sport"] for i in items}),
        "sports_seen": _sports_seen(items),
    }
    if offset + limit < len(items):
        result["next_offset"] = offset + limit
    if collected.status == "ok" and not items and sport:
        result["status"] = "no_data"
        result["note"] = f"No workouts matched sport={sport!r} in this range."
    return result


@server.tool(
    description="Full detail for one workout: laps (per length for swims, "
                "per set for strength), km splits, pauses, time-series "
                "streams (heart rate, speed, cadence, stride) and GPS. Get "
                "the track_id and source from zepp_list_workouts."
)
def zepp_workout_detail(
    track_id: str,
    source: str,
    include: list[Literal["summary", "laps", "splits", "streams", "gps"]] | None = None,
) -> dict[str, Any]:
    """Args: track_id and source, both from a zepp_list_workouts result.
    `include` selects which sections to decode; default is summary, laps and
    splits, because the stream and GPS sections can be very large."""
    try:
        outcome = api.workout_detail(_get_client(), track_id, source)
    except Exception as exc:
        return _fail(exc)

    if outcome.status != "ok":
        return outcome.as_dict()

    sections = set(include or ["summary", "laps", "splits"])
    data = outcome.data.get("data") if isinstance(outcome.data, dict) else None
    if not isinstance(data, dict):
        return {"status": "error", "note": "Detail payload had no data object."}

    result: dict[str, Any] = {"status": "ok", "track_id": track_id}
    result.update(decode.decode_detail(data, sections, _user_tz()))
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

    history = api.workout_history_all(client, start, end)
    if not history.items:
        return [], "workout_rows"
    estimates = []
    for row in history.items:
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
            entry["lactate_threshold_pace"] = (
                f"{int(pace) // 60}:{int(pace) % 60:02d}/km")
        estimates.append(entry)
    estimates.sort(key=lambda e: e["date"])
    return estimates, "workout_rows"


def _watch_statistic(client: api.ZeppClient, name: str, start: str,
                     end: str) -> dict[str, Any]:
    """One WatchSportStatistics series, passed through undecoded.

    Its response shape has never been captured by this project, so nothing
    is interpreted: the items come back raw (capped) with their provenance,
    or the failure comes back classified. Per-session exercise_load (in
    zepp_list_workouts) is the verified alternative."""
    try:
        outcome = api.watch_sport_statistics(client, name, start, end)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    out: dict[str, Any] = {
        "status": outcome.status, "verification_level": 3,
        "note": f"WatchSportStatistics/{name}: request parameters from "
                "independent clients; the response is not decoded by this "
                "server yet. Per-workout exercise_load and VO2_max in "
                "zepp_list_workouts are the verified figures.",
    }
    if outcome.status == "ok":
        data = outcome.data
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list):
            out["items_raw"] = items[:120]
            out["items_total"] = len(items)
        else:
            out["raw"] = data
    else:
        out.update({k: v for k, v in outcome.as_dict().items()
                    if k in ("http_status", "error_kind")})
    return out


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
        history = api.workout_history_all(client, start, end)
        if history.items:
            runs = [workouts.normalise(r) for r in history.items]
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
        "training_load": _watch_statistic(client, "SPORT_LOAD", start, end),
        "vo2_max_history": _watch_statistic(client, "VO2_MAX", start, end),
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


# --------------------------------------------------------------------------
# Health metrics beyond band_data
# --------------------------------------------------------------------------

HealthMetric = Literal[
    "readiness", "hrv", "resting_heart_rate", "respiratory_rate", "stress",
    "spo2", "pai", "body_charge", "daily_goals",
]

_SAMPLE_CAP = 2000


def _combine(collecteds: list[api.Collected]) -> dict[str, Any]:
    statuses = [c.status for c in collecteds]
    notes: list[str] = []
    for c in collecteds:
        notes.extend(c.notes)
        if c.error is not None:
            notes.append(f"a source failed: HTTP {c.error.http_status} "
                         f"({c.error.error_kind or 'error'})")
    if all(st == "error" for st in statuses):
        status = "error"
    elif any(st in ("error", "partial") for st in statuses):
        status = "partial"
    elif any(st == "ok" for st in statuses):
        status = "ok"
    else:
        status = "no_data"
    coverage: dict[str, Any] = {"requests": sum(c.requests for c in collecteds)}
    if notes:
        coverage["notes"] = notes
    out: dict[str, Any] = {"status": status, "coverage": coverage}
    if status == "no_data":
        out["note"] = ("Zepp returned no records for this metric and range. "
                       "Not proof of absence: the watch may not record this "
                       "metric, or the request may have been silently "
                       "rejected.")
    return out


def _cap(samples: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"samples": samples[:_SAMPLE_CAP]}
    if len(samples) > _SAMPLE_CAP:
        out["samples_truncated"] = True
        out["samples_total"] = len(samples)
    return out


def _health_metric(client: api.ZeppClient, metric: str, start: str, end: str,
                   tz: dt.tzinfo, include_samples: bool) -> dict[str, Any]:
    """Fetch, decode and roll up one metric. Returns days + trend key."""
    sources: list[api.Collected] = []
    extra: dict[str, Any] = {}

    def v2(event: str, sub: str, limit: int = 20) -> dict[str, Any]:
        collected = api.events_v2(client, event, sub, start, end, limit)
        sources.append(collected)
        return {"items": collected.items}

    def v1(event: str, sub: str | None = None) -> dict[str, Any]:
        collected = api.events_v1(client, event, start, end, sub)
        sources.append(collected)
        return {"items": collected.items}

    if metric == "readiness":
        days = events.readiness(v2("readiness", "watch_score"), tz)
        unit, trend_key = "0-100 scores; sleep_hrv_rmssd_ms in ms", "readiness_score"
    elif metric == "hrv":
        rmssd = events.hrv_rmssd(v2("HRVRMSSD", "real_data"), tz)
        ready = events.readiness(v2("readiness", "watch_score"), tz)
        nightly = {r["date"]: r for r in ready}
        sdnn = events.hrv_sdnn(v2("hrv_sdnn", "real_data"), tz)
        days = []
        rolled = {d["date"]: d for d in events.daily_rollup(rmssd, "rmssd_ms", tz)}
        sdnn_days = {d["date"]: d for d in events.daily_rollup(sdnn, "sdnn_ms", tz)}
        for day in sorted(set(rolled) | set(nightly) | set(sdnn_days)):
            entry: dict[str, Any] = {"date": day}
            if day in nightly and nightly[day].get("sleep_hrv_rmssd_ms"):
                entry["overnight_rmssd_ms"] = nightly[day]["sleep_hrv_rmssd_ms"]
            if day in rolled:
                entry["all_day_rmssd_ms"] = {k: v for k, v in rolled[day].items()
                                             if k != "date"}
            if day in sdnn_days:
                entry["sdnn_ms_unit_unverified"] = {
                    k: v for k, v in sdnn_days[day].items() if k != "date"}
            days.append(entry)
        unit, trend_key = "ms", "overnight_rmssd_ms"
        extra["note"] = ("overnight_rmssd_ms is the watch's own overnight HRV "
                         "(readiness sleepHRV). all_day_rmssd_ms is DERIVED: "
                         "statistics over that day's per-minute RMSSD samples, "
                         "awake and asleep, and runs higher and noisier.")
        if include_samples:
            extra.update(_cap([{k: v for k, v in p.items() if k != "epoch_ms"}
                               for p in rmssd]))
    elif metric == "resting_heart_rate":
        collected = api.band_data_range(client, start, end)
        sources.append(collected)
        days = []
        for row in collected.items:
            night = _night(row)
            if night and night.get("resting_heart_rate_bpm"):
                days.append({"date": night["date"],
                             "resting_heart_rate_bpm": night["resting_heart_rate_bpm"]})
        unit, trend_key = "bpm", "resting_heart_rate_bpm"
        extra["note"] = ("Sleeping resting heart rate, one per recorded night. "
                         "It equals the readiness record's sleepRHR on every "
                         "night checked.")
    elif metric == "respiratory_rate":
        points = events.respiratory_rate(v2("RespiratoryRate", "real_data"), tz)
        days = [{"date": d["date"], **{f"{k}": v for k, v in d.items() if k != "date"}}
                for d in events.daily_rollup(points, "breaths_per_minute", tz)]
        unit, trend_key = "breaths per minute", "avg"
        extra["note"] = ("Per-minute readings are recorded mostly during sleep; "
                         "daily figures are DERIVED statistics over them.")
        if include_samples:
            extra.update(_cap([{k: v for k, v in p.items() if k != "epoch_ms"}
                               for p in points]))
    elif metric == "stress":
        days = events.all_day_stress(v1("all_day_stress"), tz, include_samples)
        spot = events.single_stress(v1("single_stress"), tz)
        if spot:
            extra["single_measurements"] = _in_range(spot, start, end, "time")
        unit = "0-100 (relaxed <40, normal 40-59, medium 60-79, high >=80)"
        trend_key = "avg"
    elif metric == "spo2":
        odi = api.events_by_date(client, "blood_oxygen", "odi", start, end,
                                 _tz_name(tz))
        sources.append(odi)
        days = events.overnight_spo2({"items": odi.items}, tz)
        spot = events.spo2_spot_checks(v1("blood_oxygen", "click"), tz)
        extra["spot_checks"] = _in_range(spot, start, end, "time")
        unit = "odi in desaturation events per hour; spot checks in percent"
        trend_key = "odi_events_per_hour"
    elif metric == "pai":
        days = events.pai(v1("PaiHealthInfo"), tz)
        unit, trend_key = "PAI", "pai_7_day"
    elif metric == "body_charge":
        # ~1440 samples per day: a small page keeps each response bounded.
        days = events.body_charge(v2("Charge", "real_data", limit=7), tz,
                                  include_samples)
        unit, trend_key = "0-100", "max"
    elif metric == "daily_goals":
        days = events.daily_health(v2("DailyHealth", "summary"))
        unit, trend_key = "steps / kcal / minutes / metres", "steps"
    else:
        raise _BadInput(f"Unknown metric {metric!r}.")

    days = _in_range(days, start, end)
    result = {**_combine(sources), "unit": unit, "days": days, "day_count": len(days),
              **extra}
    if days and result.get("status") != "error":
        if result["status"] == "no_data":
            result["status"] = "ok"
            result.pop("note", None)
        values = [d.get(trend_key) for d in days
                  if isinstance(d.get(trend_key), (int, float))]
        if values:
            result["summary"] = {
                "field": trend_key, "mean": round(sum(values) / len(values), 1),
                "min": min(values), "max": max(values), "days_with_value": len(values),
                "method": "derived: over the days returned",
            }
        trend = analysis.daily_trend(days, trend_key)
        if trend:
            result["trend"] = {"field": trend_key, **trend}
    elif not days and result["status"] == "ok":
        result["status"] = "no_data"
        result["note"] = "Records came back but none fall inside the requested dates."
    return result


@server.tool(
    description="Daily health metrics over a date range, with a summary and "
                "a trend line: readiness (and its HRV / resting-HR / skin-"
                "temperature / breathing sub-scores), hrv (overnight and "
                "all-day RMSSD, SDNN), resting_heart_rate, respiratory_rate, "
                "stress (all-day and single), spo2 (overnight ODI and spot "
                "checks), pai, body_charge, daily_goals (steps/calories/"
                "active minutes against goals)."
)
def zepp_health_metrics(
    metric: HealthMetric,
    from_date: str | None = None,
    to_date: str | None = None,
    include_samples: bool = False,
) -> dict[str, Any]:
    """Args: metric (see the enum); from_date/to_date as YYYY-MM-DD, default
    the last 30 days; include_samples adds intraday samples for stress,
    hrv, respiratory_rate and body_charge (capped at 2000)."""
    try:
        start, end = _range(from_date, to_date, days=30)
        result = _health_metric(_get_client(), metric, start, end, _user_tz(),
                                include_samples)
    except _BadInput as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        return _fail(exc)
    entry = metrics.BY_NAME.get({
        "readiness": "readiness_score", "hrv": "hrv_rmssd_ms",
        "resting_heart_rate": "resting_heart_rate",
        "respiratory_rate": "respiratory_rate",
        "stress": "stress", "spo2": "overnight_odi", "pai": "pai",
        "body_charge": "body_charge", "daily_goals": "daily_goals"}[metric])
    return {
        "metric": metric, "range": {"from": start, "to": end},
        "timezone": _tz_name(_user_tz()),
        **({"source": {"endpoint": entry.endpoint, "field": entry.source_field,
                       "verification_level": entry.level}} if entry else {}),
        **result,
    }


# --------------------------------------------------------------------------
# Workout analytics
# --------------------------------------------------------------------------

@server.tool(
    description="Training volume and analytics across workouts: totals by "
                "week / month / year / sport (distance, moving time, "
                "calories, exercise load, time-weighted HR, aggregate pace), "
                "records within the range (longest, fastest, best SWOLF, "
                "most reps) with Zepp's own personal-best flags, and -- with "
                "`metric` -- a per-workout series and trend line (pace, "
                "swolf, cadence, vo2_max, avg_heart_rate, exercise_load, "
                "ground_contact_time, ...)."
)
def zepp_workout_stats(
    from_date: str | None = None,
    to_date: str | None = None,
    sport: str | None = None,
    group_by: Literal["week", "month", "year", "sport", "sport_family",
                      "none"] = "month",
    metric: Literal[
        "distance", "moving_time", "pace", "swim_pace", "avg_heart_rate",
        "max_heart_rate", "cadence", "exercise_load", "aerobic_training_effect",
        "vo2_max", "calories", "swolf", "strokes", "distance_per_stroke",
        "stride_length", "ground_contact_time", "vertical_oscillation", "power",
        "elevation_gain", "total_reps",
    ] | None = None,
) -> dict[str, Any]:
    """Args: from_date/to_date as YYYY-MM-DD (default last 90 days); sport
    filter as in zepp_list_workouts; group_by for the totals; metric for a
    per-workout progression series.

    Pace over a period is total distance / total moving time, never an
    average of per-workout paces. Aggregate pace is only given when the
    group holds a single distance sport, since a mixed run+swim pace means
    nothing."""
    try:
        start, end = _range(from_date, to_date, days=90)
        items, collected = _workouts(_get_client(), start, end, sport)
    except _BadInput as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        return _fail(exc)

    if collected.status == "error":
        return _collected_status(collected)

    result: dict[str, Any] = {
        **_collected_status(collected),
        "range": {"from": start, "to": end},
        "sport_filter": sport,
        "overall": analysis.totals(items),
        "by_sport": analysis.grouped(items, "sport"),
        "by_sport_family": analysis.grouped(items, "sport_family"),
        "records": analysis.records(items),
        "sports_seen": _sports_seen(items),
    }
    if group_by not in ("none", "sport", "sport_family"):
        result["by_period"] = {"group_by": group_by,
                               "periods": analysis.grouped(items, group_by)}
    if metric:
        result["series"] = analysis.series(items, metric)
    if not items and collected.status == "ok":
        result["status"] = "no_data"
        result["note"] = "No workouts matched in this range."
    return result


@server.tool(
    description="One-call overview of a period (default the last 30 days): "
                "activity (steps, distance, goal days), sleep averages, "
                "workouts by sport with volume and load, readiness and "
                "overnight HRV, resting-HR trend, stress and PAI. Use this "
                "first for broad 'how have I been doing' questions."
)
def zepp_period_summary(
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """Args: from_date/to_date as YYYY-MM-DD, default the last 30 days.

    Each section is fetched independently; one failing does not fail the
    others, and the failing section says so in place of its data."""
    try:
        start, end = _range(from_date, to_date, days=30)
        client = _get_client()
    except _BadInput as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        return _fail(exc)

    tz = _user_tz()
    sections: dict[str, Any] = {}
    failures: list[str] = []

    def guard(name: str, build: Any) -> None:
        try:
            sections[name] = build()
        except Exception as exc:  # one section never sinks the rest
            sections[name] = {"status": "error", "error": str(exc)}
            failures.append(name)

    def activity() -> dict[str, Any]:
        collected = api.band_data_range(client, start, end)
        days = [decode.summarise_day(r) for r in collected.items]
        nights = [n for n in (_night(r) for r in collected.items) if n]
        recorded = [n for n in nights if n.get("status") == "ok"]
        steps = [d.get("steps") or 0 for d in days]
        goal = [d for d in days if d.get("step_goal") and (d.get("steps") or 0)
                >= d["step_goal"]]
        sleep = {
            "nights_recorded": len(recorded),
            **{f"avg_{k}": analysis.mean_of(recorded, k) for k in (
                "total_asleep_minutes", "deep_minutes", "rem_minutes",
                "sleep_score", "sleep_efficiency_percent")},
        }
        rhr = [{"date": n["date"],
                "resting_heart_rate_bpm": n["resting_heart_rate_bpm"]}
               for n in recorded if n.get("resting_heart_rate_bpm")]
        sections["sleep"] = sleep
        sections["resting_heart_rate"] = {
            "avg_bpm": analysis.mean_of(rhr, "resting_heart_rate_bpm"),
            "trend": analysis.daily_trend(rhr, "resting_heart_rate_bpm"),
        }
        return {
            **_collected_status(collected),
            "days_with_data": len(days),
            "total_steps": sum(steps),
            "avg_daily_steps": round(sum(steps) / len(days)) if days else None,
            "distance_km": round(
                sum(d.get("distance_metres") or 0 for d in days) / 1000, 1),
            "days_step_goal_met": len(goal),
        }

    def training() -> dict[str, Any]:
        items, collected = _workouts(client, start, end)
        return {**_collected_status(collected), "overall": analysis.totals(items),
                "by_sport": analysis.grouped(items, "sport")}

    def recovery() -> dict[str, Any]:
        out: dict[str, Any] = {}
        for metric in ("readiness", "stress", "pai"):
            data = _health_metric(client, metric, start, end, tz, False)
            out[metric] = {k: data.get(k) for k in ("status", "summary", "trend")
                           if data.get(k) is not None}
            if metric == "readiness":
                hrv = [d for d in data.get("days", []) if d.get("sleep_hrv_rmssd_ms")]
                out["overnight_hrv_rmssd_ms"] = {
                    "avg": analysis.mean_of(hrv, "sleep_hrv_rmssd_ms"),
                    "trend": analysis.daily_trend(hrv, "sleep_hrv_rmssd_ms")}
            if metric == "pai" and data.get("days"):
                out["pai"]["latest_7_day"] = data["days"][-1].get("pai_7_day")
        return out

    guard("activity", activity)
    guard("training", training)
    guard("recovery", recovery)
    return {
        "status": "partial" if failures else "ok",
        "range": {"from": start, "to": end},
        "timezone": _tz_name(tz),
        **sections,
        **({"failed_sections": failures} if failures else {}),
        "note": "Averages and trends here are DERIVED from Zepp's daily and "
                "per-workout figures; drill down with zepp_sleep, "
                "zepp_health_metrics and zepp_workout_stats.",
    }


@server.tool(
    description="The account holder's body profile used by Zepp's "
                "calculations (height, weight, age from birth month, gender "
                "code, wear hand) and paired devices (model, firmware, data-"
                "source id). Identifying fields and device secrets are never "
                "returned."
)
def zepp_profile(include_devices: bool = True) -> dict[str, Any]:
    """Args: include_devices adds paired devices (model, firmware, the
    deviceSource id data records carry), from a route whose shape comes from
    other open-source clients and is not yet verified here."""
    try:
        client = _get_client()
        outcome = api.profile(client)
        device_outcome = api.devices(client) if include_devices else None
    except Exception as exc:
        return _fail(exc)
    if outcome.status != "ok":
        result: dict[str, Any] = outcome.as_dict()
        result.pop("data", None)
    else:
        data = outcome.data.get("data") if isinstance(outcome.data, dict) else None
        result = ({"status": "ok", **body.profile(data)} if isinstance(data, dict)
                  else {"status": "error",
                        "note": "Profile payload had no data object."})
    if device_outcome is not None:
        if device_outcome.status == "ok":
            result["devices"] = {"verification_level": 3,
                                 **body.device_summary(device_outcome.data)}
        else:
            result["devices"] = {k: v for k, v in device_outcome.as_dict().items()
                                 if k != "data"}
    return result


@server.tool(
    description="Explain what this server knows: which sport codes are "
                "identified, which stream units are verified, and where the "
                "decoding is still uncertain."
)
def zepp_describe_schema() -> dict[str, Any]:
    return {
        "sport_codes": sports.describe(),
        "stream_units": {
            name: {"unit": spec.unit, "encoding": spec.encoding,
                   "verified": spec.verified}
            for name, spec in decode.STREAM_SPECS.items()
        },
        "verification_levels": {
            "1": "live verified", "2": "fixture verified (checked against a "
            "second quantity in a real captured response)",
            "3": "corroborated by another implementation only",
            "4": "hypothesis", "5": "probed; unsupported or unresolved",
        },
        "metrics": metrics.describe(),
        "activity_stage_modes": decode.ACTIVITY_STAGE_MODES,
        "lap_columns": {"swim": decode.LAP_COLUMNS,
                        "strength": decode.STRENGTH_LAP_COLUMNS,
                        "splits": decode.SPLIT_COLUMNS},
        "known_gaps": [
            "Strength exercise codes (exercise_code / actionType) are the "
            "watch's own movement ids; no code-to-name table is verified, so "
            "sets are grouped by code, not named. The per-set load column's "
            "unit (kg or lb) is unverified.",
            "Activity-stage modes 1 and 7 are unnamed low-cadence activity; "
            "3 (walking) and 4 (running) are named from cadence and speed, "
            "not from the payload.",
            "The skin-temperature offset on readiness records has no "
            "established unit (skin_temperature_offset_raw_unverified).",
            "Body Charge mental/physical components and HRV SDNN have no "
            "second quantity in the corpus to check them against.",
            "Per-second heart rate: the file index decodes, but the route "
            "serving the files is unknown. Body Charge's stressInfo blob is "
            "an undecoded binary.",
            "Workout history paging follows data.next; every captured "
            "history fitted one page, so a multi-page walk is guarded "
            "(it stops and reports partial if a page repeats).",
            "Sport code 16 (free_training) has no positive evidence in the "
            "payload -- no field is prefixed for it the way ride_ or "
            "rope_skipping_ are for codes 9 and 21. It was named by "
            "elimination: the account owner logged exactly two new sport "
            "codes back-to-back on 2026-09-11, code 21 is unambiguously "
            "rope skipping from its own pb keys, which leaves 16 as the "
            "other one -- consistent with the row itself carrying zero "
            "distance, zero cadence and no block-specific field, the "
            "signature of an equipment-free session with nothing to "
            "measure. Weaker confirmation than every other code in this "
            "map; a screenshot of the Zepp app naming code 16 would settle "
            "it outright.",
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
