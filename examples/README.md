# Examples

Three analyses that are hard to get from the Zepp app, each runnable against
a live account. The output below is real, from the maintainer's own data.

They import `zepp_mcp` directly rather than going through MCP, which is the
point: the decoders are a library, and the MCP server is one consumer of it.
An assistant with the server connected reaches the same numbers by calling
`zepp_list_workouts` and `zepp_workout_detail`.

```bash
uv run examples/swim_efficiency.py 30
uv run examples/load_vs_recovery.py 10
uv run examples/lap_pacing.py [track_id]
```

## swim_efficiency.py — does technique survive volume?

SWOLF punishes both slowing down and thrashing; distance-per-stroke separates
them. Plotting them against session volume answers a question total distance
cannot: *at what session length does the stroke fall apart?*

```
Date           Dist  Lengths  SWOLF   DPS  Str/len  Avg HR
----------------------------------------------------------
2026-08-14     630m       30     38  1.74     12.0     138
2026-08-15     756m       36     40  1.57     13.4     139
2026-08-16     567m       27     36  1.75     12.0     131

Best SWOLF 36 on 2026-08-16 (27 lengths, 12.0 strokes/length)
Worst SWOLF 40 on 2026-08-15 (36 lengths, 13.4 strokes/length)

The worst session was 9 lengths LONGER and cost 1.4 more strokes per length.
That is volume outrunning technique, not a fitness gain.
```

## load_vs_recovery.py — are the hard days landing on rested legs?

Resting heart rate is measured during sleep, so a night's RHR reflects the
state the athlete woke up in — *before* that day's session. Elevated RHR on
the morning of the block's hardest workout is the classic setup for a bad
outcome.

```
Date          Load  Sleep  Asleep   RHR  vs base  Sessions
----------------------------------------------------------------------
2026-08-08      51     68    7h29    60       +0  outdoor_running
2026-08-09     281     42    5h39    68       +8  hiking, hiking
2026-08-10       0     78    7h21    60       +0  -
2026-08-13      12     --      --    --       --  walking, strength   (night not recorded)
2026-08-14      45     61    6h15    58       -2  strength, pool_swimming
2026-08-15      58     80    7h03    57       -2  strength, pool_swimming

Peak load: 281 on 2026-08-09 (hiking, hiking)
Median load across active days: 48
That peak is 5.9x the median active day.

Flags:
  - 2026-08-09: resting HR 68 is 8 bpm over baseline, on a 281-load day
  - 2026-08-09: 5h39 asleep before a 281-load day
```

Note the `night not recorded` rows. Zepp returns a sleep block full of zeros
for a night the watch missed, and an earlier version of this project rendered
that as `sleep_score: 0, resting_heart_rate_bpm: 0` — a missing measurement
presented as a measurement. Those rows are now excluded from the baseline
rather than dragging it down.

## lap_pacing.py — where in the session does it fall apart?

Session averages hide shape. Splitting into thirds separates two different
faults: slowing while holding stroke count is aerobic fatigue, while taking
more strokes to hold pace is technique breaking down.

```
pool_swimming  2026-08-16T08:09:34+05:30
27 laps of 21m + 2 set summaries  (summary reports 27 lengths)
SWOLF identity holds on 25/27 laps

Third       Lengths  Avg time  Strokes/len
------------------------------------------
First             9     24.4s         11.7
Middle            9     25.6s         11.6
Last              9     24.3s         12.7

Pace drift first third -> last third: -0.5%
Strokes per length drift:            +8.6%

Held pace by taking more strokes: paying for speed with efficiency.
```

### How this example resolved a documented gap

The lap decoder used to carry a warning that lap sums were unreliable: the
record count disagreed with the summary's `total_trips`, and column sums came
to roughly double the reported totals.

Writing this example exposed why. The lap field **interleaves two record
types**: one row per length, plus a summary row per set. Separating them by
distance makes everything reconcile:

| Check | Result |
| --- | --- |
| Length rows | 36 = `total_trips` 36 |
| Set rows | 2, at 63 m and 504 m (3 + 24 = 27 lengths) |
| Set distance sum | 756 m = `dis` |
| Set stroke sum | 392 + 89 = 481 = `total_strokes` |

It also confirmed the column mapping. On a length row, **column 14 = column 1
+ column 13**. SWOLF is standardised across swim wearables as *seconds for one
pool length plus strokes taken in that length* — exactly this identity, which
means the mapping is confirmed against a published definition rather than
merely inferred.

Three independent routes agree:

1. **Internal identity** — holds on 34 of 36 laps; the rest differ by 1.
2. **Published definition** — matches the standard SWOLF formula.
3. **Session reconstruction** — summing the decoded components reproduces
   Zepp's own summary figure: `(974s + 478 strokes) / 36 lengths = 40.3`
   against a reported `swolf` of **40**.

The identity is self-checking, so `decode_laps` reports how many laps satisfy
it and refuses to present the column names as fact when they do not:

```json
"swolf_check": {
  "laps_checked": 36,
  "identity_holds": 34,
  "rule": "swolf == duration_seconds + strokes (tolerance 1)"
}
```

Tolerance is 1 because Zepp rounds the sum and its components independently.

## Writing your own

Every example follows the same shape:

```python
from zepp_mcp import client as api, decode, workouts

api.load_dotenv(Path(".env"))
client = api.from_env()

start, end = api.default_range(30)
outcome = api.workout_history(client, start, end)
if outcome.status != "ok":       # never assume "ok"
    ...
rows = [workouts.normalise(r) for r in api.parse_rows(outcome.data)]
```

Two rules worth keeping:

- **Check `outcome.status`.** `no_data` means Zepp returned an empty success
  response, which is not the same as confirmed absence.
- **Do not use a value flagged `unit_verified: false`** in a calculation whose
  result you present with a unit.
