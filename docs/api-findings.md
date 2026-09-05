# Zepp Recon Spike — Findings

**Date:** 2026-08-15 · **Captures:** 47 · **Account:** single Amazfit device, ~1 week of history

Answers the twelve open questions in `docs/superpowers/specs/2026-08-15-zepp-mcp-design.md` §11,
plus five findings the spec did not anticipate. Plans 2 and 3 are written from this document.

---

## The twelve questions

**1. Region host — `api-mifit.zepp.com`** (no regional suffix). It was the first candidate tried and
returned 88 KB; the sweep short-circuited there.

Notable: all five login variants echoed `region=us-west-2` back regardless of whether we sent
`us-west-2` or `eu-central-1`. **The region parameter is not honoured at login step 1.** The
five-variant matrix in the plan was therefore near-useless — five effectively identical requests.

**But the successful login response answers the region question outright.** We sent
`country_code=US`; the redirect came back carrying `country_code=IN`:

```
...&state=REDIRECTION&region=us-west-2&refresh=<redacted>&access=<redacted>
   &country_code=IN&expiration=1787646981
```

The server knows the account's real country and returns it. So `Credential.region_host` is
populated from the login response exactly as spec §5 requires — the client never guesses, never
probes a host matrix, and never varies login parameters to find out. The `expiration` field also
gives the access code's lifetime directly (~10 days on this response), which removes the guesswork
from spec §2's "app_token TTL ≈ 30 days".

**2. Retention — indeterminate, and the question was malformed.** Every yearly probe from 2016 to
2025 returned an empty-200 (40 bytes), while the last 7 days returned 88 KB. This is not a retention
wall: the account simply has no older data. All 8 workouts fall between 2026-08-08 and 2026-08-15.
The retention ceiling remains untested and will stay untested until the account accumulates history.

This is exactly the case §6's empty-200 rule exists for — an empty response that is *genuine
absence*, distinguishable from a fault only because sibling requests succeeded.

**3. Password flow — WORKS in August 2026.** The first run's five `error=401` responses were a
mistyped password, not a broken flow. `PasswordProvider` stays the happy path in Plan 2 and
`ManualTokenProvider` stays the escape hatch, as the spec has it.

> **Lockout risk, newly discovered.** The step-1 redirect carries `attempts=N&max_attempts=10`,
> a counter shared across every host and region. Ten consecutive failures presumably locks the
> account. Plan 2 must never loop over login variants, and must surface the counter on failure.

**4. Metrics this watch produces**

| Status | Scopes |
|---|---|
| **Data** | `all_day_stress`, `PaiHealthInfo`, `blood_oxygen/click`, `single_stress`, `blood_oxygen/odi`, `readiness/watch_score`, `DailyHealth/summary`, `Charge/real_data` (3.5 MB), `Charge/stress_data` (506 KB), `hrv_sdnn`, `HRVRMSSD` (107 KB), `RespiratoryRate`, `second_heart_rate` file index, `weight`, profile |
| **Empty-200** | `health_data/blood_pressure`, `blood_oxygen/osa_event`, `blood_pressure/real_data`, `Emotion/real_data`, `LactateThreshold/summary` |
| **400** | `manualData.json`, `users/me/bloodPressure`, `users/{uid}/heartRate` — these need parameters the spec's inventory does not document |
| **500** | `WatchSportStatistics/SPORT_LOAD`, `WatchSportStatistics/VO2_MAX` |

The `metric` enum in `zepp_get_timeseries` should be built from row 1 only. Rows 2–4 are not
"unsupported" — they are unanswered, and `zepp_raw_request` is how they get revisited.

**5. `query_type` — `detail` is the superset.** `detail` returned 88,375 bytes, `summary` 26,751.
ARCHITECTURE.md §4 specifies `summary`, which is the lesser payload. Use `detail`.

**6. `history.json` covers ALL sports — the `{sport}` path segment is a fixed route name, not a
filter.** `/v1/sport/run/history.json` returned all 8 workouts across 5 distinct types. Every other
slug (`walking`, `cycling`, `swimming`, `indoor_swimming`, `strength`, `football`) returns 404.

**`zepp_list_workouts` needs no per-sport fan-out.** One call covers everything.

**7. Detail endpoint and TrackID** — `/v1/sport/run/detail.json?trackid=<id>&source=<source>`.
All six probes returned 200 (46 KB–538 KB).

- `trackid` is a **string** holding epoch *seconds* (`"1786761306"`), not an integer.
- `source` looks like `run.12345678.huami.com` and must be passed through from the index row.
- Pagination is `data.next` (`-1` when exhausted), **not** the start/stop TrackID scheme the plan
  assumed from prior art.

