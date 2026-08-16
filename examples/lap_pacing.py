"""Within-session pacing: where in the workout does it fall apart?

Session averages hide the shape of a workout. An athlete who swims the first
ten lengths fast and the last ten badly has the same average as one who holds
steady, and they need completely different coaching.

This splits a session into thirds and compares them. A first-to-last-third
slowdown of more than a few percent is a pacing problem; a rise in strokes
per length over the same span is a technique-under-fatigue problem. They are
different faults with different fixes.

Note on lap records: the lap field interleaves per-length rows with per-set
summary rows. `decode_laps` separates them, so `laps` here is genuinely one
row per length and reconciles with the summary's own `total_trips`.

Run:  uv run examples/lap_pacing.py [track_id]
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zepp_mcp import client as api  # noqa: E402
from zepp_mcp import decode, workouts  # noqa: E402


def thirds(values: list[float]) -> tuple[list[float], list[float], list[float]]:
    size = max(1, len(values) // 3)
    return values[:size], values[size:-size] or [], values[-size:]


def main(track_id: str | None = None) -> int:
    api.load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    client = api.from_env()

    start, end = api.default_range(90)
    history = api.workout_history(client, start, end)
    if history.status != "ok":
        print(f"{history.status}: {history.note}")
        return 1

    items = [workouts.normalise(r) for r in api.parse_rows(history.data)]
    lapped = [i for i in items if i["sport"] in ("pool_swimming", "outdoor_running")]
    if not lapped:
        print("No lapped sessions found.")
        return 0

    target = next((i for i in lapped if i["track_id"] == track_id), None) if track_id \
        else max(lapped, key=lambda i: i["start_local"] or "")

    if target is None:
        print(f"No session with track_id {track_id}")
        return 1

    detail = api.workout_detail(client, target["track_id"], target["source"])
    if detail.status != "ok":
        print(f"{detail.status}: {detail.note}")
        return 1

    data = detail.data.get("data") or {}
    if not data.get("lap"):
        print("This session carries no lap records.")
        return 0

    decoded = decode.decode_laps(data["lap"])
    laps = decoded["laps"]

    reported = target["swim"].get("total_trips") if "swim" in target else None
    print(f"{target['sport']}  {target['start_local']}")
    print(f"{decoded['lap_count']} laps of {decoded['unit_distance_metres']:.0f}m"
          f" + {decoded['set_count']} set summaries", end="")
    print(f"  (summary reports {reported} lengths)" if reported else "")
    check = decoded.get("swolf_check")
    if check:
        print(f"SWOLF identity holds on {check['identity_holds']}/"
              f"{check['laps_checked']} laps")
    if decoded.get("note"):
        print(f"\nWARNING: {decoded['note']}\n")
        return 1
    print()

    times = [lap["duration_seconds"] for lap in laps]
    strokes = [lap.get("strokes") for lap in laps]
    has_strokes = all(s is not None for s in strokes)

    header = f"{'Third':<10}{'Lengths':>9}{'Avg time':>10}{'Strokes/len':>13}"
    print(header)
    print("-" * len(header))
    labels = ("First", "Middle", "Last")
    parts_t = thirds(times)
    parts_s = thirds([s for s in strokes if s is not None]) if has_strokes else ([], [], [])

    summary_rows = []
    for label, part_t, part_s in zip(labels, parts_t, parts_s):
        if not part_t:
            continue
        avg_t = statistics.mean(part_t)
        avg_s = statistics.mean(part_s) if part_s else None
        summary_rows.append((label, avg_t, avg_s))
        print(f"{label:<10}{len(part_t):>9}{avg_t:>9.1f}s"
              f"{(f'{avg_s:.1f}' if avg_s else '--'):>13}")

    if len(summary_rows) >= 2:
        first, last = summary_rows[0], summary_rows[-1]
        drift = (last[1] - first[1]) / first[1] * 100
        print(f"\nPace drift first third -> last third: {drift:+.1f}%")
        if first[2] and last[2]:
            stroke_drift = (last[2] - first[2]) / first[2] * 100
            print(f"Strokes per length drift:            {stroke_drift:+.1f}%")
            if drift > 3 and stroke_drift > 3:
                print("\nBoth slowed AND took more strokes: technique degraded "
                      "under fatigue. Shorten the set or add rest.")
            elif drift > 3:
                print("\nSlowed while holding stroke count: aerobic fatigue, "
                      "technique intact. This is a fitness limiter.")
            elif stroke_drift > 3:
                print("\nHeld pace by taking more strokes: paying for speed "
                      "with efficiency. Watch for it lengthening.")
            else:
                print("\nHeld both pace and stroke count. Well-paced session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
