"""End-to-end MCP tool tests against a mock Zepp that serves the real
fixture corpus. Each tool runs its full fetch -> decode -> aggregate path
with no network and no credentials."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from zepp_mcp import client as api
from zepp_mcp import server
from zepp_mcp.auth import Credential

FIXTURES = Path(__file__).parent / "fixtures" / "zepp"
UID = "1234567890"
RANGE = {"from_date": "2026-08-01", "to_date": "2026-08-15"}


def body(name: str):
    return json.loads((FIXTURES / name).read_text())["body_parsed"]


V2 = {
    ("readiness", "watch_score"): "030_ev2_readiness_watch_score.json",
    ("DailyHealth", "summary"): "031_ev2_DailyHealth_summary.json",
    ("Charge", "real_data"): "032_ev2_Charge_real_data.json",
    ("hrv_sdnn", "real_data"): "034_ev2_hrv_sdnn_real_data.json",
    ("HRVRMSSD", "real_data"): "035_ev2_HRVRMSSD_real_data.json",
    ("RespiratoryRate", "real_data"): "036_ev2_RespiratoryRate_real_data.json",
    ("LactateThreshold", "summary"): "039_ev2_LactateThreshold_summary.json",
}
V1 = {
    "all_day_stress": "023_ev1_all_day_stress_none.json",
    "PaiHealthInfo": "024_ev1_PaiHealthInfo_none.json",
    "blood_oxygen": "025_ev1_blood_oxygen_click.json",
    "single_stress": "026_ev1_single_stress_none.json",
}
DETAIL = {
    "1786761306": "041_workout_detail_type14_1786761306.json",
    "1786714291": "042_workout_detail_type52_1786714291.json",
    "1786631538": "043_workout_detail_type8_1786631538.json",
}

DEVICES = {
    "items": [
        {
            "deviceSource": 12345678,
            "deviceType": 0,
            "firmwareVersion": "3.1.0",
            "macAddress": "AA:BB:CC:00:11:22",
            "sn": "SECRETSERIAL",
            "deviceId": "AABBCCFFFE001122",
            "additionalInfo": {"auth_key": "0xdeadbeefdeadbeefdeadbeefdeadbeef"},
        }
    ]
}


def _events(items: list[dict[str, Any]], params: dict[str, str]) -> dict[str, Any]:
    lo, hi = int(params["from"]), int(params["to"])
    limit = int(params.get("limit", 20))
    page = [i for i in items if lo <= int(i.get("timestamp", 0)) <= hi]
    return {"items": page[:limit]}


def router(request: httpx.Request) -> httpx.Response:
    path, p = request.url.path, dict(request.url.params)
    if path == "/v1/data/band_data.json":
        rows = [
            r
            for r in body("014_query_type_detail.json")["data"]
            if p["from_date"] <= r["date_time"] <= p["to_date"]
        ]
        return httpx.Response(200, json={"code": 1, "data": rows})
    if path == "/v1/sport/run/history.json":
        return httpx.Response(200, json=body("040_workout_history_run.json"))
    if path == "/v1/sport/run/detail.json":
        return httpx.Response(200, json=body(DETAIL[p["trackid"]]))
    if path == "/v2/users/me/events":
        name = V2.get((p["eventType"], p["subType"]))
        if not name:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json=_events(body(name)["items"], p))
    if path == f"/users/{UID}/events":
        name = V1.get(p["eventType"])
        return httpx.Response(
            200, json=_events(body(name)["items"], p) if name else {"items": []}
        )
    if path == f"/users/{UID}/events/dateString":
        return httpx.Response(200, json=body("028_evdate_blood_oxygen_odi.json"))
    if path == "/huami.health.getUserInfo.json":
        return httpx.Response(200, json=body("015_profile.json"))
    if path == f"/users/{UID}/devices":
        return httpx.Response(200, json=DEVICES)
    if "WatchSportStatistics" in path:
        return httpx.Response(500, json={"code": -50000})
    return httpx.Response(404, json={"error": "Not Found"})


@pytest.fixture(autouse=True)
def fake_zepp(monkeypatch):
    monkeypatch.setenv("ZEPP_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("ZEPP_TOKEN_CACHE", "off")
    c = api.ZeppClient(
        "u@example.com",
        "pw",
        http=httpx.Client(transport=httpx.MockTransport(router)),
        min_interval=0,
    )
    c._credential = Credential(
        "tok", UID, "api-mifit.example", "US", time.time() + 864000
    )
    monkeypatch.setattr(server, "_client", c)
    return c


# -- input handling ----------------------------------------------------------


def test_bad_dates_are_explained_not_raised():
    out = server.zepp_list_workouts(from_date="15/08/2026", to_date="2026-08-20")
    assert out["status"] == "error" and "YYYY-MM-DD" in out["error"]
    out = server.zepp_health_metrics(
        "pai", from_date="2026-08-10", to_date="2026-08-01"
    )
    assert out["status"] == "error" and "after" in out["error"]


# -- existing tools keep working --------------------------------------------


def test_daily_summary_with_hourly(fake_zepp):
    out = server.zepp_daily_summary(include_hourly=True, **RANGE)
    assert out["status"] == "ok" and out["day_count"] == 8
    day = out["days"][0]
    assert sum(day["hourly_steps"]) == day["steps"]
    assert out["totals"]["steps"] == sum(d["steps"] for d in out["days"])


def test_single_night_sleep_is_enriched():
    night = server.zepp_sleep(date="2026-08-09")
    assert night["status"] == "ok"
    assert night["readiness"]["sleep_hrv_rmssd_ms"] == 29
    assert night["readiness"]["sleep_resting_hr_bpm"] == night["resting_heart_rate_bpm"]
    assert 12 <= night["respiratory_rate_breaths_per_minute"]["avg"] <= 20
    assert night["overnight_spo2"]["desaturation_events"] == 1


def test_sleep_range_reports_missing_nights_separately():
    out = server.zepp_sleep(enrich=False, **RANGE)
    assert out["nights_recorded"] == 7 and out["nights_missing"] == 1
    assert out["averages"]["rem_minutes"] > 0
    assert out["totals"]["deep_minutes"] == sum(
        n.get("deep_minutes") or 0 for n in out["nights"] if n["status"] == "ok"
    )


def test_list_workouts_pages_with_offset():
    first = server.zepp_list_workouts(limit=3, **RANGE)
    assert first["total"] == 8 and first["returned"] == 3 and first["next_offset"] == 3
    second = server.zepp_list_workouts(limit=3, offset=3, **RANGE)
    ids = {w["track_id"] for w in first["workouts"]} & {
        w["track_id"] for w in second["workouts"]
    }
    assert not ids
    assert server.zepp_list_workouts(sport="52", **RANGE)["total"] == 2


def test_workout_detail_default_sections():
    out = server.zepp_workout_detail("1786631538", "run.12345678.huami.com")
    assert out["status"] == "ok" and "splits" in out
    out = server.zepp_workout_detail("1786714291", "run.12345678.huami.com")
    assert out["sets"]["set_count"] == 19


def test_training_thresholds_degrades_without_estimates():
    out = server.zepp_training_thresholds(**RANGE)
    assert out["status"] == "no_data"


# -- new tools ---------------------------------------------------------------


@pytest.mark.parametrize(
    "metric,key",
    [
        ("readiness", "readiness_score"),
        ("hrv", "overnight_rmssd_ms"),
        ("resting_heart_rate", "resting_heart_rate_bpm"),
        ("respiratory_rate", "avg"),
        ("stress", "avg"),
        ("spo2", "odi_events_per_hour"),
        ("pai", "pai_7_day"),
        ("body_charge", "max"),
        ("daily_goals", "steps"),
    ],
)
def test_every_health_metric_returns_dated_days(metric, key):
    out = server.zepp_health_metrics(metric, **RANGE)
    assert out["status"] == "ok", out
    assert out["days"], metric
    assert all("2026-08-01" <= d["date"] <= "2026-08-15" for d in out["days"])
    assert any(key in d for d in out["days"])
    assert out["source"]["verification_level"] in (1, 2, 3, 4)


def test_health_metric_trend_and_summary():
    out = server.zepp_health_metrics("resting_heart_rate", **RANGE)
    assert out["summary"]["field"] == "resting_heart_rate_bpm"
    assert out["trend"]["method"].startswith("derived")


def test_health_metric_samples_are_capped(monkeypatch):
    monkeypatch.setattr(server, "_SAMPLE_CAP", 10)
    out = server.zepp_health_metrics("hrv", include_samples=True, **RANGE)
    assert len(out["samples"]) == 10 and out["samples_truncated"] is True


def test_health_metric_outside_corpus_is_no_data():
    out = server.zepp_health_metrics(
        "pai", from_date="2025-01-01", to_date="2025-01-31"
    )
    assert out["status"] == "no_data" and "Not proof" in out["note"]


def test_workout_stats():
    out = server.zepp_workout_stats(group_by="week", metric="swolf", **RANGE)
    assert out["status"] == "ok"
    assert out["overall"]["workouts"] == 8
    assert {g["group"] for g in out["by_sport"]} >= {"pool_swimming", "hiking"}
    assert out["by_period"]["periods"]
    assert [v["value"] for v in out["series"]["values"]]
    assert out["records"]["within_range"]["outdoor_running"][
        "fastest_avg_pace_sec_per_km"
    ]


def test_workout_stats_single_sport_pace():
    out = server.zepp_workout_stats(sport="hiking", **RANGE)
    assert out["overall"]["avg_pace_method"].startswith("derived")


def test_period_summary_sections():
    out = server.zepp_period_summary(**RANGE)
    assert out["status"] == "ok"
    assert out["activity"]["days_with_data"] == 8
    assert out["sleep"]["nights_recorded"] == 7
    assert out["training"]["overall"]["workouts"] == 8
    assert out["recovery"]["overnight_hrv_rmssd_ms"]["avg"] > 0
    assert out["recovery"]["pai"]["latest_7_day"] > 0


def test_period_summary_survives_a_failing_section(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("history down")

    monkeypatch.setattr(server, "_workouts", boom)
    out = server.zepp_period_summary(**RANGE)
    assert out["status"] == "partial" and out["failed_sections"] == ["training"]
    assert out["activity"]["days_with_data"] == 8


def test_profile_never_returns_device_secrets_or_identity():
    out = server.zepp_profile()
    assert out["status"] == "ok" and out["height_cm"] == 175
    text = json.dumps(out)
    for secret in ("deadbeef", "SECRETSERIAL", "AA:BB:CC", "AABBCCFFFE001122", "Alex"):
        assert secret not in text
    assert out["devices"]["devices"][0]["firmwareVersion"] == "3.1.0"
    assert "additionalInfo" in out["devices"]["omitted_field_names"]


def test_describe_schema_lists_catalogue():
    out = server.zepp_describe_schema()
    names = {m["name"] for m in out["metrics"]}
    assert {"hrv_rmssd_ms", "readiness_score", "strength_sets"} <= names
    assert out["verification_levels"]["2"].startswith("fixture verified")


def test_sport_family_filter_and_confidence_reporting():
    out = server.zepp_list_workouts(sport="swimming", **RANGE)
    assert out["total"] == 2
    seen = out["sports_seen"]
    assert seen == [{"code": 14, "sport": "pool_swimming", "family": "swimming",
                     "confidence": "confirmed", "workouts": 2}]


def test_workout_stats_by_family():
    out = server.zepp_workout_stats(group_by="sport_family", **RANGE)
    families = {g["group"] for g in out["by_sport_family"]}
    assert {"swimming", "strength", "hiking", "walking", "running"} <= families
    assert "by_period" not in out


def test_describe_schema_lists_every_sport_code():
    codes = server.zepp_describe_schema()["sport_codes"]
    assert len(codes["codes"]) >= 140
    assert codes["count_by_confidence"]["confirmed"] == 9


def test_timezone_names_never_degrade_to_a_bare_offset_when_avoidable(monkeypatch):
    import datetime as dt
    monkeypatch.delenv("ZEPP_TIMEZONE", raising=False)
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert server._tz_name(server._user_tz()) == "Europe/Berlin"
    plus5 = dt.timezone(dt.timedelta(hours=5))
    assert server._tz_name(plus5) == "Etc/GMT-5"
    assert server._tz_name(dt.timezone(dt.timedelta(hours=-3))) == "Etc/GMT+3"
    assert server._tz_name(dt.UTC) == "UTC"
    assert server._tz_name(dt.timezone(dt.timedelta(hours=5, minutes=30))) == "+0530"


def test_zepp_timezone_wins_over_the_system(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    monkeypatch.setenv("ZEPP_TIMEZONE", "Asia/Calcutta")
    assert server._tz_name(server._user_tz()) == "Asia/Kolkata"


@pytest.mark.parametrize("metric", ["stress", "body_charge"])
def test_nested_day_samples_are_capped_too(monkeypatch, metric):
    monkeypatch.setattr(server, "_SAMPLE_CAP", 50)
    out = server.zepp_health_metrics(metric, include_samples=True, **RANGE)
    kept = sum(len(d.get("samples", [])) for d in out["days"])
    assert kept <= 50
    assert out["samples_truncated"] is True and out["samples_total"] > 50
    # The newest day keeps its samples; summaries survive on every day.
    assert out["days"][-1]["samples"]
    assert all("min" in d or "avg" in d for d in out["days"])


def test_average_steps_ignore_days_without_a_count(monkeypatch):
    from zepp_mcp import decode
    real = decode.summarise_day

    def missing_first(row):
        day = real(row)
        if day["date"] == "2026-08-08":
            day["steps"] = None
        return day

    monkeypatch.setattr(decode, "summarise_day", missing_first)
    out = server.zepp_daily_summary(**RANGE)
    counted = [d["steps"] for d in out["days"] if d["steps"] is not None]
    assert out["totals"]["days_with_steps"] == len(counted) == 7
    assert out["totals"]["avg_daily_steps"] == int(sum(counted) / len(counted))
