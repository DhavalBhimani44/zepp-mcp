# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "pycryptodome"]
# ///
"""Zepp recon spike. Run once. Throwaway.

Credentials come from spike/.env (git-ignored), or from the environment,
which wins if both are set:

    cp .env.example .env   # then fill it in
    uv run probe.py

Writes redacted captures to ./out/ and a summary to ./FINDINGS.md.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from auth import login
from capture import Capture
from redact import Redactor

OUT = Path(__file__).parent / "out"
ENV_FILE = Path(__file__).parent / ".env"
PAUSE = 1.5


def load_dotenv(path: Path = ENV_FILE) -> dict[str, str]:
    """Read KEY=VALUE lines from .env into os.environ.

    A real environment variable always wins, so `ZEPP_EMAIL=x uv run probe.py`
    overrides the file. Blank lines and `#` comments are skipped; surrounding
    quotes are stripped so a password containing `#` survives intact. Returns
    the keys it set, never the values — these are credentials.
    """
    if not path.is_file():
        return {}
    applied: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            applied[key] = "set"
    return applied

DATA_HOSTS = [
    "api-mifit.zepp.com",
    "api-mifit-us2.zepp.com",
    "api-mifit-us3.zepp.com",
    "api-mifit-de2.zepp.com",
    "api-mifit-cn.zepp.com",
]


@dataclass
class Session:
    client: httpx.Client
    cap: Capture
    redactor: Redactor
    token: str
    uid: str
    host: str = "api-mifit.zepp.com"
    notes: dict[str, Any] = field(default_factory=dict)


def get(session: Session, name: str, path: str,
        params: dict | None = None, host: str | None = None) -> dict:
    url = f"https://{host or session.host}{path}"
    started = time.monotonic()
    try:
        response = session.client.get(
            url, headers={"apptoken": session.token},
            params=params or {}, timeout=25,
        )
        status, text = response.status_code, response.text
    except httpx.HTTPError as exc:
        status, text = 0, f"TRANSPORT ERROR: {exc}"
    elapsed = int((time.monotonic() - started) * 1000)
    record = session.cap.record(
        name=name, method="GET", url=url, params=params,
        status=status, body_text=text, elapsed_ms=elapsed,
    )
    time.sleep(PAUSE)
    return record


def probe_region(session: Session) -> str:
    """Same request against every host. Spec question 1."""
    today = dt.date.today()
    week_ago = today - dt.timedelta(days=7)
    winner = session.host
    for host in DATA_HOSTS:
        record = get(
            session, f"region_{host}", "/v1/data/band_data.json",
            {"query_type": "detail", "device_type": "android_phone",
             "userid": session.uid,
             "from_date": week_ago.isoformat(), "to_date": today.isoformat()},
            host=host,
        )
        if record["status"] == 200 and not record["empty_200"]:
            winner = host
            break
    session.notes["region_host"] = winner
    return winner


def probe_retention(session: Session) -> str | None:
    """Walk back by year to find the earliest date with data. Spec question 2."""
    today = dt.date.today()
    earliest = None
    for years_back in range(1, 11):
        start = today.replace(year=today.year - years_back)
        end = start + dt.timedelta(days=30)
        record = get(
            session, f"retention_{start.year}", "/v1/data/band_data.json",
            {"query_type": "detail", "device_type": "android_phone",
             "userid": session.uid,
             "from_date": start.isoformat(), "to_date": end.isoformat()},
        )
        if record["status"] == 200 and not record["empty_200"]:
            earliest = start.isoformat()
        elif earliest:
            break
    session.notes["earliest_data"] = earliest
    return earliest


def probe_query_type(session: Session) -> None:
    """summary vs detail, side by side. Spec question 5."""
    today = dt.date.today()
    week_ago = today - dt.timedelta(days=7)
    for query_type in ("summary", "detail"):
        get(session, f"query_type_{query_type}", "/v1/data/band_data.json",
            {"query_type": query_type, "device_type": "android_phone",
             "userid": session.uid,
             "from_date": week_ago.isoformat(), "to_date": today.isoformat()})


EVENTS_V1 = [
    ("all_day_stress", None), ("PaiHealthInfo", None),
    ("blood_oxygen", "click"), ("single_stress", None),
    ("health_data", "blood_pressure"),
]

EVENTS_DATESTRING = [
    ("blood_oxygen", "odi"), ("blood_oxygen", "osa_event"),
]

EVENTS_V2 = [
    ("readiness", "watch_score"), ("DailyHealth", "summary"),
    ("Charge", "real_data"), ("Charge", "stress_data"),
    ("hrv_sdnn", "real_data"), ("HRVRMSSD", "real_data"),
    ("RespiratoryRate", "real_data"), ("blood_pressure", "real_data"),
    ("Emotion", "real_data"), ("LactateThreshold", "summary"),
]


def probe_endpoints(session: Session) -> None:
    """One request per spec section 4 endpoint. Spec question 4."""
    uid = session.uid
    today = dt.date.today()
    month_ago = today - dt.timedelta(days=30)
    for name, path, params in [
        ("profile", "/huami.health.getUserInfo.json", None),
        ("manual_data", "/v1/user/manualData.json", None),
        ("weight", f"/users/{uid}/members/-1/weightRecords", None),
        ("blood_pressure", "/users/me/bloodPressure", None),
        ("heart_rate", f"/users/{uid}/heartRate", None),
        ("sport_load", f"/v2/watch/users/{uid}/WatchSportStatistics/SPORT_LOAD", None),
        ("vo2_max", f"/v2/watch/users/{uid}/WatchSportStatistics/VO2_MAX", None),
        ("second_hr_index", "/users/me/fileInfo/events",
         {"eventType": "second_heart_rate", "subType": "real_data",
          "from": _ms(month_ago), "to": _ms(today), "limit": 50}),
    ]:
        get(session, name, path, params)


def _ms(day: dt.date) -> str:
    return str(int(dt.datetime.combine(day, dt.time.min).timestamp() * 1000))


def probe_events(session: Session) -> None:
    """Every event family and pair. Spec questions 4 and 12."""
    uid = session.uid
    today = dt.date.today()
    month_ago = today - dt.timedelta(days=30)
    from_ms, to_ms = _ms(month_ago), _ms(today)

    for event_type, sub_type in EVENTS_V1:
        params = {"from": from_ms, "to": to_ms,
                  "eventType": event_type, "limit": 20, "reverse": "false"}
        if sub_type:
            params["subType"] = sub_type
        get(session, f"ev1_{event_type}_{sub_type or 'none'}",
            f"/users/{uid}/events", params)

    for event_type, sub_type in EVENTS_DATESTRING:
        get(session, f"evdate_{event_type}_{sub_type}",
            f"/users/{uid}/events/dateString",
            {"from": month_ago.isoformat(), "to": today.isoformat(),
             "eventType": event_type, "subType": sub_type,
             "timeZone": "Asia/Kolkata", "limit": 20})

    for event_type, sub_type in EVENTS_V2:
        get(session, f"ev2_{event_type}_{sub_type}", "/v2/users/me/events",
            {"from": from_ms, "to": to_ms, "eventType": event_type,
             "subType": sub_type, "limit": 20})


# ANSWERED by the first live run: the {sport} path segment is a fixed route
# name, not a filter. "run" returns EVERY activity type; walking, cycling,
# swimming, indoor_swimming, strength and football all 404. One call covers
# all sports, so zepp_list_workouts needs no per-sport fan-out.
SPORT_SLUGS = ["run"]


def probe_workouts(session: Session) -> None:
    """Spec questions 6, 7, 8, 10."""
    today = dt.date.today()
    far_back = today.replace(year=today.year - 5)

    # Does the sport path parameter partition the results, or is it ignored?
    for slug in SPORT_SLUGS:
        get(session, f"workout_history_{slug}", f"/v1/sport/{slug}/history.json",
            {"from_date": far_back.isoformat(), "to_date": today.isoformat(),
             "userid": session.uid})

    # Fetch one detail response PER SPORT TYPE, not merely the first few.
    # Spec section 9 requires every workout decoder to be tested against a real
    # activity of that sport; three consecutive runs would leave the swim,
    # cycling and football decoders with no fixture at all.
    index_records = [
        entry for entry in session.cap.entries
        if entry["name"].startswith("workout_history_")
        and entry["status"] == 200 and not entry["empty_200"]
    ]
    workouts = _extract_workouts(index_records)
    session.notes["workout_count_seen"] = len(workouts)

    by_type: dict[str, dict] = {}
    for row in workouts:
        key = str(row.get("type", "unknown"))
        by_type.setdefault(key, row)
    session.notes["workout_types_seen"] = sorted(by_type)

    for type_key, row in by_type.items():
        track_id = row["_track_id"]
        get(session, f"workout_detail_type{type_key}_{track_id}",
            "/v1/sport/run/detail.json",
            {"trackid": track_id, "source": row.get("source", "run.mobile"),
             "userid": session.uid})

    # Longest activity overall is the best multisport candidate. Spec question 10.
    if workouts:
        longest = max(workouts, key=lambda r: int(r.get("_duration", 0) or 0))
        if longest["_track_id"] not in {r["_track_id"] for r in by_type.values()}:
            get(session, f"workout_detail_longest_{longest['_track_id']}",
                "/v1/sport/run/detail.json",
                {"trackid": longest["_track_id"],
                 "source": longest.get("source", "run.mobile"),
                 "userid": session.uid})


def _extract_workouts(records: list[dict]) -> list[dict]:
    """Flatten index rows. TrackIDs are UNIX timestamps; field naming is
    unverified, so probe several spellings and keep the whole row."""
    found: list[dict] = []
    seen: set[str] = set()
    for record in records:
        body = record.get("body_parsed")
        if not isinstance(body, dict):
            continue
        payload = body.get("data")
        rows = payload.get("summary") if isinstance(payload, dict) else payload
        if isinstance(rows, str):
            import json as _json
            try:
                rows = _json.loads(rows)
            except ValueError:
                continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            track_id = row.get("trackid") or row.get("trackId") or row.get("id")
            if track_id is None or str(track_id) in seen:
                continue
            seen.add(str(track_id))
            enriched = dict(row)
            enriched["_track_id"] = str(track_id)
            # Observed shapes: trackid is a STRING holding epoch seconds
            # ("1786761306"); the end field is end_time (underscored), also a
            # string. The plan assumed both were ints named trackid/endtime,
            # which raised TypeError on the first real payload.
            enriched["_duration"] = _seconds(row.get("end_time")) - _seconds(track_id)
            found.append(enriched)
    return found


def _seconds(value: object) -> int:
    """Coerce an epoch field to int seconds. Zepp mixes str and int, and uses
    -1 as a not-applicable sentinel across the workout summary."""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return 0 if parsed < 0 else parsed


def main() -> None:
    load_dotenv()
    email = os.environ.get("ZEPP_EMAIL")
    password = os.environ.get("ZEPP_PASSWORD")
    if not email or not password:
        sys.exit(
            "Set ZEPP_EMAIL and ZEPP_PASSWORD in spike/.env "
            "(copy .env.example) or in the environment."
        )

    redactor = Redactor()
    redactor.register(email, "email")
    redactor.register(password, "password")
    cap = Capture(OUT, redactor)

    with httpx.Client() as client:
        token_info = login(client, cap, redactor, email, password)
        session = Session(
            client=client, cap=cap, redactor=redactor,
            token=token_info["app_token"], uid=str(token_info["user_id"]),
        )
        session.notes["login_variant"] = token_info["_variant"]
        session.notes["token_info_keys"] = sorted(token_info.keys())

        session.host = probe_region(session)
        probe_retention(session)
        probe_query_type(session)
        probe_endpoints(session)
        probe_events(session)
        probe_workouts(session)

        cap.write_index()
        print(f"Captured {len(cap.entries)} responses to {OUT}")
        print(f"Region host: {session.notes['region_host']}")
        print(f"Earliest data: {session.notes['earliest_data']}")


if __name__ == "__main__":
    main()
