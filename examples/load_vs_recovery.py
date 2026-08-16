"""Training load against recovery: are the hard days landing on rested legs?

Training load and sleep are usually read in separate apps, which hides the
only question that matters: *was the athlete recovered on the day they went
hard?* Putting both on one timeline makes overreaching visible before it
becomes an injury.

Resting heart rate is the honest signal here. It is measured during sleep,
so a given night's RHR reflects the state the athlete woke up in -- before
that day's session. An RHR sitting well above baseline on the morning of the
hardest workout of the block is the classic setup for a bad outcome.

Run:  uv run examples/load_vs_recovery.py [days]
"""

from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zepp_mcp import client as api  # noqa: E402
from zepp_mcp import decode, workouts  # noqa: E402


def main(days: int = 30) -> int:
    api.load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    client = api.from_env()
    start, end = api.default_range(days)

    history = api.workout_history(client, start, end)
    band = api.band_data(client, start, end)
    if history.status != "ok" or band.status != "ok":
        print(f"workouts: {history.status}, daily: {band.status}")
        return 1

    # Load per day, summed across sessions.
    by_day: dict[str, dict] = defaultdict(lambda: {"load": 0, "sessions": []})
    for row in api.parse_rows(history.data):
        item = workouts.normalise(row)
        day = (item["start_local"] or "")[:10]
        if not day:
            continue
        by_day[day]["load"] += item["summary"].get("exercise_load") or 0
        by_day[day]["sessions"].append(item["sport"])

    days_data = [
        decode.summarise_day(r)
        for r in (band.data.get("data") or []) if isinstance(r, dict)
    ]

    # Baseline from nights that were actually recorded.
    resting = [
        d["sleep"]["resting_heart_rate_bpm"] for d in days_data
        if d.get("sleep", {}).get("resting_heart_rate_bpm")
    ]
    baseline = statistics.median(resting) if resting else None

    print(f"Training load vs recovery, {start} to {end}")
    if baseline:
        print(f"Resting HR baseline (median of recorded nights): {baseline:.0f} bpm\n")

    header = (f"{'Date':<12}{'Load':>6}{'Sleep':>7}{'Asleep':>8}"
              f"{'RHR':>6}{'vs base':>9}  Sessions")
    print(header)
    print("-" * (len(header) + 12))

    flags: list[str] = []
    for day in days_data:
        date = day["date"]
        sleep = day.get("sleep") or {}
        load = by_day.get(date, {}).get("load", 0)
        sessions = ", ".join(by_day.get(date, {}).get("sessions", [])) or "-"

        if not sleep.get("main_sleep_recorded", True):
            print(f"{date:<12}{load:>6}{'--':>7}{'--':>8}{'--':>6}{'--':>9}  "
                  f"{sessions}   (night not recorded)")
            continue

        rhr = sleep.get("resting_heart_rate_bpm")
        asleep = sleep.get("total_asleep_minutes")
        delta = f"{rhr - baseline:+.0f}" if rhr and baseline else "--"
        print(
            f"{date:<12}{load:>6}"
            f"{sleep.get('sleep_score', 0):>7}"
            f"{f'{asleep // 60}h{asleep % 60:02d}' if asleep else '--':>8}"
            f"{rhr or '--':>6}{delta:>9}  {sessions}"
        )

        if rhr and baseline and rhr - baseline >= 5:
            flags.append(
                f"{date}: resting HR {rhr} is {rhr - baseline:.0f} bpm over "
                f"baseline, on a {load}-load day"
            )
        if asleep and asleep < 360 and load > 0:
            flags.append(
                f"{date}: {asleep // 60}h{asleep % 60:02d} asleep before a "
                f"{load}-load day"
            )

    loads = [v["load"] for v in by_day.values() if v["load"]]
    if loads:
        peak_day = max(by_day.items(), key=lambda kv: kv[1]["load"])
        median_load = statistics.median(loads)
        print(f"\nPeak load: {peak_day[1]['load']} on {peak_day[0]} "
              f"({', '.join(peak_day[1]['sessions'])})")
        print(f"Median load across active days: {median_load:.0f}")
        if median_load and peak_day[1]["load"] > median_load * 3:
            print(
                f"That peak is {peak_day[1]['load'] / median_load:.1f}x the median "
                f"active day. Single-day spikes of this size are the classic "
                f"soft-tissue injury setup."
            )

    if flags:
        print("\nFlags:")
        for flag in dict.fromkeys(flags):
            print(f"  - {flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
