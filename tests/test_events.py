"""Event-family decoders against the real captured corpus.

Each test anchors a decoder to a SECOND quantity Zepp reported somewhere
else -- band_data's own sleep totals, the event's own summary fields -- so a
decoder that drifts is caught by Zepp's arithmetic, not by a number typed
into a test.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import statistics
from pathlib import Path

import pytest

from zepp_mcp import decode, events

FIXTURES = Path(__file__).parent / "fixtures" / "zepp"
IST = dt.timezone(dt.timedelta(seconds=19800))


def body(name: str):
    return json.loads((FIXTURES / name).read_text())["body_parsed"]


@pytest.fixture(scope="module")
def band_sleep():
    """date -> band_data `slp` block, the independent reference."""
    rows = body("014_query_type_detail.json")["data"]
    return {
        r["date_time"]: decode.decode_band_summary(r["summary"])["slp"] for r in rows
    }


@pytest.fixture(scope="module")
def readiness():
    return events.readiness(body("030_ev2_readiness_watch_score.json"))


# -- timezones ---------------------------------------------------------------


def test_legacy_zone_alias_resolves_instead_of_falling_back_to_utc():
    """Asia/Calcutta is what readiness/HRV events carry; minimal tz databases
    lack it. Falling back to UTC would move every morning score a day back."""
    resolved = events.zone("279,Asia/Calcutta")
    assert dt.datetime(2026, 8, 8, tzinfo=dt.UTC).astimezone(
        resolved
    ).utcoffset() == dt.timedelta(hours=5, minutes=30)


def test_unknown_zone_falls_back():
    assert events.zone("Not/AZone", IST) is IST
    assert events.zone(None, IST) is IST


# -- readiness ---------------------------------------------------------------


def test_readiness_sleep_rhr_equals_band_data_sleep_rhr(readiness, band_sleep):
    checked = 0
    for record in readiness:
        slp = band_sleep.get(record["date"])
        if not slp or not slp.get("rhr"):
            continue
        assert record["sleep_resting_hr_bpm"] == slp["rhr"]
        checked += 1
    assert checked >= 6


def test_readiness_sleep_hrv_is_mean_of_rmssd_inside_sleep_window(
    readiness, band_sleep
):
    """The strongest check in the corpus: readiness `sleepHRV` reproduces
    from an entirely different payload, the per-minute HRVRMSSD stream."""
    rmssd = events.hrv_rmssd(body("035_ev2_HRVRMSSD_real_data.json"))
    checked = 0
    for record in readiness:
        slp = band_sleep.get(record["date"])
        if not slp or slp["ed"] <= slp["st"]:
            continue
        window = events.within(rmssd, slp["st"], slp["ed"], "rmssd_ms")
        assert window["samples"] > 100
        assert abs(window["avg"] - record["sleep_hrv_rmssd_ms"]) <= 1
        checked += 1
    assert checked >= 6


def test_readiness_not_computed_subscores_are_absent(readiness):
    """255 is "not computed" -- a physical score of 255 is not a score."""
    for record in readiness:
        assert "physical_score" not in record and "mental_score" not in record
        assert all(v != 255 for v in record.values() if isinstance(v, int))


def test_readiness_dates_are_local_wake_up_days(readiness):
    assert readiness[0]["date"] == "2026-08-08"
    assert readiness[0]["readiness_score"] == 92


def test_breathing_subscore_255_on_later_records_is_dropped(readiness):
    late = [r for r in readiness if r["date"] >= "2026-08-12"]
    assert late and all("sleep_breathing_score" not in r for r in late)


# -- HRV / respiratory rate -------------------------------------------------


def test_rmssd_samples_are_per_minute_and_positive():
    points = events.hrv_rmssd(body("035_ev2_HRVRMSSD_real_data.json"))
    assert len(points) > 2000
    gaps = {
        b["epoch_ms"] - a["epoch_ms"] for a, b in zip(points, points[1:], strict=False)
    }
    assert 60000 in gaps
    assert all(p["rmssd_ms"] > 0 for p in points)


def test_sdnn_samples_decode():
    points = events.hrv_sdnn(body("034_ev2_hrv_sdnn_real_data.json"))
    assert points[0]["sdnn_ms"] == 33.54
    assert all(10 < p["sdnn_ms"] < 200 for p in points)


def test_respiratory_rate_minutes_begin_at_sleep_onset(band_sleep):
    """Index 0 is the item's UTC midnight. If the indexing were off, the
    first reading of the night would not land on band_data's sleep start."""
    points = events.respiratory_rate(body("036_ev2_RespiratoryRate_real_data.json"))
    first = points[0]["epoch_ms"] // 1000
    onset = band_sleep["2026-08-08"]["st"]
    assert abs(first - onset) <= 5 * 60
    assert all(10 <= p["breaths_per_minute"] <= 25 for p in points)


