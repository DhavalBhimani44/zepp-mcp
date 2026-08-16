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


def _stage_minutes(stages: object) -> dict[str, int]:
    by_mode: dict[str, int] = {}
    for stage in stages or []:
        if not isinstance(stage, dict):
            continue
        name = SLEEP_STAGE_MODES.get(stage.get("mode"))
        if name is None:
            name = f"unknown_mode_{stage.get('mode')}"
        by_mode[name] = by_mode.get(name, 0) + (stage["stop"] - stage["start"] + 1)
    return by_mode


def _sleep_block(sleep: dict[str, Any],
                 tzinfo: dt.tzinfo = dt.UTC) -> dict[str, Any]:
    """Sleep stage minutes, all four of them, plus naps.

    `dt` is REM. Reporting deep + light as total sleep omits it entirely.

    Two things this must not do, both found by running it against a real
    night the watch did not record:

    1. Report an unrecorded night as zeros. Zepp fills every field with 0 and
       sets `sleepSource: -1`. Emitting `sleep_score: 0` and
       `resting_heart_rate_bpm: 0` states a missing measurement as a
       measurement -- a resting heart rate of zero is not a low one.
    2. Discard naps. Daytime sleep lives in `odd_stage`, separate from the
       main `stage` list, and on a day with no main sleep it is the ONLY
       sleep there is.
    """
    by_mode = _stage_minutes(sleep.get("stage"))
    naps = _stage_minutes(sleep.get("odd_stage"))
    nap_asleep = sum(v for k, v in naps.items() if k in ("light", "deep", "rem"))

    # `sleepSource` is -1 when no main sleep was recorded. Corroborate with
    # the stage list rather than trusting one field.
    main_recorded = bool(by_mode) or any(
        sleep.get(field) for field in ("lt", "dp", "dt")
    )

    block: dict[str, Any] = {}
    if main_recorded:
        asleep = sum(v for k, v in by_mode.items() if k in ("light", "deep", "rem"))
        block.update({
            "light_minutes": sleep.get("lt"),
            "deep_minutes": sleep.get("dp"),
            "rem_minutes": sleep.get("dt"),
            "awake_minutes": sleep.get("wk"),
            "total_asleep_minutes": asleep or None,
            "wake_count": sleep.get("wc"),
            "sleep_score": sleep.get("ss") or None,
            "resting_heart_rate_bpm": sleep.get("rhr") or None,
            "stage_minutes_recomputed": by_mode,
        })
        for key, field in (("start_local", "st"), ("end_local", "ed")):
            value = sleep.get(field)
            if isinstance(value, int) and value > 0:
                block[key] = dt.datetime.fromtimestamp(value, tzinfo).isoformat(
                    timespec="minutes")
        # st == ed means a zero-length window, not a real night.
        if block.get("start_local") == block.get("end_local"):
            block.pop("start_local", None)
            block.pop("end_local", None)
    else:
        block["main_sleep_recorded"] = False
        block["note"] = (
            "No main sleep was recorded for this date -- the watch reports "
            "every field as zero. This is missing data, not a night of no "
            "sleep, and says nothing about how the person actually slept."
        )

    if naps:
        block["naps"] = {
            "nap_count": len([s for s in sleep.get("odd_stage") or []
                              if isinstance(s, dict)]),
            "total_asleep_minutes": nap_asleep,
            "stage_minutes": naps,
        }

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

# Column meanings recovered by cross-referencing a pool-swim capture against
# its own summary. Unlisted columns are returned by index under raw_columns.
#
# Column 14 is SWOLF, and it is self-checking: on every individual length,
# column 14 == column 1 + column 13, which is the literal definition of SWOLF
# (seconds for the length plus strokes taken). That identity is asserted in
# the tests and is what confirms columns 1 and 13.
LAP_COLUMNS: dict[int, str] = {
    0: "lap_index",
    1: "duration_seconds",
    2: "distance_metres",
    4: "avg_heart_rate_bpm",
    5: "elapsed_seconds_cumulative",
    9: "pace",
    12: "stroke_speed",
    13: "strokes",
    14: "swolf",
}


def decode_laps(packed: str) -> dict[str, Any]:
    """Split the lap field into individual laps and set summaries.

    The list interleaves TWO record types at different distances: one row per
    length (or per split), plus a summary row per set. In a 567 m swim of 27
    lengths the list held 29 rows -- 27 lengths of 21 m, plus set rows of
    63 m and 504 m. 3 + 24 = 27 lengths and 63 + 504 = 567 m.

    Summing the two types together is what made lap totals come to roughly
    double the workout summary, and made the record count disagree with
    `total_trips`. Separating them makes both reconcile exactly: the two set
    rows above carry 32 and 291 strokes, and 32 + 291 = 323, the reported
    `total_strokes`.

    Records are classified by distance: the most common distance is one
    length (or one split), and anything longer is a set summary.
    """
    rows = [r.split(",") for r in packed.strip().rstrip(";").split(";") if r]

    def parse(row: list[str]) -> dict[str, Any]:
        record: dict[str, Any] = {}
        for index, name in LAP_COLUMNS.items():
            if index < len(row):
                record[name] = clean(row[index])
        record["raw_columns"] = row
        return record

    parsed = [parse(row) for row in rows]
    distances = [r.get("distance_metres") for r in parsed
                 if r.get("distance_metres")]
    unit_distance = (
        max(set(distances), key=distances.count) if distances else None
    )

    laps, sets = [], []
    for record in parsed:
        distance = record.get("distance_metres")
        if unit_distance is not None and distance and distance > unit_distance:
            sets.append(record)
        else:
            laps.append(record)

    # SWOLF is self-checking; report whether the identity held.
    #
    # Tolerance is 1, not 0: Zepp computes SWOLF from unrounded seconds and
    # strokes and then rounds, while the two component columns are rounded
    # independently, so a ±1 disagreement is arithmetic rather than a wrong
    # column. On the reference swim 34 of 36 laps agree within 1; the two
    # that do not are off by 3.
    checked = [
        r for r in laps
        if r.get("swolf") is not None and r.get("duration_seconds") is not None
        and r.get("strokes") is not None
    ]
    consistent = sum(
        1 for r in checked
        if abs(r["swolf"] - (r["duration_seconds"] + r["strokes"])) <= 1
    )

    result: dict[str, Any] = {
        "lap_count": len(laps),
        "set_count": len(sets),
        "unit_distance_metres": unit_distance,
        "column_map": LAP_COLUMNS,
        "laps": laps,
    }
    if sets:
        result["sets"] = sets
    if checked:
        result["swolf_check"] = {
            "laps_checked": len(checked),
            "identity_holds": consistent,
            "rule": "swolf == duration_seconds + strokes (tolerance 1)",
        }
        if consistent < len(checked) * 0.9:
            result["note"] = (
                "SWOLF did not equal duration + strokes on every lap, so the "
                "column mapping may not hold for this sport. Treat the named "
                "columns as unverified here and read raw_columns instead."
            )
    return result


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
