"""Probe candidate Zepp endpoints against YOUR account, conservatively.

Every hypothesis in docs/capability-matrix.md that is below level 1 is listed
here with the request shape that would settle it and a NEGATIVE CONTROL --
the same route family with a fabricated name -- because on this API a single
response is often uninterpretable on its own:

* an empty 200 from the events routes looks identical for "no data" and
  "no such event type", so a candidate only counts when it returns RECORDS;
* on WatchSportStatistics, 400 means "unknown name" and 500 "known name,
  failed", which only reads correctly next to the control's status.

Safety:

* Uses the server's own client: the cached token, one login at most, never a
  retry loop against the login endpoint (Zepp locks accounts after 10
  failures).
* GET only. Nothing is written to the account.
* Paced at ZEPP_PROBE_INTERVAL seconds (default 1.5) between requests.
* Prints status, size and top-level KEY NAMES only -- never values -- so the
  output can be pasted into an issue. Use --save DIR to keep full bodies
  locally for decoder work; anonymise with tools/anonymize_fixtures.py
  before sharing anything saved.

Run:  uv run tools/probe_candidates.py [--days 14] [--save spike/out/probe]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zepp_mcp import client as api  # noqa: E402


def _ms(day: dt.date) -> str:
    return str(int(dt.datetime.combine(day, dt.time.min).timestamp() * 1000))


def candidates(uid: str, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    v2 = "/v2/users/me/events"
    window = {"from": _ms(start), "to": _ms(end + dt.timedelta(days=1)), "limit": 20}
    stats = f"/v2/watch/users/{uid}/WatchSportStatistics"
    stat_params = {"startDay": start.isoformat(), "endDay": end.isoformat(),
                   "limit": 900, "isReverse": "true"}
    out: list[dict[str, Any]] = [
        {"name": "control: WatchSportStatistics fabricated name",
         "path": f"{stats}/ZZZ_NOT_REAL", "params": stat_params, "control": True},
        {"name": "WatchSportStatistics SPORT_LOAD with startDay/endDay",
         "path": f"{stats}/SPORT_LOAD", "params": stat_params},
        {"name": "WatchSportStatistics VO2_MAX with startDay/endDay",
         "path": f"{stats}/VO2_MAX", "params": stat_params},
        {"name": "control: unknown users route",
         "path": f"/users/{uid}/zzz_not_real", "params": {}, "control": True},
        {"name": "devices",
         "path": f"/users/{uid}/devices",
         "params": {"enableMultiDevice": "true", "device_type": "android_phone"}},
        {"name": "device binds (legacy)", "path": "/v1/device/binds.json",
         "params": {"userid": uid}},
        {"name": "heartRate with startTime/endTime",
         "path": f"/users/{uid}/heartRate",
         "params": {"startTime": int(time.mktime(start.timetuple())),
                    "endTime": int(time.mktime(end.timetuple())) + 86400,
                    "limit": 1000, "type": 2}},
        {"name": "bloodPressure with days/toDate",
         "path": "/users/me/bloodPressure",
         "params": {"days": (end - start).days + 1, "toDate": end.isoformat(),
                    "sourceArrayStr": "com.huami.midong.associated,com.huami.midong"}},
        {"name": "manualData sleep", "path": "/v1/user/manualData.json",
         "params": {"userid": uid, "type": "sleep"}},
        {"name": "control: v2 events fabricated type", "path": v2,
         "params": {**window, "eventType": "ZZZ_NOT_REAL", "subType": "real_data"},
         "control": True},
    ]
    for event, sub in (
        ("exertion", "algo_result"), ("Charge", "insight_data"),
        ("sport", "recovery_hr"), ("skinTemp", "real_data"),
        ("phn", "daily_analysis"), ("phn", "training_plan"),
        ("EmotionBaseline", "real_data"), ("readiness", "watch_score_data"),
        ("blood_pressure", "manually_add_data"), ("hrv_sdnn", "real_data"),
    ):
        out.append({"name": f"v2 events {event}/{sub}", "path": v2,
                    "params": {**window, "eventType": event, "subType": sub}})
    return out


def shape(payload: Any) -> dict[str, Any]:
    """Key names and record counts only -- never values."""
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    out: dict[str, Any] = {"keys": sorted(payload)[:20]}
    for container in ("items", "data"):
        inner = payload.get(container)
        if isinstance(inner, list):
            out[f"{container}_count"] = len(inner)
            if inner and isinstance(inner[0], dict):
                first = inner[0]
                value = first.get("value")
                out[f"{container}[0]_keys"] = sorted(first)[:30]
                if isinstance(value, dict):
                    out[f"{container}[0].value_keys"] = sorted(value)[:30]
        elif isinstance(inner, dict):
            out[f"{container}_keys"] = sorted(inner)[:30]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--save", type=Path, default=None,
                        help="directory to keep full bodies (local only)")
    args = parser.parse_args()

    api.load_dotenv(ROOT / ".env")
    interval = float(os.environ.get("ZEPP_PROBE_INTERVAL", "1.5"))
    client = api.from_env()
    client._min_interval = max(interval, 1.0)
    uid = client.credential().user_id
    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)

    if args.save:
        args.save.mkdir(parents=True, exist_ok=True)

    results = []
    for index, probe in enumerate(candidates(uid, start, end)):
        outcome = client.get(probe["path"], probe["params"], use_cache=False)
        record = {
            "probe": probe["name"],
            "control": probe.get("control", False),
            "status": outcome.status,
            "http_status": outcome.http_status,
            "error_kind": outcome.error_kind,
            "shape": shape(outcome.data) if outcome.data is not None else None,
        }
        results.append(record)
        print(json.dumps(record))
        if args.save and outcome.data is not None:
            (args.save / f"{index:03d}.json").write_text(json.dumps(
                {"probe": probe["name"], "params": probe["params"],
                 "body": outcome.data}, indent=1))
            os.chmod(args.save / f"{index:03d}.json", 0o600)

    print("\nRead each candidate against the control just above it. An "
          "empty 200 is NOT evidence either way; only returned records are.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
