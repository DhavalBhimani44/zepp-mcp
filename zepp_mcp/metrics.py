"""The metric catalogue: what each exposed number is, where it comes from,
and how sure this project is about it.

This is data, not documentation. `zepp_describe_schema` serves it to the
model at call time, and docs/capability-matrix.md is the human-readable
companion. When a decoder changes, the entry here changes with it.

Verification levels (docs/capability-matrix.md):

  1  live verified        confirmed against live API responses
  2  fixture verified     confirmed against a recorded real response, by a
                          second independent quantity in the corpus
  3  corroborated         supported by another credible implementation only
  4  hypothesis           plausible, awaiting a capture
  5  unsupported          probed; not accessible or unresolved
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Metric:
    name: str
    category: str
    unit: str
    endpoint: str
    source_field: str
    tool: str
    level: int
    kind: str = "measured"  # "measured" | "derived" | "score"
    frequency: str = "per day"
    evidence: str = ""
    limitations: str = ""
    sports: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if not self.sports:
            out.pop("sports")
        return out


_V2 = "GET /v2/users/me/events"
_V1 = "GET /users/{uid}/events"
_V1D = "GET /users/{uid}/events/dateString"
_BAND = "GET /v1/data/band_data.json"
_HIST = "GET /v1/sport/run/history.json"
_DETAIL = "GET /v1/sport/run/detail.json"

CATALOGUE: tuple[Metric, ...] = (
    # -- daily activity ---------------------------------------------------
    Metric(
        "steps",
        "activity",
        "steps",
        _BAND,
        "summary.stp.ttl",
        "zepp_daily_summary",
        2,
        evidence="equals DailyHealth totalSteps and the per-minute sum on every day",
    ),
    Metric(
        "steps_per_hour",
        "activity",
        "steps",
        _BAND,
        "data (byte 2 of 3 per minute)",
        "zepp_daily_summary(include_hourly)",
        2,
        frequency="per minute",
        evidence="per-minute bytes sum to ttl on 8/8 days and reproduce 97/97 "
        "step stages",
    ),
    Metric(
        "distance_metres",
        "activity",
        "metres",
        _BAND,
        "summary.stp.dis",
        "zepp_daily_summary",
        2,
    ),
    Metric(
        "active_calories_kcal",
        "activity",
        "kcal",
        _BAND,
        "summary.stp.cal",
        "zepp_daily_summary",
        2,
        evidence="equals DailyHealth totalCalories; ~250-500 on rest days, so "
        "active not total",
    ),
    Metric(
        "activity_periods",
        "activity",
        "minutes/steps by mode",
        _BAND,
        "summary.stp.stage[]",
        "zepp_daily_summary",
        2,
        evidence="stage totals reproduce from per-minute steps",
        limitations="mode names for 1 and 7 unknown; 3/4 named from cadence and speed",
    ),
    Metric(
        "daily_goals",
        "activity",
        "steps / kcal / minutes",
        _V2,
        "DailyHealth/summary",
        "zepp_health_metrics(daily_goals)",
        2,
        evidence="totals equal band_data on every shared day",
    ),
    Metric(
        "active_minutes",
        "activity",
        "minutes",
        _V2,
        "DailyHealth totalBurningDuration",
        "zepp_health_metrics(daily_goals)",
        2,
        limitations="Zepp's 'fat burning' minutes; definition not published",
    ),
    # -- sleep -------------------------------------------------------------
    Metric(
        "sleep_stages",
        "sleep",
        "minutes",
        _BAND,
        "summary.slp.lt/dp/dt/wk + stage[]",
        "zepp_sleep",
        2,
        evidence="stage list recomputes all four totals on 3+ nights",
    ),
    Metric(
        "sleep_score",
        "sleep",
        "0-100",
        _BAND,
        "summary.slp.ss",
        "zepp_sleep",
        2,
        kind="score",
    ),
    Metric(
        "sleep_latency_minutes",
        "sleep",
        "minutes",
        _BAND,
        "summary.slp.is",
        "zepp_sleep",
        2,
        evidence="st == obt + is on 7/7 nights",
    ),
    Metric(
        "time_in_bed_minutes",
        "sleep",
        "minutes",
        _BAND,
        "summary.slp.obt/ebt",
        "zepp_sleep",
        2,
        evidence="ebt == ed + lb and st == obt + is on 7/7 nights",
    ),
    Metric(
        "sleep_efficiency_percent",
        "sleep",
        "percent",
        _BAND,
        "derived",
        "zepp_sleep",
        2,
        kind="derived",
    ),
    Metric(
        "naps", "sleep", "minutes", _BAND, "summary.slp.odd_stage[]", "zepp_sleep", 2
    ),
    Metric(
        "sleep_respiratory_rate",
        "sleep",
        "breaths/min",
        _V2,
        "RespiratoryRate/real_data within the sleep window",
        "zepp_sleep",
        2,
        kind="derived",
    ),
    Metric(
        "overnight_odi",
        "sleep",
        "events/hour",
        _V1D,
        "blood_oxygen/odi",
        "zepp_health_metrics(spo2)",
        2,
        evidence="odi == odiNum / (cost/3600); cost == sleep duration",
    ),
    Metric(
        "sleep_breathing_score",
        "sleep",
        "0-100",
        _V1D,
        "blood_oxygen/odi score",
        "zepp_health_metrics(spo2)",
        2,
        kind="score",
    ),
    # -- heart ------------------------------------------------------------
    Metric(
        "heart_rate_per_minute",
        "heart",
        "bpm",
        _BAND,
        "data_hr",
        "zepp_heart_rate",
        2,
        frequency="per minute",
    ),
    Metric(
        "resting_heart_rate",
        "heart",
        "bpm",
        _BAND,
        "summary.slp.rhr",
        "zepp_health_metrics(resting_heart_rate)",
        2,
        evidence="equals readiness sleepRHR on 6/6 nights",
    ),
    Metric(
        "hrv_rmssd_ms",
        "heart",
        "ms",
        _V2,
        "HRVRMSSD/real_data samples[].hrv",
        "zepp_health_metrics(hrv)",
        2,
        frequency="per minute",
        evidence="sleep-window mean reproduces readiness sleepHRV within 1 ms, "
        "6/6 nights",
    ),
    Metric(
        "hrv_sdnn_ms",
        "heart",
        "ms",
        _V2,
        "hrv_sdnn/real_data samples[].sdnn",
        "zepp_health_metrics(hrv)",
        4,
        frequency="a few per day",
        limitations="unit from field name and range only; nothing to cross-check",
    ),
    Metric(
        "lactate_threshold",
        "heart",
        "bpm, s/km",
        _V2,
        "LactateThreshold/summary",
        "zepp_training_thresholds",
        1,
    ),
    Metric(
        "vo2_max",
        "fitness",
        "ml/kg/min",
        _HIST,
        "VO2_max",
        "zepp_training_thresholds",
        2,
        limitations="per-workout copy of a rolling estimate; the dedicated "
        "WatchSportStatistics/VO2_MAX route returns HTTP 500",
    ),
    # -- readiness / recovery ----------------------------------------------
    Metric(
        "readiness_score",
        "recovery",
        "0-100",
        _V2,
        "readiness/watch_score rdnsScore",
        "zepp_health_metrics(readiness)",
        2,
        kind="score",
        evidence="same records carry sleepRHR/sleepHRV that reproduce band_data "
        "and HRVRMSSD",
    ),
    Metric(
        "readiness_subscores",
        "recovery",
        "0-100",
        _V2,
        "hrvScore/rhrScore/skinTempScore/ahiScore",
        "zepp_health_metrics(readiness)",
        2,
        kind="score",
        limitations="255 = not computed; phy/ment/afib never computed in corpus",
    ),
    Metric(
        "skin_temperature_offset",
        "recovery",
        "unknown",
        _V2,
        "readiness skinTempCalibrated",
        "zepp_health_metrics(readiness)",
        4,
        limitations="unit unknown; exposed as *_raw_unverified",
    ),
    Metric(
        "body_charge",
        "recovery",
        "0-100",
        _V2,
        "Charge/real_data samples[].total",
        "zepp_health_metrics(body_charge)",
        2,
        kind="score",
        frequency="per minute",
        evidence="total == mean(mental, physical) within 1",
        limitations="mental/physical components unverified; 255 = no score",
    ),
    Metric(
        "pai",
        "recovery",
        "PAI",
        _V1,
        "PaiHealthInfo totalPai/dailyPai",
        "zepp_health_metrics(pai)",
        2,
        kind="score",
        evidence="totalPai is the running sum of dailyPai over the first 7 days",
    ),
    # -- stress / respiration / oxygen --------------------------------------
    Metric(
        "stress",
        "stress",
        "0-100",
        _V1,
        "all_day_stress data[]",
        "zepp_health_metrics(stress)",
        2,
        kind="score",
        frequency="~every 5 min",
        evidence="min/max/avg and zone proportions recompute exactly from samples",
    ),
    Metric(
        "single_stress",
        "stress",
        "0-100",
        _V1,
        "single_stress value",
        "zepp_health_metrics(stress)",
        2,
        kind="score",
        frequency="on demand",
    ),
    Metric(
        "respiratory_rate",
        "respiration",
        "breaths/min",
        _V2,
        "RespiratoryRate/real_data measurements",
        "zepp_health_metrics(respiratory_rate)",
        2,
        frequency="per minute (mostly overnight)",
        evidence="first non-zero minute aligns with band_data sleep onset",
    ),
    Metric(
        "spo2_spot",
        "oxygen",
        "percent",
        _V1,
        "blood_oxygen/click extra.spo2",
        "zepp_health_metrics(spo2)",
        2,
        frequency="on demand",
    ),
    # -- body -------------------------------------------------------------
    Metric(
        "weight_kg",
        "body",
        "kg",
        "GET /users/{uid}/members/-1/weightRecords",
        "summary.weight",
        "zepp_body_composition",
        2,
    ),
    Metric(
        "body_composition",
        "body",
        "percent/kg/kcal",
        "GET /users/{uid}/members/-1/weightRecords",
        "summary.fatRate ...",
        "zepp_body_composition",
        3,
        evidence="AlexxIT/SmartScaleConnect capture",
    ),
    Metric(
        "profile",
        "body",
        "cm/kg",
        "GET /huami.health.getUserInfo.json",
        "height/weight/birthday/gender",
        "zepp_profile",
        2,
    ),
    # -- workouts ---------------------------------------------------------
    Metric(
        "moving_time_seconds",
        "workout",
        "seconds",
        _HIST,
        "exerciseTimeWithMillis",
        "zepp_list_workouts",
        2,
        frequency="per workout",
        evidence="total == moving + paused on 8/8 workouts",
    ),
    Metric(
        "distance_metres_workout",
        "workout",
        "metres",
        _HIST,
        "highPrecisionDistance / dis",
        "zepp_list_workouts",
        2,
        frequency="per workout",
    ),
    Metric(
        "derived_pace",
        "workout",
        "s/km or s/100m",
        _HIST,
        "derived",
        "zepp_list_workouts",
        2,
        kind="derived",
        frequency="per workout",
    ),
    Metric(
        "heart_rate_zones",
        "workout",
        "seconds",
        _HIST,
        "heart_range",
        "zepp_list_workouts",
        2,
        frequency="per workout",
    ),
    Metric(
        "training_effect",
        "workout",
        "0-5",
        _HIST,
        "te / anaerobic_te",
        "zepp_list_workouts",
        2,
        kind="score",
        frequency="per workout",
    ),
    Metric(
        "exercise_load",
        "workout",
        "Zepp load units",
        _HIST,
        "exercise_load",
        "zepp_list_workouts",
        2,
        kind="score",
        frequency="per workout",
    ),
    Metric(
        "swolf",
        "workout",
        "swolf",
        _HIST,
        "swolf",
        "zepp_list_workouts",
        2,
        frequency="per workout",
        sports=("pool_swimming",),
    ),
    Metric(
        "swim_laps",
        "workout",
        "per length",
        _DETAIL,
        "lap cols 1/13/14",
        "zepp_workout_detail",
        2,
        frequency="per length",
        sports=("pool_swimming",),
    ),
    Metric(
        "running_dynamics",
        "workout",
        "ms / mm / %",
        _HIST,
        "averageGct/averageVo/avgVertStrideRatio",
        "zepp_list_workouts",
        2,
        frequency="per workout",
        sports=("outdoor_running",),
    ),
    Metric(
        "strength_sets",
        "workout",
        "reps",
        _HIST,
        "strength_training_group",
        "zepp_list_workouts",
        2,
        frequency="per set",
        evidence="matches detail lap rows 19/19 in reps and exercise code",
        sports=("strength_training", "rope_skipping"),
    ),
    Metric(
        "strength_set_detail",
        "workout",
        "s / bpm / reps",
        _DETAIL,
        "lap cols 1/4/5/21/22/28",
        "zepp_workout_detail",
        2,
        frequency="per set",
        limitations="column 21 (load) unit unverified; exercise codes unnamed",
        sports=("strength_training",),
    ),
    Metric(
        "km_splits",
        "workout",
        "seconds per km",
        _DETAIL,
        "kilo_pace",
        "zepp_workout_detail(splits)",
        2,
        frequency="per km",
        limitations="HR/stride/cadence columns corroborated on one split only; "
        "the geohash column is never returned",
    ),
    Metric(
        "gait",
        "workout",
        "cm / steps per minute",
        _DETAIL,
        "gait cols 2/3",
        "zepp_workout_detail(streams)",
        2,
        frequency="per second",
        evidence="mean/max match the row's stride and cadence",
    ),
    Metric(
        "dem_elevation",
        "workout",
        "metres",
        _HIST,
        "add_info.adjust_dem_altitude",
        "zepp_list_workouts",
        2,
        frequency="per workout",
        sports=("hiking",),
    ),
    Metric(
        "workout_aggregates",
        "workout",
        "various",
        _HIST,
        "derived",
        "zepp_workout_stats",
        2,
        kind="derived",
        frequency="per period",
    ),
    Metric(
        "sport_codes",
        "workout",
        "name per code",
        _HIST,
        "type",
        "zepp_list_workouts",
        2,
        frequency="per workout",
        evidence="9 confirmed here; the rest ranked corroborated / reported / "
        "catalogued / contested in sports.py",
        limitations="catalogued names rest on the cloud-code == watch-id rule "
        "(verified 3/3 here for codes >= 18), not on cloud captures",
    ),
    # -- discovered, not exposed -------------------------------------------
    Metric(
        "second_heart_rate_files",
        "heart",
        "file index",
        "GET /users/me/fileInfo/events",
        "second_heart_rate/real_data",
        "zepp_raw_request",
        4,
        limitations="the index decodes; the route that serves the files is unknown",
    ),
    Metric(
        "body_charge_stress_blob",
        "recovery",
        "unknown",
        _V2,
        "Charge/stress_data stressInfo",
        "zepp_raw_request",
        5,
        limitations="base64 binary, layout unknown",
    ),
    Metric(
        "training_load_rolling",
        "fitness",
        "unknown",
        "GET /v2/watch/users/{uid}/WatchSportStatistics/SPORT_LOAD",
        "-",
        "zepp_raw_request",
        5,
        limitations="HTTP 500 with no parameters",
    ),
)

BY_NAME: dict[str, Metric] = {m.name: m for m in CATALOGUE}


def describe() -> list[dict[str, Any]]:
    return [m.as_dict() for m in CATALOGUE]