**8. The index carries sport-specific summaries — abundantly.** Each row has ~150 fields. A pool
swim row includes `swolf`, `total_strokes`, `avg_distance_per_stroke`, `avg_stroke_speed`,
`swim_pool_length`, `swim_style`, `freestyle_length`, `breast_stroke_length`, `butterfly_length`,
`medley_length`, `total_trips`, and a `pb` personal-best object.

This confirms the spec §8 decision to push aggregates into the index: *"how has my SWOLF trended"*
is genuinely one call, no detail fetches required.

**9. VO₂ max — unknown.** Both `SPORT_LOAD` and `VO2_MAX` return 500. The workout summary carries a
per-workout `VO2_max` field (`-1` on these activities), so it may be per-activity rather than a
separate series. Unresolved; needs `zepp_raw_request` exploration once there is data.

**10. Multisport — none recorded, but the schema clearly supports it.** All 8 activities are
single-sport. Critically, every row carries `parent_trackid` (`-1`) and `child_list` (empty).

**Those two fields are the multisport mechanism**, and their existence vindicates spec §8's
`segments[]` model. Plan 3 should build parent/child assembly now; a triathlon will populate
`child_list` with member TrackIDs and set `parent_trackid` on the legs.

**11. Devices — one.** `bind_device` = `0:MILI:12345678:0.0.0.0`, `devicesource` = `12345678`,
consistent across all 8 workouts. No stream collision to design around yet.

**12. Payload shapes — two distinct encodings, both hostile.**

Double-encoded JSON is confirmed exactly as spec §5 warned: `add_info`, `pb`, and
`strength_training_group` are JSON strings nested inside JSON.

The detail response is worse, and is finding A below.

---

## Findings the spec did not anticipate

**A. Workout detail is ~70 flat delta-encoded strings, not JSON.** `data` is a single-level dict
whose every value is a packed string. Which fields are populated depends entirely on sport — the
pool swim has empty `longitude_latitude`/`altitude`/`speed`, the run populates all three.

```
heart_rate           "22,117;1,-1;1,2;1,0;..."     (time_delta, value_delta) pairs
longitude_latitude   "1854240229,7379252091;151,39;..."  absolute, then deltas, scaled 1e-8
lap                  38 records × ~70 positional columns, no header
pool_swim_pace       "98,2.06000;0,2.06000;..."
```

Every stream needs its own decoder, and the `lap` field needs a positional column map derived from
the fixtures. This is decoder work for Plan 3 and it is larger than the spec assumed.

It also maps cleanly onto spec §8's `include=["summary","laps","streams","gps"]` selector:
`laps` → the `lap` field; `gps` → `longitude_latitude`/`altitude`/`accuracy`; `streams` →
everything else.

**B. Sentinel values are pervasive and varied.** `-1` is the general not-applicable marker, but
altitude uses `-20000`, angle uses `-361`, and elevation gain/loss use `-100`. Spec §6 already says
"sentinels are a set" for `data_hr`; that rule now extends across the entire workout surface, and
rendering `-20000` as an altitude in metres would look plausible and be nonsense.

**C. Sport type is an unnamed numeric code.** Observed: `1`, `8`, `14`, `22`, `52`. Type 14 is pool
swimming (carries `swolf`/`swim_pool_length`); type 1 is outdoor running (GPS, 2.8 m/s). The payload
contains no human-readable sport name anywhere. `zepp_describe_schema` must ship a code→name map,
and it can only be built by correlating captures against what the Zepp app displays.

**D. Timestamps mix both type and unit within a single row.** `trackid` is a string of seconds,
`end_time` is a string of seconds, `createTime` is an int of milliseconds, `totalTimeWithMillis` is
an int of milliseconds, `run_time` is a string of seconds. Spec §5's "normalize on write" applies
per-field, not per-endpoint.

**E. GPS coordinates are in the corpus, and the redactor does not touch them.**

`longitude_latitude` in the running detail decodes to real coordinates at 1e-8 scaling (verified: 3 of 47 captures carry them) — a home
address, in practice, since routes start where the runner lives. The redactor was built for
credentials and correctly ignores this.

**This blocks fixture promotion as currently specified.** Spec §9 and the plan's Task 7 copy the
capture corpus into `tests/fixtures/zepp/` as committed fixtures, in a repository intended for
open-sourcing. Committing the running and walking details as-is would publish location history.

Three options for Plan 2, in preference order:

1. **Promote a curated subset** — the pool swim (no GPS), plus GPS-bearing activities only after
   their coordinate streams are scrubbed or replaced with synthetic tracks.
2. **Extend the redactor with a coordinate scrubber** and run it over every detail capture.
3. **Keep all fixtures local and git-ignored**, testing decoders against a machine-local corpus.

Option 1 is recommended: it preserves real bytes for every decoder except the GPS one, which is the
easiest to synthesise convincingly.

---

## Corrections this forces on the spec and plan

