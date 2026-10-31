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
        "step_goal": summary.get("goal"),
    }
    # `runDist` was previously emitted as run_distance_metres. It is not: it
    # sits at 79-81 % of `dis` on every day in the corpus, including days
    # with no run at all (3150 of 3918 m), while the day's actual run
    # distance (3363 m, from DailyHealth and the workout itself) is a
    # different number. Kept, under a name that does not claim a meaning.
    if steps.get("runDist") is not None:
        out["runDist_raw_unverified"] = steps.get("runDist")
    if isinstance(steps.get("stage"), list) and steps["stage"]:
        out["activity_periods"] = _activity_periods(steps["stage"])

    max_hr = (summary.get("hr") or {}).get("maxHr") or {}
    if max_hr.get("hr"):
        out["max_heart_rate_bpm"] = max_hr["hr"]

    if sleep:
        out["sleep"] = _sleep_block(sleep, _tz(summary))
    return out


# Step-stage modes. VERIFIED that every stage's `step` total equals the sum
# of the per-minute step bytes over its [start, stop] minutes (97 of 97
# stages), so the minute indexing is certain. The NAMES are not: the payload
# carries none. Mode 4's stages run at ~150 steps/min and ~2.4 m/s, the
# signature of running; mode 3 at ~90-100 steps/min, brisk walking; modes 1
# and 7 at well under 30 steps/min. Cadence and speed are returned per
# stage so a caller can see the evidence rather than trust the label.
ACTIVITY_STAGE_MODES: dict[int, str] = {
    1: "light_activity_mode_1",
    3: "walking",
    4: "running",
    7: "light_activity_mode_7",
}


def _activity_periods(stages: list[Any]) -> dict[str, Any]:
    by_mode: dict[str, dict[str, float]] = {}
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        try:
            minutes = int(stage["stop"]) - int(stage["start"]) + 1
        except (KeyError, TypeError, ValueError):
            continue
        name = ACTIVITY_STAGE_MODES.get(stage.get("mode"),
                                        f"unknown_mode_{stage.get('mode')}")
        bucket = by_mode.setdefault(name, {"minutes": 0, "steps": 0,
                                           "distance_metres": 0, "periods": 0})
        bucket["minutes"] += minutes
        bucket["steps"] += stage.get("step") or 0
        bucket["distance_metres"] += stage.get("dis") or 0
        bucket["periods"] += 1
    for bucket in by_mode.values():
        if bucket["minutes"]:
            bucket["steps_per_minute"] = round(bucket["steps"] / bucket["minutes"], 1)
    return by_mode


def decode_activity_minutes(encoded: str) -> list[dict[str, int]] | None:
    """`data` is three bytes per minute of the local day, 1440 minutes.

    Byte 2 is the minute's step count -- VERIFIED: it sums to the day's
    `ttl` exactly on all eight days in the corpus, and reproduces every
    step stage's own total. Bytes 0 and 1 vary with activity but have no
    second quantity to check them against; they are returned raw as
    `kind_raw` / `intensity_raw`, never as a named measurement.
    """
    try:
        raw = base64.b64decode(encoded)
    except (ValueError, binascii.Error):
        return None
    if not raw or len(raw) % 3:
        return None
    return [{"kind_raw": raw[i], "intensity_raw": raw[i + 1], "steps": raw[i + 2]}
            for i in range(0, len(raw), 3)]


