"""Decoders for Zepp's wire formats.

Three unrelated encodings live behind one API:

1. Daily band data  -- base64 JSON summary, plus one byte per minute of heart
   rate, plus a three-byte-per-minute activity blob.
2. Workout streams  -- semicolon-separated `time,value` pairs. Whether `value`
   is a delta or an absolute reading is PER FIELD, not global. Getting this
   wrong silently produces numbers that look real.
3. Workout laps     -- ~70 positional columns with no header.

The rule throughout: a field is decoded and unit-attributed only when the
fixture corpus proves its meaning. Everything else is passed through verbatim
so the caller can see it without being told what it means.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from .codes import HR_BYTE_SENTINELS, SLEEP_STAGE_MODES, clean


# --------------------------------------------------------------------------
# Daily band data
# --------------------------------------------------------------------------

def decode_band_summary(encoded: str) -> dict[str, Any]:
    """The `summary` field is base64-wrapped JSON."""
    try:
        return json.loads(base64.b64decode(encoded))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return {}


def decode_hr_minutes(encoded: str) -> list[int | None]:
    """`data_hr` is one unsigned byte per minute, 1440 per day.

    0, 254 and 255 are not-worn / no-reading markers. They are returned as
    None rather than dropped, so index still equals minute-of-day.
    """
    try:
        raw = base64.b64decode(encoded)
    except (ValueError, binascii.Error):
        return []
    return [None if byte in HR_BYTE_SENTINELS else byte for byte in raw]


def summarise_day(row: dict[str, Any]) -> dict[str, Any]:
    """One band_data row -> a flat, unit-labelled day summary."""
    summary = decode_band_summary(row.get("summary") or "")
    steps = summary.get("stp") or {}
    sleep = summary.get("slp") or {}

    out: dict[str, Any] = {
        "date": row.get("date_time"),
        "steps": steps.get("ttl"),
        "distance_metres": steps.get("dis"),
        "calories_kcal": steps.get("cal"),
        "run_distance_metres": steps.get("runDist"),
        "step_goal": summary.get("goal"),
    }

    max_hr = (summary.get("hr") or {}).get("maxHr") or {}
    if max_hr.get("hr"):
        out["max_heart_rate_bpm"] = max_hr["hr"]

    if sleep:
        out["sleep"] = _sleep_block(sleep, _tz(summary))
    return out


def _tz(summary: dict[str, Any]) -> dt.tzinfo:
    """Band payloads carry the watch's UTC offset in seconds as `tz`.

    Without it a 00:28 bedtime renders as the previous evening, which reads
    as a completely different night.
    """
    try:
        return dt.timezone(dt.timedelta(seconds=int(summary.get("tz"))))
    except (TypeError, ValueError):
        return dt.UTC


def _sleep_block(sleep: dict[str, Any],
                 tzinfo: dt.tzinfo = dt.UTC) -> dict[str, Any]:
    """Sleep stage minutes, all four of them.

    `dt` is REM. Reporting deep + light as total sleep omits it entirely.
    """
    stages = [s for s in sleep.get("stage") or [] if isinstance(s, dict)]
    by_mode: dict[str, int] = {}
    for stage in stages:
        name = SLEEP_STAGE_MODES.get(stage.get("mode"))
        if name is None:
            name = f"unknown_mode_{stage.get('mode')}"
        by_mode[name] = by_mode.get(name, 0) + (stage["stop"] - stage["start"] + 1)

    asleep = sum(v for k, v in by_mode.items() if k in ("light", "deep", "rem"))
    block: dict[str, Any] = {
        "light_minutes": sleep.get("lt"),
        "deep_minutes": sleep.get("dp"),
        "rem_minutes": sleep.get("dt"),
        "awake_minutes": sleep.get("wk"),
        "total_asleep_minutes": asleep or None,
        "wake_count": sleep.get("wc"),
        "sleep_score": sleep.get("ss"),
        "resting_heart_rate_bpm": sleep.get("rhr"),
        "stage_minutes_recomputed": by_mode,
    }
    for key, field in (("start_local", "st"), ("end_local", "ed")):
        value = sleep.get(field)
        if isinstance(value, int) and value > 0:
            block[key] = dt.datetime.fromtimestamp(value, tzinfo).isoformat(
                timespec="minutes")
    return {k: v for k, v in block.items() if v not in (None, {}, [])}


# --------------------------------------------------------------------------
# Workout streams
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class StreamSpec:
    encoding: str   # "delta" | "absolute"
    unit: str
    scale: float = 1.0
    verified: bool = True


# Encoding is per field and was determined by decoding each stream both ways
# and checking the result against the workout summary's own totals:
#
#   heart_rate       cumulative -> 116-164 bpm      (absolute -> nonsense)
#   temperature      cumulative -> 29.0 C           (absolute -> deltas only)
#   currentDistance  absolute   -> 75600 cm = 756 m (matches summary `dis`)
#   speed            absolute   -> 0-1.3 m/s, and integrates to 1061 m
#                                  against a summary distance of 1064 m
STREAM_SPECS: dict[str, StreamSpec] = {
    "heart_rate": StreamSpec("delta", "bpm"),
    "temperature": StreamSpec("delta", "celsius"),
    "currentDistance": StreamSpec("absolute", "metres", 0.01),
    "speed": StreamSpec("absolute", "metres_per_second"),
    "pool_stroke_speed": StreamSpec("absolute", "strokes_per_second"),
    # Value tracks the summary's per-length pace but its unit does not match
    # the summary's own seconds-per-metre. Passed through unconverted.
    "pool_swim_pace": StreamSpec("absolute", "unknown", verified=False),
    # No non-GPS fixture carries these; the corpus excludes GPS workouts.
    "altitude": StreamSpec("delta", "unknown", verified=False),
    "cadence": StreamSpec("absolute", "unknown", verified=False),
}


def parse_pairs(packed: str) -> list[tuple[int, float]] | None:
    """Split `t,v;t,v;...`. Returns None if the field is not pair-shaped."""
    out: list[tuple[int, float]] = []
    for chunk in packed.strip().rstrip(";").split(";"):
        if not chunk:
            continue
        parts = chunk.split(",")
        if len(parts) != 2:
            return None
        try:
            out.append((int(parts[0]), float(parts[1])))
        except ValueError:
            return None
    return out


def decode_stream(name: str, packed: str) -> dict[str, Any]:
    """Decode one packed stream into a time series with an explicit unit.

    An unrecognised or non-pair field is returned raw. It is never guessed at:
    a stream decoded with the wrong encoding yields plausible-looking numbers,
    which is worse than returning nothing.
    """
    pairs = parse_pairs(packed)
    if pairs is None:
        return {"name": name, "encoding": "unknown",
                "note": "not a time,value stream", "raw": packed}

    spec = STREAM_SPECS.get(name)
    if spec is None:
        return {"name": name, "encoding": "unknown", "unit": "unknown",
                "sample_count": len(pairs),
                "note": "unrecognised stream; values returned unconverted",
                "raw_pairs": pairs}

    elapsed = 0
    running = 0.0
    times: list[int] = []
    values: list[float | None] = []
    for delta_t, value in pairs:
        elapsed += delta_t
        running = running + value if spec.encoding == "delta" else value
        times.append(elapsed)
        values.append(clean(running * spec.scale))

    present = [v for v in values if v is not None]
    return {
        "name": name,
        "unit": spec.unit,
        "encoding": spec.encoding,
        "unit_verified": spec.verified,
        "sample_count": len(values),
        "duration_seconds": times[-1] if times else 0,
        "min": min(present) if present else None,
        "max": max(present) if present else None,
        "avg": round(sum(present) / len(present), 3) if present else None,
        "offsets_seconds": times,
        "values": values,
    }


# --------------------------------------------------------------------------
# Laps
# --------------------------------------------------------------------------

# Column meanings recovered by cross-referencing the pool-swim fixture against
# its own summary and sibling streams. Unlisted columns are returned by index.
LAP_COLUMNS: dict[int, str] = {
    0: "lap_index",
    2: "distance_metres",
    4: "avg_heart_rate_bpm",
    5: "elapsed_seconds_cumulative",
    9: "pace",
    12: "stroke_speed",
    13: "strokes",
}


def decode_laps(packed: str) -> dict[str, Any]:
    rows = [r.split(",") for r in packed.strip().rstrip(";").split(";") if r]
    laps: list[dict[str, Any]] = []
    previous = 0.0
    for row in rows:
        lap: dict[str, Any] = {}
        for index, name in LAP_COLUMNS.items():
            if index < len(row):
                lap[name] = clean(row[index], "altitude" if name == "altitude" else "default")
        cumulative = lap.get("elapsed_seconds_cumulative")
        if cumulative is not None:
            lap["duration_seconds"] = cumulative - previous
            previous = cumulative
        lap["raw_columns"] = row
        laps.append(lap)

    return {
        "lap_count": len(laps),
        "column_map": LAP_COLUMNS,
        "note": (
            "Column names are recovered, not documented. In the reference "
            "pool-swim capture the lap list held 38 records against a summary "
            "total_trips of 36, and column sums came to roughly twice the "
            "summary totals -- so laps appear to be recorded more than once. "
            "Do not sum these columns and present the result as a total; use "
            "the workout summary's own aggregates instead."
        ),
        "laps": laps,
    }


def decode_detail(data: dict[str, Any], include: set[str]) -> dict[str, Any]:
    """Split a workout detail payload into laps / gps / other streams."""
    gps_fields = {"longitude_latitude", "altitude", "accuracy", "DEMAltitude"}
    out: dict[str, Any] = {}

    populated = {
        key: value for key, value in data.items()
        if isinstance(value, str) and value.strip()
    }

    if "laps" in include and "lap" in populated:
        out["laps"] = decode_laps(populated["lap"])

    if "gps" in include:
        tracks = {k: v for k, v in populated.items() if k in gps_fields}
        out["gps"] = (
            {k: decode_stream(k, v) for k, v in tracks.items()}
            if tracks else {"note": "no GPS recorded for this activity"}
        )

    if "streams" in include:
        skip = gps_fields | {"lap", "source", "provider"}
        out["streams"] = {
            key: decode_stream(key, value)
            for key, value in populated.items() if key not in skip
        }

    out["available_fields"] = sorted(populated)
    return out