| Location | Change |
|---|---|
| Spec §4, plan Task 6 | Sport enumeration is unnecessary — one `run` call returns all sports |
| Spec §4 | Pagination is `data.next`, not start/stop TrackIDs |
| Spec §4 | `query_type=detail`, not `summary` |
| Spec §8 | `parent_trackid`/`child_list` are the multisport mechanism — build for them |
| Spec §9 | Fixture promotion needs a GPS decision before anything is committed |
| Plan Task 3 | Never loop login variants — `max_attempts=10` is shared and region is ignored anyway |
| Plan Task 6 | `trackid` is `str`, and the field is `end_time` not `endtime` (this crashed the first run) |

---

## Training plans and Zepp Coach — probed 2026-08-17

**No training-calendar or coach endpoint was found**, and the negative result
needs its controls stated, because two of the three signals turned out to be
worthless on their own.

### What was probed

Thirteen candidate routes across `/v1/sport/`, `/v1/training/`, `/v1/course/`,
`/v2/users/me/`, `/v2/watch/users/{uid}/` and `/v1/coach/`, plus three
`WatchSportStatistics` names and four event types.

### 404 vs 400 vs 500 on WatchSportStatistics

| Statistic | HTTP | Meaning |
|---|---|---|
| `ZZZ_NOT_REAL` (control) | 400 | unknown name |
| `TRAINING_LOAD`, `RECOVERY_TIME`, `TRAINING_STATUS` | 400 | **unknown name — same as the control** |
| `SPORT_LOAD`, `VO2_MAX` | 500 | recognised name, server-side failure |

The control is what makes this readable. A 400 here means "no such statistic",
so the three training names carry no evidence of existing. The 500s are the
interesting ones: those two names ARE recognised and are broken server-side.

**Discriminator worth reusing:** on `WatchSportStatistics`, 400 means the name
is unknown and 500 means the name is real but failing.

### The events API cannot answer this question at all

`eventType=TrainingPlan`, `trainingLoad` and `SportPlan` each returned an
empty HTTP 200 — and so did a deliberately fabricated `eventType=ZZZ_NOT_REAL`.

An empty 200 from `/v2/users/me/events` is therefore **indistinguishable
between "no such event type" and "no data for this range"**. No conclusion
about training plans can be drawn from it in either direction. This is spec
section 6's empty-200 rule meeting a live case: the response is a fault and an
absence wearing the same clothes.

### The schema does support plans

Every workout row carries `runningProgram`, `dailyPlanFinished`,
`course_title`, `coachInsight`, `totalInsight`, `degreeOfCompletion`,
`runningType` and `scoringMethod`. On this account all are empty or zero,
consistent with no plan ever having been active.

**The likely route to plan data is therefore the workout row itself, not a
separate endpoint.** If someone activates a Zepp Coach plan and completes a
planned session, those fields should populate. A capture of such a workout
would settle it, and is a genuinely useful contribution.

---

## Training plans — confirmed 2026-09-03

The hypothesis above is now confirmed. Once a Zepp Coach plan is active, the
workout row's plan fields populate on every subsequent run:

| Field | No plan (2026-08-08) | Plan active (2026-08-29 onward) |
|---|---|---|
| `runningProgram` | `0` | `3` |
| `runningType` | `0` | `1` or `2` |
| `dailyPlanFinished` | `False` | `True` |
| `dailyScore` | `0.0` (absent) | `98.75`–`100.0` |

`course_title` and `coachInsight` are still empty strings on every run
observed, plan or no plan — those two remain unconfirmed.

**Zepp Coach itself still has no dedicated endpoint.** A further nine routes
were probed against an account with an *active* plan — the 2026-08-17 probe
ran without one, so its all-empty result proved nothing either way. Every
route 404'd against a 404 control (`/v1/zzz/not_real.json`), and
`WatchSportStatistics/TRAINING_PLAN` returned 400, the same as a fabricated
statistic name. Plan progress is exposed entirely through the workout row;
there is no separate plan or course endpoint to query.

## A working v2 events endpoint the previous probe missed: LactateThreshold

While re-probing Zepp Coach, `eventType=LactateThreshold&subType=summary`
against `/v2/users/me/events` returned a real, structured payload:

```json
{"items": [{"value": {"samples": [
  {"lactateThresholdHr": 166, "lactateThresholdPace": 322,
   "dateString": "2026-08-29"}
]}}]}
```

This is the **authoritative** LTHR source — a dated estimate log independent
of the workout that produced it — and it had been invisible until now because
of a bug described below.

## The `_is_empty` classifier only checked for a `data` key

**This is a defect in this project's own client, not a Zepp API finding, but
it is recorded here because it is what made the LactateThreshold discovery
possible and it affects every v2 events endpoint, not just Coach-related
ones.**

