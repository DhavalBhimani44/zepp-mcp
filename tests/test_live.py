"""Opt-in live checks against a real account. Never run in CI.

    ZEPP_LIVE=1 uv run pytest tests/test_live.py

Uses ZEPP_EMAIL / ZEPP_PASSWORD from .env and the cached token, so a run
costs at most one login. Each tool is called once over the last 14 days and
must answer with a recognised status -- the point is to catch a changed
response shape, not to assert anything about the account's data. Nothing
here prints a value.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ZEPP_LIVE") != "1",
    reason="live API checks are opt-in: set ZEPP_LIVE=1",
)

OK = {"ok", "no_data", "partial"}


@pytest.fixture(scope="module")
def window():
    end = dt.date.today()
    return {"from_date": (end - dt.timedelta(days=14)).isoformat(),
            "to_date": end.isoformat()}


def test_auth_status():
    from zepp_mcp import server
    assert server.zepp_auth_status()["status"] == "ok"


@pytest.mark.parametrize("tool", [
    "zepp_daily_summary", "zepp_sleep", "zepp_list_workouts",
    "zepp_workout_stats", "zepp_period_summary", "zepp_training_thresholds",
])
def test_range_tools_answer(tool, window):
    from zepp_mcp import server
    assert getattr(server, tool)(**window)["status"] in OK


@pytest.mark.parametrize("metric", [
    "readiness", "hrv", "resting_heart_rate", "respiratory_rate", "stress",
    "spo2", "pai", "body_charge", "daily_goals",
])
def test_health_metrics_answer(metric, window):
    from zepp_mcp import server
    assert server.zepp_health_metrics(metric, **window)["status"] in OK


def test_profile_answers_without_secrets():
    from zepp_mcp import server
    out = server.zepp_profile()
    assert out["status"] in OK
    assert "auth_key" not in str(out)
