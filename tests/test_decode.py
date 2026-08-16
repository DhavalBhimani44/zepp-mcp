"""Decoder tests against the real captured corpus in tests/fixtures/zepp/.

Every assertion here is anchored to a value the Zepp API itself reported in
the same payload, so a decoder that drifts is caught by Zepp's own arithmetic
rather than by a number someone typed into a test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zepp_mcp import decode, workouts
from zepp_mcp.client import _is_empty, parse_rows
from zepp_mcp.codes import sport_name

FIXTURES = Path(__file__).parent / "fixtures" / "zepp"


def body(name: str):
    return json.loads((FIXTURES / name).read_text())["body_parsed"]


@pytest.fixture(scope="module")
def band_rows():
    return body("014_query_type_detail.json")["data"]


@pytest.fixture(scope="module")
def swim_detail():
    return body("041_workout_detail_type14_1786761306.json")["data"]


@pytest.fixture(scope="module")
def walk_detail():
    return body("043_workout_detail_type8_1786631538.json")["data"]


@pytest.fixture(scope="module")
def history_rows():
    return parse_rows(body("040_workout_history_run.json"))


# -- daily / sleep ---------------------------------------------------------

def test_sleep_stage_modes_reproduce_zepps_own_totals(band_rows):
    """Modes 4/5/8/7 are light/deep/rem/awake.

    The summary reports lt/dp/dt/wk independently of the stage list, so
    recomputing from stages and matching all four proves the mapping.
    """
    checked = 0
    for row in band_rows:
        summary = decode.decode_band_summary(row["summary"])
        sleep = summary.get("slp")
        if not sleep or not sleep.get("stage"):
            continue
        block = decode._sleep_block(sleep)
        recomputed = block["stage_minutes_recomputed"]
        assert recomputed.get("light") == sleep["lt"]
        assert recomputed.get("deep") == sleep["dp"]
        assert recomputed.get("rem") == sleep["dt"]
        assert recomputed.get("awake") == sleep["wk"]
        checked += 1
    assert checked >= 3, "expected several nights of sleep in the corpus"


def test_total_asleep_includes_rem(band_rows):
    """Deep + light is not total sleep. REM is a third of it here."""
    summary = decode.summarise_day(band_rows[0])
    sleep = summary["sleep"]
    assert sleep["rem_minutes"] > 0
    assert sleep["total_asleep_minutes"] == (
        sleep["light_minutes"] + sleep["deep_minutes"] + sleep["rem_minutes"]
    )
    assert sleep["total_asleep_minutes"] > sleep["light_minutes"] + sleep["deep_minutes"]


def test_daily_summary_units(band_rows):
    day = decode.summarise_day(band_rows[0])
    assert day["steps"] > 0
    assert day["distance_metres"] > 0
    assert day["date"] == band_rows[0]["date_time"]


def test_hr_minutes_mask_sentinels_without_shifting_index(band_rows):
    minutes = decode.decode_hr_minutes(band_rows[0]["data_hr"])
    assert len(minutes) == 1440, "one byte per minute of the day"
    readings = [v for v in minutes if v is not None]
    assert readings, "expected some readings"
    # 0/254/255 are markers, not heart rates.
    assert all(0 < v < 254 for v in readings)
    assert len(readings) < len(minutes), "unworn minutes should be masked"


# -- workout streams -------------------------------------------------------

def test_heart_rate_stream_is_delta_encoded(swim_detail):
    stream = decode.decode_stream("heart_rate", swim_detail["heart_rate"])
    assert stream["encoding"] == "delta"
    assert stream["unit"] == "bpm"
    # Decoded as deltas these are a plausible swim; decoded as absolutes the
    # series would be dominated by values near zero.
    assert 60 <= stream["min"] <= 200
    assert 60 <= stream["max"] <= 220
    assert stream["min"] < stream["avg"] < stream["max"]


def test_distance_stream_matches_the_summary_total(swim_detail, history_rows):
    """currentDistance is absolute centimetres, not a delta."""
    stream = decode.decode_stream("currentDistance", swim_detail["currentDistance"])
    swim = next(r for r in history_rows if str(r["trackid"]) == "1786761306")
    assert stream["encoding"] == "absolute"
    assert stream["unit"] == "metres"
    assert stream["max"] == pytest.approx(float(swim["dis"]), rel=0.01)


def test_speed_stream_integrates_to_the_summary_distance(walk_detail, history_rows):
    stream = decode.decode_stream("speed", walk_detail["speed"])
    walk = next(r for r in history_rows if str(r["trackid"]) == "1786631538")
    # One sample per second, so summing m/s approximates total metres.
    travelled = sum(v for v in stream["values"] if v is not None)
    assert travelled == pytest.approx(float(walk["dis"]), rel=0.02)


def test_temperature_stream_decodes_to_a_real_temperature(swim_detail):
    stream = decode.decode_stream("temperature", swim_detail["temperature"])
    assert stream["encoding"] == "delta"
    assert 0 < stream["max"] < 60


def test_unknown_stream_is_passed_through_without_a_unit(walk_detail):
    """An unmodelled stream must not be given a plausible unit."""
    stream = decode.decode_stream("gait", walk_detail["gait"])
    assert stream["encoding"] == "unknown"
    assert "raw" in stream or "raw_pairs" in stream
    assert stream.get("unit", "unknown") == "unknown"
    assert "avg" not in stream


def test_unverified_stream_is_flagged(swim_detail):
    stream = decode.decode_stream("pool_swim_pace", swim_detail["pool_swim_pace"])
    assert stream["unit_verified"] is False


# -- laps ------------------------------------------------------------------

def test_laps_decode_with_durations_and_a_reliability_note(swim_detail, history_rows):
    laps = decode.decode_laps(swim_detail["lap"])
    swim = next(r for r in history_rows if str(r["trackid"]) == "1786761306")

    assert laps["lap_count"] > 0
    first = laps["laps"][0]
    assert first["distance_metres"] == float(swim["swim_pool_length"])
    assert all("duration_seconds" in lap for lap in laps["laps"])
    assert all("raw_columns" in lap for lap in laps["laps"])

    # The record count disagrees with the summary; the note must say so
    # rather than let a caller sum the columns and trust the result.
    assert laps["lap_count"] != int(swim["total_trips"])
    assert "total_trips" in laps["note"]


def test_gps_section_reports_absence_rather_than_inventing_a_track(swim_detail):
    result = decode.decode_detail(swim_detail, {"gps"})
    assert "no GPS" in result["gps"]["note"]


# -- workout normalisation -------------------------------------------------

def test_swim_row_surfaces_swim_metrics(history_rows):
    row = next(r for r in history_rows if r["type"] == 14)
    item = workouts.normalise(row)
    assert item["sport"] == "pool_swimming"
    assert item["swim"]["swolf"] == int(row["swolf"])
    assert item["swim"]["total_strokes"] == int(row["total_strokes"])
    assert item["swim"]["swim_pool_length"] == int(row["swim_pool_length"])
    assert item["elapsed_seconds"] > 0


def test_non_swim_row_never_reports_swim_metrics(history_rows):
    """swolf is -1 on a walk. Emitting it would read as a real statistic."""
    row = next(r for r in history_rows if r["type"] == 8)
    item = workouts.normalise(row)
    assert "swim" not in item
    assert "swolf" not in json.dumps(item)


def test_strength_row_surfaces_set_count(history_rows):
    row = next(r for r in history_rows if r["type"] == 52)
    item = workouts.normalise(row)
    assert item["sport"] == "strength_training"
    assert item["strength"]["total_group"] == int(row["total_group"])


def test_hiking_row_surfaces_elevation_in_metres(history_rows):
    """elevationGain is centimetres. Emitted raw it turns a 280 m climb into
    a 27961 m one, which still looks like a number a watch might report."""
    row = next(r for r in history_rows if r["type"] == 22)
    item = workouts.normalise(row)
    assert item["sport"] == "hiking"
    hike = item["hike"]
    assert "elevationGain" not in hike, "raw centimetre field must not escape"
    assert hike["elevation_gain_metres"] == pytest.approx(
        float(row["elevationGain"]) / 100, rel=1e-6)
    # The row's own metre-denominated ascent figure agrees to within 1 m.
    assert hike["elevation_gain_metres"] == pytest.approx(
        float(row["altitude_ascend"]), abs=1.0)
    assert hike["elevation_loss_metres"] == pytest.approx(
        float(row["altitude_descend"]), abs=1.0)


def test_unidentified_sport_is_named_as_unknown_not_guessed(history_rows):
    """Every code in the corpus is now identified, so use a synthetic one:
    an unrecognised sport must degrade to a plain label, not a guess."""
    row = dict(next(r for r in history_rows if r["type"] == 22))
    row["type"] = 99
    item = workouts.normalise(row)
    assert item["sport"] == "unknown_sport_99"
    assert item["sport_code"] == 99
    # The data is still exposed, just not mislabelled.
    assert item["summary"]["calorie"] > 0
    assert item["unclassified_metrics"]


def test_sentinels_are_stripped_from_the_summary(history_rows):
    for row in history_rows:
        item = workouts.normalise(row)
        assert -1 not in item["summary"].values()


def test_sport_name_handles_junk():
    assert sport_name(None) == "unknown_sport_None"
    assert sport_name("14") == "pool_swimming"


def test_every_history_row_normalises(history_rows):
    assert len(history_rows) == 8
    for row in history_rows:
        item = workouts.normalise(row)
        assert item["track_id"]
        assert item["source"]


# -- empty-200 classification ---------------------------------------------

@pytest.mark.parametrize("name", [
    "003_retention_2025.json", "029_evdate_blood_oxygen_osa_event.json",
])
def test_known_empty_responses_are_detected(name):
    assert _is_empty(body(name)) is True


def test_populated_response_is_not_empty():
    assert _is_empty(body("040_workout_history_run.json")) is False


# -- timezone --------------------------------------------------------------

def test_workout_times_use_the_watch_timezone(history_rows):
    """A workout recorded at 08:04 IST must not be reported as 02:34."""
    row = next(r for r in history_rows if str(r["trackid"]) == "1786761306")
    item = workouts.normalise(row)
    assert item["timezone"] == "Asia/Kolkata"
    assert item["start_local"].endswith("+05:30")
    # 08:05 local. The same instant rendered in UTC is 02:35, which reads as
    # a middle-of-the-night session rather than a morning swim.
    assert item["start_local"] == "2026-08-15T08:05:06+05:30"


def test_sleep_times_use_the_band_utc_offset(band_rows):
    """`tz` is a UTC offset in seconds; ignoring it shifts the night."""
    summary = decode.decode_band_summary(band_rows[0]["summary"])
    assert summary["tz"] == "19800"
    block = decode.summarise_day(band_rows[0])["sleep"]
    assert block["start_local"].endswith("+05:30")


def test_sport_code_as_string_still_selects_the_sport_block(history_rows):
    row = dict(next(r for r in history_rows if r["type"] == 14))
    row["type"] = "14"
    item = workouts.normalise(row)
    assert item["sport"] == "pool_swimming"
    assert "swim" in item


def test_impossible_sensor_values_are_stripped(history_rows):
    """-274 C is below absolute zero and 0% SpO2 is not a reading. Both are
    no-sensor markers, and both survived the first implementation."""
    for row in history_rows:
        summary = workouts.normalise(row)["summary"]
        for field in ("avg_temperature", "min_temperature", "max_temperature"):
            assert summary.get(field, 20) > -273, f"{field} below absolute zero"
        for field in ("spo2_max", "spo2_min"):
            assert summary.get(field, 98) > 0, f"{field} reported as zero"


def test_real_temperature_still_survives(history_rows):
    """The pool swims measure water temperature -- do not strip those."""
    swim = next(r for r in history_rows if r["type"] == 14)
    summary = workouts.normalise(swim)["summary"]
    assert 20 < summary["avg_temperature"] < 40