def hourly_steps(encoded: str) -> list[int] | None:
    """Per-hour step totals (24 entries, local hours) from the activity blob."""
    minutes = decode_activity_minutes(encoded)
    if minutes is None:
        return None
    hours = [0] * ((len(minutes) + 59) // 60)
    for index, minute in enumerate(minutes):
        hours[index // 60] += minute["steps"]
    return hours


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
            block.update(_bed_window(sleep, tzinfo, asleep))
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


def _bed_window(sleep: dict[str, Any], tzinfo: dt.tzinfo,
                asleep: int) -> dict[str, Any]:
    """Time in bed, sleep latency and efficiency.

    Four identities hold on every night in the corpus (7 of 7), which is what
    pins these fields' meaning rather than their names:

      lt + dp + dt + wk == (ed - st) / 60      the sleep window, in minutes
      st == obt + is                           `obt` = into bed, `is` = latency
      ebt == ed + lb                           `ebt` = out of bed, `lb` = lingering

    `obt`/`ebt` are minutes from local midnight of the wake-up date (negative
    when bedtime was the previous evening). Efficiency is DERIVED: minutes
    asleep over minutes in bed.
    """
    out: dict[str, Any] = {}
    latency = sleep.get("is")
    if isinstance(latency, int) and latency >= 0:
        out["sleep_latency_minutes"] = latency
    st, ed = sleep.get("st"), sleep.get("ed")
    window = (ed - st) // 60 if isinstance(st, int) and isinstance(ed, int) else None
    if window and window > 0:
        out["sleep_window_minutes"] = window
    obt, ebt = sleep.get("obt"), sleep.get("ebt")
    bounded = isinstance(obt, int) and isinstance(ebt, int) and ebt > obt
    if bounded and isinstance(ed, int):
        midnight = dt.datetime.fromtimestamp(ed, tzinfo).replace(
            hour=0, minute=0, second=0, microsecond=0)
        out["in_bed_local"] = (midnight + dt.timedelta(minutes=obt)).isoformat(
            timespec="minutes")
        out["out_of_bed_local"] = (midnight + dt.timedelta(minutes=ebt)).isoformat(
            timespec="minutes")
        in_bed = ebt - obt
        out["time_in_bed_minutes"] = in_bed
        if asleep and in_bed:
            out["sleep_efficiency_percent"] = round(asleep / in_bed * 100, 1)
            out["sleep_efficiency_method"] = (
                "derived: total_asleep_minutes / time_in_bed_minutes")
    return out


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


def decode_heart_zones(packed: str) -> dict[str, Any]:
    """Decode `heart_range`: time spent in each heart-rate zone.

    Format is `seconds,upper_bpm;seconds,upper_bpm;...` where each pair is
    the time spent below that boundary and above the previous one. It lives
    on the workout INDEX row, not the detail payload, so a zone breakdown
    costs no extra request.

    Verified on a run whose zone seconds total 3909 against a reported
    run_time of 3921 -- 0.3% apart, consistent with unrecorded gaps rather
    than a different quantity.

    Zone boundaries are the watch's own personalised ones. They are NOT
    recomputed here, because a zone model derived from a guessed maximum
    heart rate would disagree with what the athlete sees in the app.
    """
    pairs: list[tuple[int, int]] = []
    for chunk in packed.strip().rstrip(";").split(";"):
        if not chunk:
            continue
        parts = chunk.split(",")
        if len(parts) != 2:
            return {"note": "unrecognised heart_range format", "raw": packed}
        try:
            pairs.append((int(float(parts[0])), int(float(parts[1]))))
        except ValueError:
            return {"note": "unrecognised heart_range format", "raw": packed}

    total = sum(seconds for seconds, _ in pairs)
    if not total:
        return {"note": "no time recorded in any zone"}

    zones = []
    lower = 0
    for seconds, upper in pairs:
        zones.append({
            "lower_bpm": lower,
            "upper_bpm": upper,
            "seconds": seconds,
            "percent": round(seconds / total * 100, 1),
        })
        lower = upper
    return {"total_seconds": total, "zone_count": len(zones), "zones": zones}


# --------------------------------------------------------------------------
# Laps
# --------------------------------------------------------------------------

# Column meanings recovered by cross-referencing a pool-swim capture against
# its own summary. Unlisted columns are returned by index under raw_columns.
#
# Columns 1, 13 and 14 are CONFIRMED rather than inferred, by three
# independent routes:
#
#  1. Internal identity. On a length row, column 14 == column 1 + column 13,
#     holding on 34 of 36 laps in the reference swim (the rest differ by 1,
#     which is rounding).
#  2. The published definition. SWOLF is standardised across swim wearables
#     as the time in seconds for one pool length plus the strokes taken in
#     that length -- exactly the identity above.
#  3. Session reconstruction. Summing the components and dividing by the
#     length count reproduces the workout summary's own figure:
#     (974 s + 478 strokes) / 36 lengths = 40.3 against a reported swolf 40.
#
# Lower is better: it penalises both slowing down and adding strokes.
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


# Strength-training lap rows reuse the same ~70-column layout with different
# columns populated. VERIFIED against the session's own index row: column 22
# equals `strength_training_group[i].count` and column 28 its `actionType` on
# 19 of 19 sets; column 43 is column 1 in milliseconds; column 5's last value
# (4586) matches the session's run_time (4585). Column 21 steps up 15 -> 17.5
# -> 20 within an exercise, the shape of added load, but nothing in the
# payload states its unit (kg or lb), so it is flagged.
STRENGTH_LAP_COLUMNS: dict[int, str] = {
    0: "set_index",
    1: "duration_seconds",
    4: "avg_heart_rate_bpm",
    5: "elapsed_seconds_cumulative",
    21: "weight_unit_unverified",
    22: "reps",
    28: "exercise_code",
}


def decode_strength_laps(packed: str) -> dict[str, Any]:
    """One record per set, plus the DERIVED rest before each set
    (gap between the previous set's end and this set's start)."""
    rows = [r.split(",") for r in packed.strip().rstrip(";").split(";") if r]
    sets: list[dict[str, Any]] = []
    previous_end: float | None = None
    for row in rows:
        record: dict[str, Any] = {}
        for index, name in STRENGTH_LAP_COLUMNS.items():
            if index < len(row):
                value = clean(row[index])
                if value is not None:
                    record[name] = int(value) if value.is_integer() else value
        end = record.get("elapsed_seconds_cumulative")
        duration = record.get("duration_seconds")
        if previous_end is not None and end is not None and duration is not None:
            rest = end - duration - previous_end
            if rest >= 0:
                record["rest_before_seconds_derived"] = rest
        previous_end = end if end is not None else previous_end
        sets.append(record)
    return {
        "set_count": len(sets),
        "total_reps": sum(s.get("reps") or 0 for s in sets),
        "column_map": STRENGTH_LAP_COLUMNS,
        "sets": sets,
        "note": "exercise_code is the watch's own movement id; no name table "
                "is verified. weight_unit_unverified is the logged load "
                "without a confirmed unit.",
    }


# --------------------------------------------------------------------------
# Splits, gait and pauses (detail payload)
# --------------------------------------------------------------------------

# `kilo_pace` / `mile_pace`: one row per completed km / mile. Column 2 is a
# GEOHASH of where the split ended -- a location -- and is never decoded or
# returned. Columns below are corroborated on the corpus's single split (a
# 1064 m walk): column 1 (855) matches the walk's own 854 s/km pace, column 4
# its avg HR (125), column 9 its stride (74 cm) and column 13 its cadence
# (94 vs 93). One split cannot distinguish a per-split value from a running
# session value, so the extra columns carry that caveat.
SPLIT_COLUMNS: dict[int, str] = {
    0: "split_index",
    1: "split_seconds",
    # Elapsed time at the split, corroborated by an independent client;
    # 855 against 855 on the corpus's only split.
    5: "elapsed_seconds_cumulative",
    4: "avg_heart_rate_bpm",
    9: "avg_stride_length_cm",
    13: "avg_cadence_spm",
}
_SPLIT_PRIVATE_COLUMNS = frozenset({2})


def decode_splits(packed: str, unit: str = "km") -> dict[str, Any]:
    rows = [r.split(",") for r in packed.strip().rstrip(";").split(";") if r]
    splits = []
    for row in rows:
        record: dict[str, Any] = {}
        for index, name in SPLIT_COLUMNS.items():
            if index < len(row) and index not in _SPLIT_PRIVATE_COLUMNS:
                value = clean(row[index])
                if value is not None:
                    record[name] = int(value) if value.is_integer() else value
        seconds = record.get("split_seconds")
        if seconds:
            record["pace"] = f"{int(seconds) // 60}:{int(seconds) % 60:02d}/{unit}"
        splits.append(record)
    fastest = min((s for s in splits if s.get("split_seconds")),
                  key=lambda s: s["split_seconds"], default=None)
    out: dict[str, Any] = {
        "unit": unit, "split_count": len(splits), "splits": splits,
        "note": "Only split_seconds is verified per split. The HR, stride "
                "and cadence columns are corroborated on a single-split "
                "session only. The split's end location is deliberately "
                "omitted.",
    }
    if fastest:
        out["fastest_split_index"] = fastest.get("split_index")
    return out


def decode_gait(packed: str) -> dict[str, Any]:
    """`gait`: one `dt,?,stride_cm,cadence_spm` row per second.

    VERIFIED on a walk: column 0 sums to the session's run_time (909 s);
    column 2 averages 73.6 against the row's avg_stride_length 74 cm; column
    3 averages 94.9 against avg_frequency 93 and its maximum (107) equals
    max_frequency exactly. Column 1 is unidentified and not returned. Zeros
    (the first seconds, before a stride is established) are absences.
    """
    elapsed = 0
    offsets: list[int] = []
    stride: list[float | None] = []
    cadence: list[float | None] = []
    for chunk in packed.strip().rstrip(";").split(";"):
        parts = chunk.split(",")
        if len(parts) != 4:
            return {"note": "unrecognised gait format", "encoding": "unknown"}
        try:
            delta, _, stride_cm, spm = (float(p) for p in parts)
        except ValueError:
            return {"note": "unrecognised gait format", "encoding": "unknown"}
        elapsed += int(delta)
        offsets.append(elapsed)
        stride.append(stride_cm if stride_cm > 0 else None)
        cadence.append(spm if spm > 0 else None)

    def stats(values: list[float | None]) -> dict[str, Any]:
        present = [v for v in values if v is not None]
        if not present:
            return {}
        return {"min": min(present), "max": max(present),
                "avg": round(sum(present) / len(present), 1)}

    return {
        "sample_count": len(offsets),
        "duration_seconds": offsets[-1] if offsets else 0,
        "stride_length_cm": stats(stride),
        "cadence_spm": stats(cadence),
        "offsets_seconds": offsets,
        "stride_length_cm_values": stride,
        "cadence_spm_values": cadence,
    }


def decode_pauses(packed: str, tzinfo: dt.tzinfo = dt.UTC) -> dict[str, Any]:
    """`pause`: `start_epoch_seconds,duration_seconds,-1,-1,kind` per pause.

    Corroborated: a swim's single 22 s pause against its
    pauseTimeWithMillis of 20860, and a strength session's two 1 s pauses
    against 2610 ms. The trailing kind code is returned raw.
    """
    pauses = []
    for chunk in packed.strip().rstrip(";").split(";"):
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        try:
            start, seconds = int(float(parts[0])), int(float(parts[1]))
        except ValueError:
            continue
        record: dict[str, Any] = {"duration_seconds": seconds}
        if start > 0:
            record["start_local"] = dt.datetime.fromtimestamp(start, tzinfo).isoformat(
                timespec="seconds")
        if len(parts) >= 5:
            record["kind_raw"] = parts[4]
        pauses.append(record)
    return {"pause_count": len(pauses),
            "total_seconds": sum(p["duration_seconds"] for p in pauses),
            "pauses": pauses}


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


# Detail fields that carry location in a non-coordinate form. Never passed
# through as a raw stream: `kilo_pace`/`mile_pace` embed a geohash per split.
LOCATION_BEARING_FIELDS = frozenset({"kilo_pace", "mile_pace"})


def decode_detail(data: dict[str, Any], include: set[str],
                  tzinfo: dt.tzinfo = dt.UTC) -> dict[str, Any]:
    """Split a workout detail payload into laps / splits / gps / streams."""
    gps_fields = {"longitude_latitude", "altitude", "accuracy", "DEMAltitude"}
    out: dict[str, Any] = {}

    populated = {
        key: value for key, value in data.items()
        if isinstance(value, str) and value.strip()
    }

    if "laps" in include and "lap" in populated:
        # Strength sessions put sets in the lap field; strengthAssess is
        # present on exactly those payloads.
        if "strengthAssess" in populated:
            out["sets"] = decode_strength_laps(populated["lap"])
        else:
            out["laps"] = decode_laps(populated["lap"])

    if "splits" in include:
        for field, unit in (("kilo_pace", "km"), ("mile_pace", "mile")):
            if field in populated:
                splits = decode_splits(populated[field], unit)
                out.setdefault("splits", {})[unit] = splits

    if "pause" in populated and ("laps" in include or "summary" in include):
        out["pauses"] = decode_pauses(populated["pause"], tzinfo)

    if "gps" in include:
        tracks = {k: v for k, v in populated.items() if k in gps_fields}
        out["gps"] = (
            {k: decode_stream(k, v) for k, v in tracks.items()}
            if tracks else {"note": "no GPS recorded for this activity"}
        )

    if "streams" in include:
        skip = (gps_fields | LOCATION_BEARING_FIELDS
                | {"lap", "source", "provider", "pause", "gait", "strengthAssess"})
        out["streams"] = {
            key: decode_stream(key, value)
            for key, value in populated.items() if key not in skip
        }
        if "gait" in populated:
            out["streams"]["gait"] = decode_gait(populated["gait"])
        if "strengthAssess" in populated:
            out["streams"]["strengthAssess"] = {
                "encoding": "json", "note": "per-exercise assessment JSON; "
                "not decoded, returned raw", "raw": populated["strengthAssess"]}

    out["available_fields"] = sorted(populated)
    return out
