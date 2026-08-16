"""Normalise a workout index row into something an LLM can read safely.

The index row carries ~172 fields, most of them not applicable to any given
sport and filled with a sentinel. Emitting all of them invites the model to
report `swolf: -1` as a swim statistic on a bike ride.

So: a common block every sport has, plus one sport-specific block, plus the
untouched row under `raw` for anything not covered.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .codes import clean, sport_name

# Fields worth surfacing per sport, in the sport's own terms. A field is only
# emitted when it is not that field's not-applicable sentinel.
SPORT_FIELDS: dict[str, tuple[str, ...]] = {
    "swim": (
        "swolf", "total_strokes", "total_trips", "swim_pool_length",
        "avg_distance_per_stroke", "avg_stroke_speed", "max_stroke_speed",
        "swim_style", "freestyle_length", "breast_stroke_length",
        "butterfly_length", "medley_length", "back_stroke_length",
        "other_stroke_length",
    ),
    "foot": (
        "total_step", "avg_pace", "max_pace", "min_pace", "avg_cadence",
        "max_cadence", "avg_stride_length", "elevationGain", "elevationLoss",
        "avg_altitude", "max_altitude", "min_altitude", "landing_time",
        "flight_ratio", "forefoot_ratio", "marathon", "run_time",
    ),
    "strength": (
        "total_group", "work_value", "totalMuscularExertion",
        "totalCardiacExertion", "totalScore", "strengthScores",
    ),
    "hike": (
        "total_step", "avg_pace", "max_pace", "run_time",
        "elevationGain", "elevationLoss", "altitude_ascend",
        "altitude_descend", "distance_ascend", "avg_altitude",
        "max_altitude", "min_altitude", "avg_slope", "max_slope",
    ),
    "ride": (
        "avg_power", "max_power", "avg_frequency", "max_frequency",
        "avg_slope", "max_slope", "elevationGain", "elevationLoss",
    ),
}

# Which sport-specific block applies to which numeric type code.
_BLOCK_FOR_CODE: dict[int, str] = {
    1: "foot", 8: "foot", 14: "swim", 22: "hike", 52: "strength",
}

COMMON_FIELDS: tuple[str, ...] = (
    "calorie", "dis", "avg_heart_rate", "max_heart_rate", "min_heart_rate",
    "te", "anaerobic_te", "exercise_load", "rpe", "VO2_max", "spo2_max",
    "spo2_min", "avg_temperature", "min_temperature", "max_temperature",
)

# Altitude readings and elevation totals use DIFFERENT not-applicable
# markers, so they cannot share a sentinel family.
_ALTITUDE_FIELDS = frozenset({
    "avg_altitude", "max_altitude", "min_altitude", "upstairs_height",
})
_ELEVATION_FIELDS = frozenset({
    "elevationGain", "elevationLoss", "altitude_ascend", "altitude_descend",
})

# elevationGain / elevationLoss are CENTIMETRES, and are renamed on the way
# out so the unit travels with the number.
#
# Verified in the hiking rows: elevationGain 27961 sits beside
# altitude_ascend 279 in the same row, and elevationLoss 22926 beside
# altitude_descend 229. Emitted raw, "27961" reads as a plausible metre
# figure in a table and turns a 280 m hill into an alpine ascent.
_CENTIMETRE_FIELDS: dict[str, str] = {
    "elevationGain": "elevation_gain_metres",
    "elevationLoss": "elevation_loss_metres",
}

_TEMPERATURE_FIELDS = frozenset({
    "avg_temperature", "min_temperature", "max_temperature",
})
_PERCENTAGE_FIELDS = frozenset({"spo2_max", "spo2_min"})

# Which sentinel family guards each field. A field left out defaults to -1
# only, which is why a -274 temperature reached the output on first run.
_FAMILY_FOR_FIELD: dict[str, str] = {
    **{name: "altitude" for name in _ALTITUDE_FIELDS},
    **{name: "elevation" for name in _ELEVATION_FIELDS},
    **{name: "temperature" for name in _TEMPERATURE_FIELDS},
    **{name: "percentage" for name in _PERCENTAGE_FIELDS},
}


def _epoch(value: object) -> int | None:
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _zone(row: dict[str, Any]) -> dt.tzinfo:
    """Workout rows name their timezone (`syncedTimezone: "Asia/Kolkata"`).

    An 08:04 swim rendered in UTC becomes 02:34, which looks like a different
    session entirely when the user scans a list by time of day.
    """
    name = row.get("syncedTimezone")
    if isinstance(name, str) and name.strip():
        try:
            return ZoneInfo(name.strip())
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return dt.UTC


def _iso(value: object, tzinfo: dt.tzinfo = dt.UTC) -> str | None:
    seconds = _epoch(value)
    if seconds is None:
        return None
    return dt.datetime.fromtimestamp(seconds, tzinfo).isoformat(timespec="seconds")


def _numeric(value: object) -> float | None:
    """Zepp sends numbers as strings about half the time: a workout row has
    `dis` as "756.0" and `type` as 14. Sentinel stripping has to see through
    that, or `swolf: "-1"` survives into the output as real-looking data."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _collect(row: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in names:
        if name not in row:
            continue
        value = row[name]
        if isinstance(value, (list, dict)):
            if value:
                out[name] = value
            continue
        number = _numeric(value)
        if number is not None:
            family = _FAMILY_FOR_FIELD.get(name, "default")
            cleaned = clean(number, family)
            if cleaned is None:
                continue
            if name in _CENTIMETRE_FIELDS:
                out[_CENTIMETRE_FIELDS[name]] = round(cleaned / 100, 2)
            else:
                out[name] = int(cleaned) if cleaned.is_integer() else cleaned
            continue
        if isinstance(value, str) and value.strip():
            out[name] = value
    return out


def normalise(row: dict[str, Any], include_raw: bool = False) -> dict[str, Any]:
    code = row.get("type")
    start = _epoch(row.get("trackid"))
    end = _epoch(row.get("end_time"))

    zone = _zone(row)
    out: dict[str, Any] = {
        "track_id": str(row.get("trackid", "")),
        "source": row.get("source"),
        "sport": sport_name(code),
        "sport_code": code,
        "start_local": _iso(row.get("trackid"), zone),
        "end_local": _iso(row.get("end_time"), zone),
        "timezone": row.get("syncedTimezone") or "UTC",
    }
    if start and end and end > start:
        out["elapsed_seconds"] = end - start

    out["summary"] = _collect(row, COMMON_FIELDS)

    code_number = _numeric(code)
    block = _BLOCK_FOR_CODE.get(int(code_number) if code_number is not None else -1)
    if block:
        detail = _collect(row, SPORT_FIELDS[block])
        if detail:
            out[block] = detail
    else:
        # Unnamed sport: show every sport block that has non-sentinel values,
        # so nothing is hidden just because the code is unrecognised.
        for name, fields in SPORT_FIELDS.items():
            detail = _collect(row, fields)
            if detail:
                out.setdefault("unclassified_metrics", {})[name] = detail

    parent = row.get("parent_trackid")
    children = row.get("child_list")
    parent_number = _numeric(parent)
    has_parent = parent is not None and (parent_number is None or parent_number > 0)
    if has_parent or children:
        out["multisport"] = {
            "parent_track_id": parent,
            "child_track_ids": children,
            "note": "This activity is a leg of, or the parent of, a "
                    "multi-sport session.",
        }

    if include_raw:
        out["raw"] = row
    return out