`zepp_mcp/client.py::_is_empty` inspected `payload.get("data")` to decide
whether an HTTP 200 was empty. The v2 events family
(`/v2/users/me/events` — `readiness`, `hrv_sdnn`, `DailyHealth`,
`RespiratoryRate`, `LactateThreshold`, and by extension every metric spec
section 4 lists as "reachable only via `zepp_raw_request`") wraps its results
in `items`, not `data`. `payload.get("data")` on a v2 response is always
`None`, so every v2 response was reported `no_data` regardless of content.

Confirmed live before the fix: `readiness` (20 items, 16.6 KB), `DailyHealth`
(20 items, 10 KB), `hrv_sdnn` (20 items, 8.2 KB) and `RespiratoryRate` (5
items) were all classified `no_data`. Since the server instructs the model to
report `no_data` as "the query came back empty, not that the activity did not
happen," this would have had the model tell a user they had no HRV data while
20 real samples sat in the response it just discarded.

Fixed by recognising `items` alongside `data`, judging emptiness by whichever
container is present, and treating a payload with **neither** container as an
unfamiliar shape rather than assuming it is empty. See the docstring on
`_is_empty` for the full reasoning and `tests/test_decode.py`'s
`test_v2_items_response_with_data_is_not_empty` and neighbours for the
regression coverage.

**Practical effect:** every metric in the "reachable only via
`zepp_raw_request`" list is now genuinely reachable — the endpoint calls were
already correct, but the response was being discarded before a caller ever
saw it.

## Body composition scale readings — confirmed 2026-09-05

Filed as issue #7: users with a smart body-composition scale wanted weight,
body fat %, muscle mass and BMI alongside the rest of this project's data.

**The endpoint was already in this project's own corpus.** Row 4's metrics
table (`weight`, above) points at `tests/fixtures/zepp/017_weight.json`,
captured in the original recon spike:

```json
{"items": [{
  "generatedTime": 1784546800, "weightType": 1,
  "summary": {"weight": 70.0, "height": 175, "age": 21, "bmi": 27.8,
              "encryptImpedance": "0", "bodyBalanceScore": 93,
              "oneFootMeasureTime": 93.0, "source": 2}
}]}
```

from `GET /users/{uid}/members/-1/weightRecords`. It never became a tool
because that capture has no `fatRate`/`muscleRate`/`boneMass` — the account
behind it never owned a real scale; `source: 2` and (on the second item)
`thirdAppName: "Health"` mark these as manual/HealthKit-linked entries, not
a bio-impedance sync.

**Both real records also have a `bmi` that contradicts their own
`weight`/`height`** (record 1: reported 27.8 vs. 70 / 1.75² = 22.9; record
2: 26.0 vs. the same 22.9). This is not a decoding bug -- it is what the API
returned. `zepp_mcp/body.py::normalise` computes the expected BMI itself and
sets `bmi_consistent: false` on records where it disagrees, rather than
passing through a number the payload's own arithmetic contradicts.

**The full scale schema came from outside this project.** No account this
project holds has a real bio-impedance scale to capture from. GitHub code
search for `encryptImpedance` (initially suspected to be a crypto blocker —
it is not; see below) turned up `github.com/AlexxIT/SmartScaleConnect`, an
independent open-source client for the same `api-mifit.zepp.com` API, whose
README documents a real Mi Body Composition Scale 2 reading:

```json
{
  "weight": 64.7, "height": 172.0, "bmi": 21.8,
  "fatRate": 17.01331, "bodyWaterRate": 56.92887, "boneMass": 2.7305484,
  "muscleRate": 50.961838, "muscleAge": 25, "proteinRatio": 21.837502,
  "visceralFat": 9.0, "metabolism": 1358.0, "bodyScore": 89, "bodyStyle": 5,
  "standBodyWeight": 64.4, "impedance": 451, "encryptImpedance": "451"
}
```

`64.7 / 1.72² = 21.87 ≈ 21.8` — self-consistent, unlike this project's own
two captures. `encryptImpedance` is a misnomer, not a cipher: here it is the
literal string form of the plain `impedance` int next to it, "451" == 451.
Body composition is computed scale-side (or by whatever app writes the
record — the Go client's own writer pre-computes it and never sends
`impedance` at all) and stored as ordinary numeric fields; nothing about
reading it requires decryption.

**What this means for verification status.** `weight_kg`, `height_cm` and
`bmi` are cross-checked the same way SWOLF is elsewhere in this project, so
they carry no caveat. Everything else in `body_composition` — fat/water/
muscle %, bone mass, BMR, visceral fat, body score — is exposed with its
provenance stated rather than asserted as fact, because the only evidence
for those field names and units is one community-documented capture, not
this project's own data. `muscleRate` keeps its ambiguous name: even
SmartScaleConnect's own author does not know if it is a percentage or an
absolute mass ("don't know why name is rate?!").
