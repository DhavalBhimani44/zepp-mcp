"""Daily, workout-row, workout-detail and analytics expansions.

Same rule as test_decode.py: every assertion is anchored to a quantity Zepp
reported elsewhere in the same corpus.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from zepp_mcp import analysis, body, decode, workouts
from zepp_mcp.client import parse_rows

FIXTURES = Path(__file__).parent / "fixtures" / "zepp"
IST = dt.timezone(dt.timedelta(seconds=19800))


def load(name: str):
    return json.loads((FIXTURES / name).read_text())["body_parsed"]


@pytest.fixture(scope="module")
def band_rows():
    return load("014_query_type_detail.json")["data"]


@pytest.fixture(scope="module")
def rows():
    return parse_rows(load("040_workout_history_run.json"))


@pytest.fixture(scope="module")
def normalised(rows):
    return [workouts.normalise(r) for r in rows]


def by_track(items, track_id):
    return next(
        i for i in items if str(i.get("track_id") or i.get("trackid")) == track_id
    )


# -- daily -------------------------------------------------------------------


def test_per_minute_steps_sum_to_the_daily_total(band_rows):
    for row in band_rows:
        minutes = decode.decode_activity_minutes(row["data"])
        assert len(minutes) == 1440
        total = decode.decode_band_summary(row["summary"])["stp"]["ttl"]
        assert sum(m["steps"] for m in minutes) == total
        assert sum(decode.hourly_steps(row["data"])) == total


def test_every_step_stage_reproduces_from_per_minute_steps(band_rows):
    checked = 0
    for row in band_rows:
        steps = [m["steps"] for m in decode.decode_activity_minutes(row["data"])]
        for stage in decode.decode_band_summary(row["summary"])["stp"]["stage"]:
            assert sum(steps[stage["start"] : stage["stop"] + 1]) == stage["step"]
            checked += 1
    assert checked == 97


def test_running_stage_has_running_cadence(band_rows):
    day = decode.summarise_day(band_rows[0])
    running = day["activity_periods"]["running"]
    assert 140 <= running["steps_per_minute"] <= 190
    assert day["activity_periods"]["walking"]["steps_per_minute"] > 80


def test_rundist_is_no_longer_labelled_run_distance(band_rows):
    """runDist is ~80% of distance even on days with no run at all."""
    for row in band_rows:
        day = decode.summarise_day(row)
        assert "run_distance_metres" not in day
        ratio = day["runDist_raw_unverified"] / day["distance_metres"]
        assert 0.75 < ratio < 0.9


def test_bad_activity_blob_is_rejected():
    assert decode.decode_activity_minutes("AAAA") == [
        {"kind_raw": 0, "intensity_raw": 0, "steps": 0}
    ]  # 3 bytes, one minute
    assert decode.decode_activity_minutes("AAE=") is None  # 2 bytes
    assert decode.hourly_steps("%%") is None


# -- sleep window ------------------------------------------------------------


def test_bed_window_identities_hold_on_every_night(band_rows):
    checked = 0
    for row in band_rows:
        slp = decode.decode_band_summary(row["summary"])["slp"]
        if slp["ed"] <= slp["st"]:
            continue
        assert (
            slp["lt"] + slp["dp"] + slp["dt"] + slp["wk"]
            == (slp["ed"] - slp["st"]) // 60
        )
        block = decode.summarise_day(row)["sleep"]
        in_bed = dt.datetime.fromisoformat(block["in_bed_local"])
        onset = dt.datetime.fromisoformat(block["start_local"])
        assert (onset - in_bed).total_seconds() // 60 == block["sleep_latency_minutes"]
        out_of_bed = dt.datetime.fromisoformat(block["out_of_bed_local"])
        wake = dt.datetime.fromisoformat(block["end_local"])
        assert (out_of_bed - wake).total_seconds() // 60 == slp["lb"]
        assert 0 < block["sleep_efficiency_percent"] <= 100
        assert block["sleep_efficiency_method"].startswith("derived")
        checked += 1
    assert checked == 7


def test_unrecorded_night_gets_no_bed_window(band_rows):
    row = next(r for r in band_rows if r["date_time"] == "2026-08-13")
    sleep = decode.summarise_day(row)["sleep"]
    assert "time_in_bed_minutes" not in sleep


# -- workout rows ------------------------------------------------------------


def test_total_time_is_moving_plus_paused(normalised):
    for item in normalised:
        s = item["summary"]
        assert s["total_time_seconds"] == pytest.approx(
            s["moving_time_seconds"] + s["paused_seconds"], abs=0.1
        )


def test_moving_time_matches_run_time(rows, normalised):
    for row, item in zip(rows, normalised, strict=True):
        # run_time truncates; moving time is rounded to 0.1 s.
        assert item["summary"]["moving_time_seconds"] == pytest.approx(
            int(row["run_time"]), abs=1
        )


def test_high_precision_distance(normalised):
    walk = by_track(normalised, "1786631538")
    assert walk["summary"]["distance_metres"] == 1064.37


def test_derived_pace_is_distance_over_moving_time(normalised):
    run = by_track(normalised, "1786191738")
    assert run["derived"]["pace_sec_per_km"] == pytest.approx(
        run["summary"]["moving_time_seconds"]
        / run["summary"]["distance_metres"]
        * 1000,
        abs=0.1,
    )
    # Agrees with Zepp's own avgEquivPace (354 s/km) to within a second.
    assert run["derived"]["pace_sec_per_km"] == pytest.approx(
        run["foot"]["avg_pace_sec_per_km"], abs=1.5
    )
    swim = by_track(normalised, "1786761306")
    assert "pace_sec_per_100m" in swim["derived"]
    strength = by_track(normalised, "1786714291")
    assert "derived" not in strength


def test_strength_sets_from_the_index_row(normalised):
    sets = by_track(normalised, "1786714291")["strength"]["sets"]
    assert sets["sets_recorded"] == 19
    assert sets["total_reps"] == 201
    assert sets["by_exercise_code"]["109"] == {"sets": 4, "reps": 45}
    assert sets["rest_between_sets_seconds"] == pytest.approx(3650.9)


def test_stale_strength_groups_on_swims_are_ignored(normalised):
    swim = by_track(normalised, "1786761306")
    assert "sets" not in (swim.get("swim") or {})
    assert "strength" not in swim


def test_dem_elevation_kept_separate_from_barometric(normalised):
    hike = by_track(normalised, "1786267428")
    assert hike["dem_elevation"]["elevation_gain_metres"] == 359.6
    assert hike["hike"]["elevation_gain_metres"] == pytest.approx(279.61)


# -- workout detail ----------------------------------------------------------


def test_strength_laps_match_index_row_sets(rows):
    detail = load("042_workout_detail_type52_1786714291.json")["data"]
    decoded = decode.decode_detail(detail, {"laps"})
    assert "laps" not in decoded
    sets = decoded["sets"]["sets"]
    groups = json.loads(by_track(rows, "1786714291")["strength_training_group"])
    assert [s["reps"] for s in sets] == [g["count"] for g in groups]
    assert [s["exercise_code"] for s in sets] == [g["actionType"] for g in groups]
    assert sets[-1]["elapsed_seconds_cumulative"] == pytest.approx(4585, abs=2)
    assert sets[1]["rest_before_seconds_derived"] == 365 - 45 - 187


def test_swim_detail_still_uses_swim_lap_columns():
    detail = load("041_workout_detail_type14_1786761306.json")["data"]
    decoded = decode.decode_detail(detail, {"laps"})
    assert "laps" in decoded and "sets" not in decoded


def test_gait_matches_the_walks_own_stride_and_cadence(rows):
    detail = load("043_workout_detail_type8_1786631538.json")["data"]
    walk = by_track(rows, "1786631538")
    gait = decode.decode_gait(detail["gait"])
    assert gait["duration_seconds"] == int(walk["run_time"])
    assert gait["stride_length_cm"]["avg"] == pytest.approx(
        walk["avg_stride_length"], abs=1
    )
    assert gait["cadence_spm"]["max"] == walk["max_frequency"]
    assert gait["cadence_spm"]["avg"] == pytest.approx(
        float(walk["avg_frequency"]), abs=2.5
    )


def test_splits_never_carry_the_geohash():
    detail = load("043_workout_detail_type8_1786631538.json")["data"]
    decoded = decode.decode_detail(detail, {"splits", "streams", "laps", "summary"})
    km = decoded["splits"]["km"]["splits"][0]
    assert km["split_seconds"] == 855
    assert km["avg_heart_rate_bpm"] == 125
    text = json.dumps(decoded)
    assert "geohash" not in text and detail["kilo_pace"].split(",")[2] not in text
    assert "kilo_pace" not in decoded["streams"]


def test_synthetic_split_rows_drop_column_two():
    synthetic = (
        "0,300,u4pruydqqvj,-1,150,300,300000,150,0,110,0,0,0,170,0;"
        "1,290,u4pruydqqvk,-1,155,590,590000,155,0,112,0,0,0,172,0"
    )
    decoded = decode.decode_splits(synthetic)
    assert decoded["fastest_split_index"] == 1
    assert "u4pr" not in json.dumps(decoded)


def test_pauses_reconcile_with_the_row(rows):
    detail = load("041_workout_detail_type14_1786761306.json")["data"]
    pauses = decode.decode_pauses(detail["pause"], IST)
    paused_ms = by_track(rows, "1786761306")["pauseTimeWithMillis"]
    assert pauses["total_seconds"] == pytest.approx(paused_ms / 1000, abs=1.5)


def test_gait_rejects_unknown_shape():
    assert decode.decode_gait("1,2;3,4")["encoding"] == "unknown"


# -- analysis ----------------------------------------------------------------


def test_period_pace_is_total_distance_over_total_time(normalised):
    walks = [w for w in normalised if w["sport"] == "walking"]
    totals = analysis.totals(walks)
    expected = (
        walks[0]["summary"]["moving_time_seconds"]
        / walks[0]["summary"]["distance_metres"]
        * 1000
    )
    assert totals["avg_pace_sec_per_km"] == pytest.approx(expected, abs=0.1)


def test_mixed_sports_get_no_aggregate_pace(normalised):
    assert "avg_pace" not in analysis.totals(normalised)


def test_aggregate_pace_is_not_the_mean_of_paces():
    def workout(distance, seconds):
        return {
            "sport": "outdoor_running",
            "sport_family": "running",
            "start_local": "2026-08-01T07:00:00",
            "summary": {"distance_metres": distance, "moving_time_seconds": seconds},
        }

    # 10 km in 3000 s and 1 km in 600 s: mean of paces is 450 s/km, the
    # correct aggregate is 3600 / 11 = 327.3 s/km.
    totals = analysis.totals([workout(10000, 3000), workout(1000, 600)])
    assert totals["avg_pace_sec_per_km"] == pytest.approx(327.3, abs=0.1)


def test_grouping_by_month_and_sport(normalised):
    months = analysis.grouped(normalised, "month")
    assert [m["group"] for m in months] == ["2026-08"]
    assert months[0]["workouts"] == len(normalised)
    sports = {g["group"]: g for g in analysis.grouped(normalised, "sport")}
    assert sports["pool_swimming"]["workouts"] == 2


def test_records_and_flagged_bests(normalised):
    records = analysis.records(normalised)["within_range"]
    assert records["pool_swimming"]["best_swolf"]["value"] > 0
    assert records["strength_training"]["most_reps"]["value"] == 201
    flagged = records["zepp_flagged_personal_bests"]
    assert flagged["run_3_km"]["value"] == 1067
    assert flagged["swim_furthest_m"]["value"] == 756


def test_series_and_trend():
    def workout(day, swolf):
        return {
            "sport": "pool_swimming",
            "start_local": f"2026-08-{day:02d}T07:00:00",
            "track_id": str(day),
            "swim": {"swolf": swolf},
        }

    result = analysis.series([workout(1, 42), workout(8, 40), workout(15, 38)], "swolf")
    assert result["best"]["value"] == 38
    assert result["trend"]["change_per_30_days"] == pytest.approx(-8.571, abs=0.01)
    assert result["lower_is_better"] is True


def test_trend_needs_three_points():
    assert analysis.linear_trend([(dt.date(2026, 1, 1), 1.0)]) is None


# -- profile -----------------------------------------------------------------


def test_profile_drops_identifying_fields():
    data = load("015_profile.json")["data"]
    profile = body.profile(data, today=dt.date(2026, 8, 15))
    assert profile["height_cm"] == 175 and profile["weight_kg"] == 70.0
    assert profile["age_years"] == 36
    assert profile["wear_hand"] == "left_hand"
    text = json.dumps(profile)
    for leaked in ("Alex", "avatar", "nick", "alarm"):
        assert leaked not in text


def test_pauses_come_back_even_when_only_streams_are_requested():
    detail = load("041_workout_detail_type14_1786761306.json")["data"]
    decoded = decode.decode_detail(detail, {"streams"})
    assert decoded["pauses"]["pause_count"] == 1
    assert "pause" not in decoded["streams"]


def test_format_pace():
    from zepp_mcp.codes import format_pace
    assert format_pace(354.1) == "5:54/km"
    assert format_pace(105.9, "100m") == "1:45/100m"
