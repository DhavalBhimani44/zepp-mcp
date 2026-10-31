"""Decoders for Zepp's event families -- the health metrics that are not
band_data and not workouts.

Three route shapes carry them, and each wraps its records in `items`:

* v2  `/v2/users/me/events`              -- readiness, HRV, respiratory rate,
                                             Body Charge, DailyHealth, ...
* v1  `/users/{uid}/events`              -- all-day stress, PAI, SpO2 spot
                                             checks, single stress readings
* v1  `/users/{uid}/events/dateString`   -- overnight SpO2 (ODI)

Every field mapped here was checked against a SECOND, independent quantity
in the fixture corpus rather than read off its name. The checks are listed
with each decoder and pinned by tests/test_events.py, so a decoder that
drifts is caught by Zepp's own arithmetic:

* readiness `sleepRHR` equals band_data's sleep `rhr` on all 6 shared nights.
* readiness `sleepHRV` is the mean of the per-minute HRVRMSSD samples inside
  that night's sleep window, to within 1 ms, on all 6 shared nights.
* all_day_stress min/max/avg and the four zone proportions recompute exactly
  from the event's own sample series, with zones <40 / 40-59 / 60-79 / >=80.
* ODI equals `odiNum / (cost / 3600)` -- events per hour of measured sleep --
  and `cost` matches that night's sleep duration from band_data.
* Respiratory-rate minutes begin at the sleep onset band_data reports.
* PaiHealthInfo `totalPai` is the running sum of `dailyPai` over the first
  seven days of the series.
* DailyHealth `totalSteps` / `totalCalories` equal band_data's `ttl` / `cal`
  on every shared day.
* Body Charge `total` is the mean of `mental` and `physical` to within 1.

Pure functions only: no I/O, no clock. 255 is the general "not computed"
marker across these payloads (a readiness sub-score of 255 is not a score).
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import statistics
from collections.abc import Iterable
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Readiness, PAI and Body Charge use 255 for "not computed"; 32767 appears on
# skin-temperature baselines before calibration. Neither is a reading.
NOT_COMPUTED = frozenset({255, 32767})

# All-day stress zones. VERIFIED: these four bands reproduce the payload's
# own relax/normal/medium/high proportions exactly on all six days in
# tests/fixtures/zepp/023.
STRESS_ZONES: tuple[tuple[int, str], ...] = (
    (40, "relaxed"),
    (60, "normal"),
    (80, "medium"),
    (101, "high"),
)


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int_or_float(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(value, 2)


# Zepp writes legacy IANA aliases ("Asia/Calcutta" on readiness and HRV
# events, "Asia/Kolkata" on workouts from the same watch). Minimal system
# tz databases ship only the canonical names, and an unresolved zone would
# silently fall back to UTC and move a morning score onto the previous day.
_LEGACY_ZONES: dict[str, str] = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Rangoon": "Asia/Yangon",
    "Asia/Dacca": "Asia/Dhaka",
    "Asia/Ulan_Bator": "Asia/Ulaanbaatar",
    "Europe/Kiev": "Europe/Kyiv",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Indianapolis": "America/Indiana/Indianapolis",
    "Pacific/Truk": "Pacific/Chuuk",
    "Atlantic/Faeroe": "Atlantic/Faroe",
}


def zone(name: object, fallback: dt.tzinfo = dt.UTC) -> dt.tzinfo:
    """Resolve a timezone name, including the v2 "N,Asia/Calcutta" form.

    v2 events prefix several metadata strings with the sample count
    (`"279,Asia/Calcutta"` on an item with 279 samples), so the name is the
    part after the last comma.
    """
    if not isinstance(name, str) or not name.strip():
        return fallback
    candidate = name.rsplit(",", 1)[-1].strip()
    for key in (candidate, _LEGACY_ZONES.get(candidate)):
        if not key:
            continue
        try:
            return ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return fallback


def iso(ms: float | int | None, tzinfo: dt.tzinfo) -> str | None:
    if ms is None:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, tzinfo).isoformat(timespec="minutes")


def local_date(ms: float | int, tzinfo: dt.tzinfo) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tzinfo).date().isoformat()


def _items(payload: Any) -> list[dict[str, Any]]:
    items = payload.get("items") if isinstance(payload, dict) else payload
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _value(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _score(value: object) -> int | float | None:
    number = _number(value)
    if number is None or number < 0 or number in NOT_COMPUTED:
        return None
    return _int_or_float(number)


def summarise_series(points: Iterable[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [p[key] for p in points if p.get(key) is not None]
    if not values:
        return {"samples": 0}
    return {
        "samples": len(values),
        "min": min(values),
        "max": max(values),
        "avg": round(statistics.fmean(values), 1),
    }


def daily_rollup(
    points: list[dict[str, Any]], key: str, tzinfo: dt.tzinfo
) -> list[dict[str, Any]]:
    """Group timestamped samples by local calendar date. Derived, not
    measured: each day's figures are statistics over that day's samples."""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for point in points:
        when = dt.datetime.fromisoformat(point["time"]).astimezone(tzinfo)
        by_day.setdefault(when.date().isoformat(), []).append(point)
    return [
        {"date": day, **summarise_series(rows, key)}
        for day, rows in sorted(by_day.items())
    ]


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeated items across overlapping pages, keeping order."""
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = (item.get("eventType"), item.get("subType"), item.get("timestamp"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# --------------------------------------------------------------------------
# Readiness  (v2 readiness / watch_score)
# --------------------------------------------------------------------------

# Sub-scores on a 0-100 scale. phy/ment/afib read 255 (not computed) on every
# record in the corpus; they are mapped so a watch that does compute them is
# not silently dropped, and stay absent until it does.
_READINESS_SCORES: dict[str, str] = {
    "rdnsScore": "readiness_score",
    "hrvScore": "hrv_score",
    "rhrScore": "resting_hr_score",
    "skinTempScore": "skin_temperature_score",
    "ahiScore": "sleep_breathing_score",
    "phyScore": "physical_score",
    "mentScore": "mental_score",
    "afibScore": "afib_score",
}


def readiness(payload: Any, fallback_tz: dt.tzinfo = dt.UTC) -> list[dict[str, Any]]:
    """One record per morning.

    `value.timestamp` is local midnight of the day the score is for; the
    item `timestamp` is when it was computed (minutes after wake-up, which
    is itself a check: it lands within a minute of band_data's sleep end).
    """
    out: list[dict[str, Any]] = []
    for item in _items(payload):
        value = _value(item)
        tzinfo = zone(value.get("timezoneId"), fallback_tz)
        day_ms = _number(value.get("timestamp")) or _number(item.get("timestamp"))
        if day_ms is None:
            continue
        record: dict[str, Any] = {
            "date": local_date(day_ms, tzinfo),
            "computed_at": iso(_number(item.get("timestamp")), tzinfo),
        }
        for src, key in _READINESS_SCORES.items():
            score = _score(value.get(src))
            if score is not None:
                record[key] = score
        hrv = _score(value.get("sleepHRV"))
        if hrv:
            record["sleep_hrv_rmssd_ms"] = hrv
        rhr = _score(value.get("sleepRHR"))
        if rhr:
            record["sleep_resting_hr_bpm"] = rhr
        # Signed offset from the personal skin-temperature baseline. Its unit
        # (tenths or hundredths of a degree?) is not established by anything
        # in the corpus, so it is passed through under a name that says so.
        offset = _number(value.get("skinTempCalibrated"))
        if offset is not None:
            record["skin_temperature_offset_raw_unverified"] = _int_or_float(offset)
        status = _number(value.get("status"))
        if status is not None:
            record["status_code"] = int(status)
        out.append(record)
    out.sort(key=lambda r: r["date"])
    return out


# --------------------------------------------------------------------------
# HRV  (v2 HRVRMSSD / real_data, hrv_sdnn / real_data)
# --------------------------------------------------------------------------


def _timed_samples(
    payload: Any, field: str, out_key: str, fallback_tz: dt.tzinfo
) -> list[dict[str, Any]]:
    """Samples are `{s: ms offset from value.startTime, <field>: value}`."""
    points: list[dict[str, Any]] = []
    for item in _items(payload):
        value = _value(item)
        tzinfo = zone(value.get("timeZone"), fallback_tz)
        start = _number(value.get("startTime"))
        samples = value.get("samples")
        if start is None or not isinstance(samples, list):
            continue
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            offset = _number(sample.get("s"))
            reading = _number(sample.get(field))
            if offset is None or reading is None or reading <= 0:
                continue
            points.append(
                {
                    "time": iso(start + offset, tzinfo),
                    "epoch_ms": int(start + offset),
                    out_key: _int_or_float(reading),
                }
            )
    points.sort(key=lambda p: p["epoch_ms"])
    return points


def hrv_rmssd(payload: Any, fallback_tz: dt.tzinfo = dt.UTC) -> list[dict[str, Any]]:
    """Per-minute RMSSD in milliseconds. VERIFIED: averaging these across a
    night's sleep window reproduces that morning's readiness `sleepHRV`."""
    return _timed_samples(payload, "hrv", "rmssd_ms", fallback_tz)


def hrv_sdnn(payload: Any, fallback_tz: dt.tzinfo = dt.UTC) -> list[dict[str, Any]]:
    """Spot SDNN measurements in milliseconds, a few per day. The unit
    follows from the field name and its 20-100 range (a plausible SDNN in
    ms); unlike RMSSD there is no second quantity in the corpus to check it
    against, so it carries `unit_verified: false` in the metric catalogue."""
    return _timed_samples(payload, "sdnn", "sdnn_ms", fallback_tz)


# --------------------------------------------------------------------------
# Respiratory rate  (v2 RespiratoryRate / real_data)
# --------------------------------------------------------------------------


def respiratory_rate(
    payload: Any, fallback_tz: dt.tzinfo = dt.UTC
) -> list[dict[str, Any]]:
    """`measurements` is base64, one unsigned byte per minute, 1440 per item.

    Index 0 is the item's own `timestamp` (UTC midnight), so index i is
    `timestamp + i minutes` -- verified because the first non-zero minute of
    each item's `timeZone[].period` lands on band_data's sleep onset. 0 is
    "no reading". Values are breaths per minute (12-20 across the corpus).
    """
    points: list[dict[str, Any]] = []
    for item in _items(payload):
        value = _value(item)
        base = _number(item.get("timestamp"))
        encoded = value.get("measurements")
        if base is None or not isinstance(encoded, str):
            continue
        try:
            minutes = base64.b64decode(encoded)
        except (ValueError, binascii.Error):
            continue
        tzinfo = fallback_tz
        periods = value.get("timeZone")
        if isinstance(periods, list) and periods and isinstance(periods[0], dict):
            offset = _number(periods[0].get("offset"))
            if offset is not None:
                tzinfo = dt.timezone(dt.timedelta(seconds=int(offset)))
        for index, reading in enumerate(minutes):
            if not reading or reading >= 255:
                continue
            epoch = int(base + index * 60000)
            points.append(
                {
                    "time": iso(epoch, tzinfo),
                    "epoch_ms": epoch,
                    "breaths_per_minute": reading,
                }
            )
    points.sort(key=lambda p: p["epoch_ms"])
    return points


# --------------------------------------------------------------------------
# Stress  (v1 all_day_stress, single_stress)
# --------------------------------------------------------------------------


def stress_zone(value: float) -> str:
    for upper, name in STRESS_ZONES:
        if value < upper:
            return name
    return "high"


def all_day_stress(
    payload: Any, fallback_tz: dt.tzinfo = dt.UTC, include_samples: bool = False
) -> list[dict[str, Any]]:
    """One record per day. The item's own summary fields are reported as
    measured; `data` is a JSON string of `{time: ms, value}` samples."""
    out: list[dict[str, Any]] = []
    for item in _items(payload):
        stamp = _number(item.get("timestamp"))
        if stamp is None:
            continue
        record: dict[str, Any] = {"date": local_date(stamp, fallback_tz)}
        for src, key in (
            ("avgStress", "avg"),
            ("minStress", "min"),
            ("maxStress", "max"),
        ):
            number = _number(item.get(src))
            if number is not None:
                record[key] = _int_or_float(number)
        zones = {}
        for src, key in (
            ("relaxProportion", "relaxed"),
            ("normalProportion", "normal"),
            ("mediumProportion", "medium"),
            ("highProportion", "high"),
        ):
            number = _number(item.get(src))
            if number is not None:
                zones[key] = _int_or_float(number)
        if zones:
            record["zone_percent"] = zones

        samples: list[dict[str, Any]] = []
        raw = item.get("data")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                when, level = _number(entry.get("time")), _number(entry.get("value"))
                if when is None or level is None or level <= 0 or level > 100:
                    continue
                samples.append(
                    {"time": iso(when, fallback_tz), "stress": _int_or_float(level)}
                )
        record["sample_count"] = len(samples)
        if include_samples:
            record["samples"] = samples
        out.append(record)
    out.sort(key=lambda r: r["date"])
    return out


def single_stress(
    payload: Any, fallback_tz: dt.tzinfo = dt.UTC
) -> list[dict[str, Any]]:
    """Manually triggered stress measurements."""
    out = []
    for item in _items(payload):
        stamp, level = _number(item.get("timestamp")), _number(item.get("value"))
        if stamp is None or level is None or level <= 0:
            continue
        out.append(
            {
                "time": iso(stamp, fallback_tz),
                "stress": _int_or_float(level),
                "zone": stress_zone(level),
            }
        )
    out.sort(key=lambda r: r["time"] or "")
    return out


# --------------------------------------------------------------------------
# PAI  (v1 PaiHealthInfo)
# --------------------------------------------------------------------------


def pai(payload: Any, fallback_tz: dt.tzinfo = dt.UTC) -> list[dict[str, Any]]:
    """Daily PAI. `totalPai` is the rolling 7-day score -- the figure the Zepp
    app headlines -- and `dailyPai` what that day contributed (capped at 75:
    the corpus shows 75 exactly on its heaviest training day, against zone
    contributions summing to 76.2). Zone lower limits are the watch's own
    personalised heart-rate thresholds.

    The watch can upload more than one record for a day (each upload
    re-states the day so far); only the latest `uploadTimestamp` per day is
    kept, as independent clients also do."""
    latest: dict[str, tuple[float, dict[str, Any]]] = {}
    for item in _items(payload):
        stamp = _number(item.get("time")) or _number(item.get("timestamp"))
        if stamp is None:
            continue
        day = local_date(stamp, fallback_tz)
        uploaded = _number(item.get("uploadTimestamp")) or 0.0
        if day not in latest or uploaded >= latest[day][0]:
            latest[day] = (uploaded, item)

    out: list[dict[str, Any]] = []
    for _, item in latest.values():
        stamp = _number(item.get("time")) or _number(item.get("timestamp"))
        if stamp is None:
            continue
        record: dict[str, Any] = {"date": local_date(stamp, fallback_tz)}
        for src, key in (("totalPai", "pai_7_day"), ("dailyPai", "pai_earned_today")):
            number = _number(item.get(src))
            if number is not None:
                record[key] = round(number, 1)
        zones = {}
        for band in ("low", "medium", "high"):
            entry: dict[str, Any] = {}
            for suffix, key in (
                ("ZonePai", "pai"),
                ("ZoneMinutes", "minutes"),
                ("ZoneLowerLimit", "lower_limit_bpm"),
            ):
                number = _number(item.get(f"{band}{suffix}"))
                if number is not None:
                    entry[key] = round(number, 2) if key == "pai" else int(number)
            if entry:
                zones[band] = entry
        if zones:
            record["zones"] = zones
        for src, key in (("restHr", "resting_hr_bpm"), ("maxHr", "max_hr_bpm")):
            number = _number(item.get(src))
            if number and number not in NOT_COMPUTED:
                record[key] = int(number)
        out.append(record)
    out.sort(key=lambda r: r["date"])
    return out


# --------------------------------------------------------------------------
# Blood oxygen  (v1 blood_oxygen / click, dateString blood_oxygen / odi)
# --------------------------------------------------------------------------


def spo2_spot_checks(
    payload: Any, fallback_tz: dt.tzinfo = dt.UTC
) -> list[dict[str, Any]]:
    """Spot SpO2 readings. The record lives in `extra`, JSON inside JSON."""
    out = []
    for item in _items(payload):
        extra = item.get("extra")
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except ValueError:
                extra = None
        if not isinstance(extra, dict):
            continue
        spo2 = _number(extra.get("spo2"))
        stamp = _number(extra.get("timestamp")) or _number(item.get("timestamp"))
        # 0 and -1 are "no reading" here as on workout rows; SpO2 below 50 %
        # is not survivable and is treated as a sensor fault, not data.
        if spo2 is None or stamp is None or spo2 < 50 or spo2 > 100:
            continue
        tzinfo = zone(extra.get("timezone") or item.get("timezone"), fallback_tz)
        record: dict[str, Any] = {
            "time": iso(stamp, tzinfo),
            "spo2_percent": _int_or_float(spo2),
        }
        if isinstance(extra.get("isAuto"), bool):
            record["automatic"] = extra["isAuto"]
        out.append(record)
    out.sort(key=lambda r: r["time"] or "")
    return out


def overnight_spo2(
    payload: Any, fallback_tz: dt.tzinfo = dt.UTC
) -> list[dict[str, Any]]:
    """Overnight oxygen desaturation (sleep breathing quality), one per night.

    VERIFIED: `odi` == `odiNum / (cost / 3600)`, truncated to two places --
    desaturation events per hour of measured sleep -- and `cost` equals that
    night's sleep duration in band_data (30420 s against a 00:58-09:25
    night). `score` is the Zepp sleep-breathing score shown in the app.
    """
    out = []
    for item in _items(payload):
        stamp = _number(item.get("timestamp"))
        if stamp is None:
            continue
        tzinfo = zone(item.get("timezone"), fallback_tz)
        record: dict[str, Any] = {"date": local_date(stamp, tzinfo)}
        odi = _number(item.get("odi"))
        if odi is not None and odi >= 0:
            record["odi_events_per_hour"] = round(odi, 2)
        count = _number(item.get("odiNum"))
        if count is not None and count >= 0:
            record["desaturation_events"] = int(count)
        seconds = _number(item.get("cost"))
        if seconds:
            record["measured_minutes"] = round(seconds / 60)
        score = _number(item.get("score"))
        if score is not None and 0 <= score <= 100:
            record["sleep_breathing_score"] = int(score)
        valid = _number(item.get("valid"))
        if valid is not None:
            record["validity_code"] = int(valid)
        out.append(record)
    out.sort(key=lambda r: r["date"])
    return out


# --------------------------------------------------------------------------
# Body Charge  (v2 Charge / real_data)
# --------------------------------------------------------------------------


def body_charge(
    payload: Any, fallback_tz: dt.tzinfo = dt.UTC, include_samples: bool = False
) -> list[dict[str, Any]]:
    """Per-minute Body Charge (0-100), rolled up per day.

    `total` is the Body Charge score; 255 marks minutes with no score. The
    corpus shows 255 throughout the days before the watch was paired, while
    `mental`/`physical` still carry an app-generated curve repeated identically
    day after day -- a model, not a measurement. So days are reported only
    from minutes with a real `total`; the two components are passed through
    alongside it, flagged unverified, because `total` reproduces their mean
    to within 1 but nothing establishes what each means on its own.
    """
    out: list[dict[str, Any]] = []
    for item in _items(payload):
        value = _value(item)
        tzinfo = zone(value.get("timeZone"), fallback_tz)
        start = _number(value.get("startTime"))
        samples = value.get("samples")
        if start is None or not isinstance(samples, list):
            continue
        minutes: list[dict[str, Any]] = []
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            total = _number(sample.get("total"))
            offset = _number(sample.get("s"))
            if total is None or offset is None or total in NOT_COMPUTED or total < 0:
                continue
            point: dict[str, Any] = {
                "time": iso(start + offset, tzinfo),
                "body_charge": int(total),
            }
            for key in ("mental", "physical"):
                number = _number(sample.get(key))
                if number is not None:
                    point[f"{key}_unverified"] = round(number, 1)
            minutes.append(point)
        if not minutes:
            continue
        values = [m["body_charge"] for m in minutes]
        record: dict[str, Any] = {
            "date": local_date(start, tzinfo),
            "minutes_scored": len(values),
            "min": min(values),
            "max": max(values),
            "first": values[0],
            "last": values[-1],
            "first_time": minutes[0]["time"],
            "last_time": minutes[-1]["time"],
        }
        if include_samples:
            record["samples"] = minutes
        out.append(record)
    out.sort(key=lambda r: r["date"])
    return out


# --------------------------------------------------------------------------
# Daily goals  (v2 DailyHealth / summary)
# --------------------------------------------------------------------------


def daily_health(payload: Any) -> list[dict[str, Any]]:
    """Daily totals against their goals.

    `totalSteps`/`totalCalories` equal band_data's own totals on every shared
    day. `totalRunningDistance` is NOT all-day running: it equals the
    distance of that day's recorded on-foot workouts (3363.28 m on the day of
    the 3363 m run, 1064.375 m on the day of a 1064 m WALK), so it is named
    for what it measures. `totalBurningDuration` is the app's "fat-burning"
    / active-minutes figure against `burningDurationGoal`.
    """
    out: list[dict[str, Any]] = []
    for item in _items(payload):
        value = _value(item)
        for sample in value.get("samples") or []:
            if not isinstance(sample, dict) or not sample.get("dateString"):
                continue
            record: dict[str, Any] = {"date": sample["dateString"]}
            for src, key in (
                ("totalSteps", "steps"),
                ("stepGoal", "step_goal"),
                ("totalCalories", "active_calories_kcal"),
                ("calorieGoal", "calorie_goal_kcal"),
                ("totalBurningDuration", "active_minutes"),
                ("burningDurationGoal", "active_minutes_goal"),
                ("totalRunningDistance", "on_foot_workout_distance_metres"),
                ("totalCyclingDistance", "cycling_workout_distance_metres"),
            ):
                number = _number(sample.get(src))
                if number is not None:
                    record[key] = (
                        round(number, 1) if not number.is_integer() else int(number)
                    )
            for total, goal, key in (
                ("steps", "step_goal", "step_goal_met"),
                ("active_calories_kcal", "calorie_goal_kcal", "calorie_goal_met"),
                ("active_minutes", "active_minutes_goal", "active_minutes_goal_met"),
            ):
                if record.get(goal):
                    record[key] = record.get(total, 0) >= record[goal]
            out.append(record)
    out.sort(key=lambda r: r["date"])
    return out


# --------------------------------------------------------------------------
# Second-resolution heart-rate file index  (fileInfo second_heart_rate)
# --------------------------------------------------------------------------


def second_hr_files(payload: Any) -> list[dict[str, Any]]:
    """Index of per-second heart-rate files. The index is decodable; the
    route that serves the files themselves has not been found, so this is
    exposed only as evidence of what exists."""
    out = []
    for item in _items(payload):
        value = _value(item)
        start = _number(value.get("startTime"))
        for sample in value.get("samples") or []:
            if not isinstance(sample, dict):
                continue
            s, e = _number(sample.get("s")), _number(sample.get("e"))
            out.append(
                {
                    "date": sample.get("dateString"),
                    "file_type": sample.get("fileType"),
                    "file_id": sample.get("fileId"),
                    "span_seconds": round((e - s) / 1000)
                    if s is not None and e
                    else None,
                    "start_epoch_ms": int(start + s)
                    if start is not None and s is not None
                    else None,
                }
            )
    return out


# --------------------------------------------------------------------------
# Joins with sleep
# --------------------------------------------------------------------------


def within(
    points: list[dict[str, Any]], start_epoch: int, end_epoch: int, key: str
) -> dict[str, Any]:
    """Statistics over samples falling inside [start, end] (epoch seconds).
    Derived: used to attach sleep-window respiratory rate and HRV to a night."""
    window = [
        p[key]
        for p in points
        if p.get(key) is not None
        and start_epoch * 1000 <= p["epoch_ms"] <= end_epoch * 1000
    ]
    if not window:
        return {"samples": 0}
    return {
        "samples": len(window),
        "avg": round(statistics.fmean(window), 1),
        "min": min(window),
        "max": max(window),
    }