def test_respiratory_rate_rollup_is_local_days():
    points = events.respiratory_rate(body("036_ev2_RespiratoryRate_real_data.json"))
    days = events.daily_rollup(points, "breaths_per_minute", IST)
    assert days[0]["date"] == "2026-08-08"
    assert 12 <= days[0]["avg"] <= 20


def test_respiratory_rate_rejects_bad_base64():
    payload = {"items": [{"timestamp": 0, "value": {"measurements": "%%%"}}]}
    assert events.respiratory_rate(payload) == []


# -- stress ------------------------------------------------------------------


def test_stress_summary_recomputes_from_its_own_samples():
    payload = body("023_ev1_all_day_stress_none.json")
    days = events.all_day_stress(payload, IST, include_samples=True)
    assert len(days) == 6
    for day in days:
        values = [s["stress"] for s in day["samples"]]
        assert min(values) == day["min"]
        assert max(values) == day["max"]
        assert round(statistics.fmean(values)) == pytest.approx(day["avg"], abs=1)
        zones = {z: 0 for z in ("relaxed", "normal", "medium", "high")}
        for value in values:
            zones[events.stress_zone(value)] += 1
        for zone, count in zones.items():
            assert round(count * 100 / len(values)) == pytest.approx(
                day["zone_percent"][zone], abs=1
            )


def test_stress_days_need_the_user_timezone():
    """v1 stress items stamp local midnight; read in UTC they land on the
    previous day. The decoder takes the zone rather than guessing it."""
    payload = body("023_ev1_all_day_stress_none.json")
    assert events.all_day_stress(payload, IST)[0]["date"] == "2026-08-07"
    assert events.all_day_stress(payload, dt.UTC)[0]["date"] == "2026-08-06"


def test_single_stress():
    readings = events.single_stress(body("026_ev1_single_stress_none.json"), IST)
    assert readings == [
        {"time": "2026-08-07T12:41+05:30", "stress": 54, "zone": "normal"}
    ]


# -- PAI ---------------------------------------------------------------------


def test_pai_total_is_running_sum_of_daily_for_first_week():
    days = events.pai(body("024_ev1_PaiHealthInfo_none.json"), IST)
    running = 0.0
    for day in days[:7]:
        running += day["pai_earned_today"]
        assert day["pai_7_day"] == pytest.approx(running, abs=0.2)


def test_pai_daily_cap_and_zone_limits():
    day = next(
        d
        for d in events.pai(body("024_ev1_PaiHealthInfo_none.json"), IST)
        if d["date"] == "2026-08-09"
    )
    assert day["pai_earned_today"] == 75.0
    assert day["zones"]["high"]["lower_limit_bpm"] == 158
    assert day["resting_hr_bpm"] == 57


# -- SpO2 --------------------------------------------------------------------


def test_odi_is_events_per_hour_of_measured_sleep(band_sleep):
    nights = events.overnight_spo2(body("028_evdate_blood_oxygen_odi.json"), IST)
    assert len(nights) == 4
    for night in nights:
        hours = night["measured_minutes"] / 60
        # Zepp truncates to two places.
        assert night["odi_events_per_hour"] == pytest.approx(
            night["desaturation_events"] / hours, abs=0.011
        )
        slp = band_sleep.get(night["date"])
        if slp:
            assert night["measured_minutes"] == (slp["ed"] - slp["st"]) // 60


