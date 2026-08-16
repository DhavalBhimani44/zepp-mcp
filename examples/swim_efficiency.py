"""Swim technique analysis: does efficiency hold up as volume rises?

SWOLF (seconds + strokes per length) is the single best pool metric, because
it punishes both slowing down and thrashing. Distance-per-stroke separates
the two: a rising SWOLF with falling DPS is technique breaking down, not
fatigue in the legs.

The question this answers is one a coach actually asks: *at what session
length does your stroke fall apart?* Total distance cannot answer it. Only
per-session SWOLF against volume can.

Run:  uv run examples/swim_efficiency.py [days]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zepp_mcp import client as api  # noqa: E402
from zepp_mcp import workouts  # noqa: E402


def main(days: int = 90) -> int:
    api.load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    client = api.from_env()

    start, end = api.default_range(days)
    outcome = api.workout_history(client, start, end)
    if outcome.status != "ok":
        print(f"{outcome.status}: {outcome.note}")
        return 1

    swims = [
        item for item in (workouts.normalise(r) for r in api.parse_rows(outcome.data))
        if item["sport"] == "pool_swimming"
    ]
    swims.sort(key=lambda s: s["start_local"] or "")

    if not swims:
        print("No pool swims in this range.")
        return 0

    print(f"Pool swims, {start} to {end}\n")
    header = f"{'Date':<12}{'Dist':>7}{'Lengths':>9}{'SWOLF':>7}{'DPS':>6}{'Str/len':>9}{'Avg HR':>8}"
    print(header)
    print("-" * len(header))

    rows = []
    for swim in swims:
        detail = swim["swim"]
        lengths = detail.get("total_trips") or 0
        strokes = detail.get("total_strokes") or 0
        per_length = strokes / lengths if lengths else 0
        rows.append((swim, per_length))
        print(
            f"{(swim['start_local'] or '')[:10]:<12}"
            f"{detail.get('dis', swim['summary'].get('dis', 0)):>6.0f}m"
            f"{lengths:>9}"
            f"{detail.get('swolf', 0):>7}"
            f"{detail.get('avg_distance_per_stroke', 0):>6.2f}"
            f"{per_length:>9.1f}"
            f"{swim['summary'].get('avg_heart_rate', 0):>8}"
        )

    # The coaching read: correlate volume against efficiency.
    best = min(rows, key=lambda r: r[0]["swim"].get("swolf", 999))
    worst = max(rows, key=lambda r: r[0]["swim"].get("swolf", 0))
    if best is not worst:
        bd, wd = best[0]["swim"], worst[0]["swim"]
        print(
            f"\nBest SWOLF {bd['swolf']} on {(best[0]['start_local'] or '')[:10]} "
            f"({bd.get('total_trips')} lengths, {best[1]:.1f} strokes/length)"
        )
        print(
            f"Worst SWOLF {wd['swolf']} on {(worst[0]['start_local'] or '')[:10]} "
            f"({wd.get('total_trips')} lengths, {worst[1]:.1f} strokes/length)"
        )
        volume_delta = (wd.get("total_trips", 0) - bd.get("total_trips", 0))
        if volume_delta > 0:
            print(
                f"\nThe worst session was {volume_delta} lengths LONGER and cost "
                f"{worst[1] - best[1]:.1f} more strokes per length. That is "
                f"volume outrunning technique, not a fitness gain."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 90))