def test_spo2_spot_check_reads_double_encoded_extra():
    readings = events.spo2_spot_checks(body("025_ev1_blood_oxygen_click.json"), IST)
    assert readings == [
        {"time": "2026-08-07T13:27+05:30", "spo2_percent": 99, "automatic": False}
    ]


def test_spo2_zero_is_not_a_reading():
    payload = {"items": [{"extra": json.dumps({"spo2": 0, "timestamp": 1})}]}
    assert events.spo2_spot_checks(payload) == []


# -- Body Charge -------------------------------------------------------------


def test_body_charge_reports_only_scored_days():
    """Twenty items come back; only one carries a real `total`. The others
    are an app-generated curve with total 255 and must not become days."""
    payload = body("032_ev2_Charge_real_data.json")
    assert len(payload["items"]) == 20
    days = events.body_charge(payload, IST)
    assert [d["date"] for d in days] == ["2026-08-08"]
    assert days[0]["max"] == 86 and days[0]["min"] >= 0


def test_body_charge_total_is_mean_of_components():
    days = events.body_charge(
        body("032_ev2_Charge_real_data.json"), IST, include_samples=True
    )
    for sample in days[0]["samples"]:
        mean = (sample["mental_unverified"] + sample["physical_unverified"]) / 2
        assert abs(sample["body_charge"] - mean) <= 1.1


# -- DailyHealth -------------------------------------------------------------


def test_daily_health_totals_equal_band_data():
    rows = body("014_query_type_detail.json")["data"]
    band = {
        r["date_time"]: decode.decode_band_summary(r["summary"])["stp"] for r in rows
    }
    checked = 0
    for day in events.daily_health(body("031_ev2_DailyHealth_summary.json")):
        if day["date"] in band:
            assert day["steps"] == band[day["date"]]["ttl"]
            assert day["active_calories_kcal"] == band[day["date"]]["cal"]
            checked += 1
    assert checked >= 7


def test_daily_health_running_distance_is_on_foot_workouts():
    days = {
        d["date"]: d
        for d in events.daily_health(body("031_ev2_DailyHealth_summary.json"))
    }
    assert days["2026-08-08"]["on_foot_workout_distance_metres"] == pytest.approx(
        3363.3
    )
    # The walk on 08-13, not a run, still counts.
    assert days["2026-08-13"]["on_foot_workout_distance_metres"] == pytest.approx(
        1064.4
    )
    assert days["2026-08-08"]["step_goal_met"] is False
    assert days["2026-08-09"]["step_goal_met"] is True


# -- shared helpers ----------------------------------------------------------


def test_dedupe_drops_repeated_items_across_pages():
    item = {"eventType": "x", "subType": "y", "timestamp": 1}
    assert events.dedupe([item, dict(item), {**item, "timestamp": 2}]) == [
        item,
        {**item, "timestamp": 2},
    ]


def test_second_hr_index_decodes_without_claiming_the_data():
    files = events.second_hr_files(body("022_second_hr_index.json"))
    assert files and files[0]["file_type"] == "SEC_HR"


@pytest.mark.parametrize("payload", [None, {}, {"items": None}, {"items": "x"}, []])
def test_malformed_payloads_decode_to_nothing(payload):
    for decoder in (
        events.readiness,
        events.hrv_rmssd,
        events.respiratory_rate,
        events.pai,
        events.overnight_spo2,
        events.body_charge,
        events.daily_health,
    ):
        assert decoder(payload) == []


def test_activity_blob_sanity_is_independent_of_events():
    """Guard for the per-minute step decoder that DailyHealth corroborates."""
    rows = body("014_query_type_detail.json")["data"]
    raw = base64.b64decode(rows[0]["data"])
    assert len(raw) == 1440 * 3
