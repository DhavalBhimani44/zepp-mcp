# Zepp MCP Server — Design Spec

**Status:** Approved · **Date:** 2026-08-15 · **Owner:** project maintainer
**Supersedes:** `ARCHITECTURE.md` (Draft v1, 2026-08-14) where the two conflict.

An MCP server exposing Zepp/Amazfit health data to an LLM. Personal use first;
open-sourced later if it works, so no decision here may depend on the maintainer's own
account, region, or machine.

---

## 1. What changed from ARCHITECTURE.md

| Area | ARCHITECTURE.md | This spec | Why |
|---|---|---|---|
| Storage | SQLite spine; every read flows through it | **No health data on disk by default**; opt-in archive, off by default | Privacy stance for the eventual open-source release; §2's justification for a mandatory store was based on a false premise (§2 below) |
| Retention premise | "Same-day-and-gone; pass-through impossible" | **Cloud serves history**; pass-through is viable | Discussion #276 describes the on-watch sensor API, not the cloud REST API |
| Auth ordering | `ManualTokenProvider` first, password login "fragile" | **`PasswordProvider` first**, manual as escape hatch | A working ~50-line implementation exists; proxy capture cannot be shipped to other users |
| Auth providers | Three | **Two** — `ThirdPartyOAuthProvider` dropped | Speculative; the doc itself calls it a post-v1 prototype |
| Tools | 12 | **8** | `zepp_sync` deleted (nothing to sync into); `zepp_coverage` folded into every response; devices+profile folded into status |
| Workouts | One endpoint, two tools, Phase 3 | **v1, sport-heterogeneous, multisport-aware** | Triathlon is the primary use case |
| Sequencing | Build Phase 1 | **Recon spike first** | Nothing has been captured; the entire endpoint inventory is unverified |
| Phase 5 (Zepp OS app) | On the roadmap | **Out of scope** | Separate toolchain, shares nothing with this design |

## 2. Corrections to ARCHITECTURE.md

These are errors in the source document, recorded so they are not re-inherited.

**§2's central premise is wrong.** The claim *"Zepp health sensors expose same-day
data only"* cites zepp-health discussion #276, which concerns the **on-watch Zepp OS
device sensor API** — a different surface from the cloud REST API this project
targets. The doc then appends an unsourced clause about cloud retention and
concludes that a persistent store is *structurally required*. It is not. The
reference implementation (below) is a pure pass-through whose headline feature is a
month of sleep history, and third parties have backfilled multi-year histories via
`band_data.json` date ranges. The "no historical data" language in Zepp's own
documentation refers to the gated Open API for third-party developers — the path
§1 already correctly rejects.

A local store remains a good idea. The justification is token death, unknown rate
limits, query latency, and archiving unparsed fields — not data loss.

**§4's workout row is incomplete.** It lists `/v1/sport/{sport}/history.json` only.
Missing: the workout *detail* endpoint (history returns an index; tracks are a
second fetch keyed by TrackID), cursor pagination at 200 records via start/stop
TrackIDs which are UNIX timestamps, and whether `sport` as a path parameter
requires enumerating every sport mode.

**§5's sentinel set is incomplete.** It documents `>= 200` as the `data_hr`
no-reading sentinel but not `0`. Filtering only the upper sentinel lets zeros
through as valid samples, which makes any `min()` over the day report 0 bpm.

**§7 frames the context-budget tension on the wrong axis.** It treats "every piece
of information" vs. context budget as a question of *tool count*. The binding
constraint is *intra-response*: a single two-hour activity at 1 Hz is thousands of
samples across many fields. No tool-count discipline saves you from that.

**Two concepts are absent entirely:** multisport/segmented activities, and any rule
governing derived versus measured values. Both are specified below.

## 3. Reference implementation review

`github.com/imastarboy97/zepp-mcp` — one commit, June 2026, ~500 lines, no storage.
Reviewed in full. It is a **reference for request shapes and a catalogue of
mistakes**, not a base to build on.

Useful:

- `zepp_client.py:46-93` implements the Sept-2025 AES-encrypted login and the
  Dec-2025 webapp-header patch. AES-CBC, key `xeNtBVqzDc6tuNTh`, POST to
  `/v2/registrations/tokens`, 303 redirect carrying `access`, exchanged at
  `/v2/client/login` with `third_name=huami` for `token_info.app_token` + `user_id`.
- It demonstrates that a pass-through server works at all, which is the evidence
  against §2.

Defects to avoid, each of which informs a rule below:

| Location | Defect |
|---|---|
| `zepp_client.py:33-39` | Four hardcoded hosts across three domains (`api-user-us2.zepp.com`, `api-mifit-us2.zepp.com`, `api-mifit.huami.com`, `api-mifit.zepp.com`), plus `country_code=US` and `region=us-west-2`. None derived from the login response. Non-US accounts will very likely see empty-200s. |
| `zepp_client.py:164` | `data.get("data", [])` renders an empty-200 as absence; `server.py:60` then reports *"No sleep data. Make sure your watch synced recently."* — a friendly, plausible, wrong message. |
| `zepp_client.py:195-209` | Docstring claims "1440 Java shorts". `x` initialises to `1` and increments to `2` on the first byte, so the branch fires every iteration and consumes one byte per minute — correct behaviour, wrong comment, inherited from `zepp_to_influxdb`. Confirms §5's warning. |
| `zepp_client.py:205` | Filters `v < 200` but not `v == 0`, so no-reading zeros become valid samples and `server.py:96` reports a minimum heart rate of 0 bpm. |
| `server.py:66-71` | Reports `dp + lt` as total sleep and never reads `stage[]`, so REM is absent from output entirely. Exactly the prior-art bug §5 warns against. |
| `server.py:42-47` | Bare `except Exception: pass` around the native-stress path silently substitutes a computed proxy. |
| `stress_analyzer.py:82` | `(8-sdsd)/8*60*0.6 + elev*2*0.4` — undocumented magic numbers emitted on the same 0–100 scale with the same Relaxed/Normal/Medium/High labels as the real sensor score. |
| `README.md:42-47` | Instructs users to put their Zepp password in plaintext in `claude_desktop_config.json`. |

## 4. Phase 0 — recon spike

**Purpose:** convert assumptions into observations before any design is committed to
code. Throwaway. Written to the scratchpad, never committed to this repository.

**Auth:** `PasswordProvider` flow, credentials read from `ZEPP_EMAIL` /
`ZEPP_PASSWORD` in the process environment. Never written to a file, never embedded
in the script.

**Probes:**

1. **Login, retaining the entire response.** Dump `token_info` in full rather than
   extracting two fields. The login request already sends a `dn` parameter listing
   candidate domains, which suggests the response carries region routing. This may
   answer region binding by itself.
2. **Region.** Issue the same `band_data.json` request against every known host from
   §4. Record status, body length, and whether each body is a real payload or an
   empty-200. Establishes both the correct binding and a captured example of the
   empty-200 failure mode.
3. **Retention.** Request `from_date=2020-01-01` to today, then binary-search the
   earliest date returning data. Settles §2 empirically.
4. **`query_type`.** ARCHITECTURE.md §4 specifies `summary`; the reference
   implementation uses `detail`. Capture both and diff the response shapes.
5. **Endpoint sweep.** One request per endpoint in §4. Record status, shape, size.
   Several are expected to fail.
6. **Event family sweep.** One request per `(eventType, subType)` pair across all
   three families. Establishes which metrics this account and watch actually
   produce — the real input to tool scoping.
7. **Workout probes.** Whether `history.json` returns all sports or only the one in
   the path; the detail endpoint and TrackID format; total activity count; whether
   the index carries sport-specific summary fields; one full detail response.
8. **Workout capture targets.** One each of: pool swim, run, walk, ride, strength
   session, football match, plus any multisport/triathlon activity if one exists.
9. **Training load.** `WatchSportStatistics/SPORT_LOAD` and `VO2_MAX` — in
   particular whether VO₂ max is reported globally or per discipline.

**Output:** one JSON file per request, timestamped, plus a findings summary.

**Constraints:** sleep between requests; do not retry the auth endpoint; quotas are
unknown and are to be treated conservatively.

**Non-goals:** no decoding, no schema, no MCP server, no error handling beyond
recording what happened.

**Second payoff:** the dump becomes the test fixture corpus (§9).

## 5. Architecture

Storage is not the spine. The read path goes to the network; the archive is a tap on
the side, disabled by default.

```
┌─────────────────────────────────────────────┐
│  MCP surface     tools · schema             │
├─────────────────────────────────────────────┤
│  Analysis        rollups, joins, patterns   │
├─────────────────────────────────────────────┤
│  Decoders        pure: bytes/dict → typed   │  ← no I/O, fixture-tested
├─────────────────────────────────────────────┤
│  Client          region-bound · cache ·     │──┐
│                  rate limit · empty-200     │  │ if enabled
├─────────────────────────────────────────────┤  ▼
│  Auth            AuthProvider (2 impls)     │  [archive: raw → disk]
└─────────────────────────────────────────────┘
```

**Modules**, each with one responsibility and a testable boundary:

- **`auth.py`** — the `Credential` dataclass and providers. `region_host` is a field
  on `Credential`, populated at acquisition from the login response. The client has
  no other way to learn a host, which makes the reference implementation's
  hardcoding defect *structurally unrepresentable* rather than merely discouraged.
  Two providers: `PasswordProvider` (happy path) and `ManualTokenProvider` (escape
  hatch for when Zepp breaks the login flow again).
- **`client.py`** — the only module that touches the network. Owns the in-memory
  cache (process lifetime, keyed on endpoint plus params), rate limiting, cursor
  pagination, and empty-200 classification.
- **`decoders/`** — pure functions, `bytes | dict → typed value`. No I/O, no
  network, no clock. Additive, never exhaustive (§8).
- **`metrics.py`** — the metric catalogue as data, preserving §7's instinct that
  depth is discovered by asking rather than preloaded into tool definitions.
  Carries units, resolution, and measured-vs-derived status per metric, per sport
  where applicable.
- **`archive.py`** — the opt-in raw sink. Imported only when enabled.
- **`analysis.py`** — rollups and pattern detection.
- **`server.py`** — MCP tool definitions. Thin.

**Caching.** In-memory only, for the process lifetime. No disk, therefore no privacy
question. Eliminates redundant round trips within a single conversation.

Cache entries covering **the current local day are held with a short TTL**;
everything older is cached for the process lifetime. Today's data is still
accumulating on the watch, so a long-lived entry would serve a step count or HR
series that is silently stale within the same conversation. This preserves
ARCHITECTURE.md §9's "today is always re-fetched" rule in a cache-only design.

**Archive (opt-in, default off).** When enabled by config, the client writes each
raw response verbatim to disk before decoding, preserving §3's property A and the
ability to re-parse history after a decoder fix. Ships disabled so the open-source
default is private by construction.

## 6. Failure semantics

**Empty-200.** Never rendered as absence. A successful request returning nothing is
classified `unknown`, not `empty`. The within-request heuristic: if a seven-day
fetch returns data for six days, day seven is plausibly genuinely empty; if it
returns zero of seven, the fault is suspected upstream and the response says so.

**Auth failure.** Degrades to a structured unavailability carrying the reason and
the action that resolves it. Never an exception, never an empty result that reads as
absence. With no archive behind it, "degrade" means *explain*, not *serve stale*.

**Partial failure.** One missing metric does not fail the response. Return what was
retrieved and name what was not.

**Derived ≠ measured.** Every value carries a `source` discriminator — `measured`,
`derived`, or `absent`. Derived values may not borrow the unit vocabulary of
measured ones: if the sensor stress score is 0–100 with Relaxed/Normal/Medium/High,
a heart-rate proxy must not also be 0–100 with those labels. No silent fallback —
if the measured path fails, the response says so, and offering a derived substitute
is a separate explicit request. **Rationale:** the consumer is an LLM that will
restate these numbers to a human as fact. It cannot see a disclaimer in a header.

**Units are attached, never implied.** Decoders return values with units. This is
what prevents §5's skin-temperature trap, where hundredths-of-a-degree-from-baseline
renders as a plausible absolute °C.

**Sentinels are a set, and gaps are represented.** `data_hr` no-reading values are
`0` *and* `>= 200`. Gaps are emitted explicitly rather than by omission, because
omission is how a minimum heart rate of 0 bpm reaches the user.

**Pace is inverted speed.** Any response carrying pace also carries total distance
and total time, so a correct aggregate is derivable from the payload rather than
dependent on the model knowing not to average paces.

## 7. Tool surface

| Tool | Purpose |
|---|---|
| `zepp_status` | Auth state, bound region host, token age, paired devices, profile basics, current reachability |
| `zepp_get_daily` | Steps, distance, calories, sleep summary, HR summary, training load across a date range |
| `zepp_get_sleep` | Sessions with the real `stage[]` breakdown — deep, light, REM, awake |
| `zepp_get_timeseries` | `metric` enum × range × resolution |
| `zepp_list_workouts` | Workout index across a date range, all sports, with sport-specific summary metrics and a sport filter |
| `zepp_get_workout` | One workout in full — segments, laps, streams, GPS — via an `include` selector |
| `zepp_describe_schema` | Metric catalogue: units, resolutions, measured-vs-derived, per-sport availability |
| `zepp_raw_request` | Authenticated passthrough inheriting region routing and token refresh |

**`zepp_sync` is deleted.** There is nothing to sync into; fetching is implicit in
every query. §9's cursor-and-backfill apparatus goes with it, except for the
pagination that moves inside `client.py`.

**`zepp_coverage` folds into every response** rather than remaining a tool. A
separate coverage tool only protects the user if the model chooses to call it, which
it will not when the first answer looked complete. Attaching the qualification to
the data makes it unmissable.

**`zepp_list_devices` and `zepp_get_profile` fold into `zepp_status`** — together
they answer one question.

**`zepp_raw_request` is retained and is more important than in the original design.**
With no archive and no sync, it is the only route by which a newly-shipped Zepp
endpoint reaches the user without a release.

**Responses are JSON-serialised structured data, not pre-formatted markdown.** MCP
tool results are text either way; the distinction is that the payload is a
machine-readable object rather than rendered prose, so the `source` discriminator,
units, and empty-200 qualification survive as fields the model must read rather than
formatting it may skim past. Rendering is the model's job, not the server's.

**The `metric` enum is populated from what the spike proves the watch produces**, not
from §7's aspirational list.

## 8. Workouts, sports, and multisport

**Sport-heterogeneous by nature.** Pool swimming yields SWOLF, stroke count, stroke
rate, stroke type, distance-per-stroke, pool length and per-lap splits. Running
yields cadence, stride length, ground contact time, vertical oscillation. Cycling
yields power and speed. Strength yields sets and reps. Football yields sprint counts
and time in HR zones. These barely overlap.

**Therefore decoders are additive, never exhaustive.** Known fields are typed and
unit-attributed; every unrecognised field passes through verbatim under a `raw` key.
This is `attrs_json` from §6, relocated from the storage layer to the response
layer — the mechanism by which "every small piece of information" survives the
deletion of the archive. It also means data from a sport nobody has written a
decoder for is available on day one.

**Every workout has `segments[]`.** A single-sport activity is the degenerate case
with one segment. A triathlon is swim, T1, bike, T2, run — five segments, each with
its own sport type and metric set, transitions included as first-class segments with
their own durations rather than gaps to be inferred. Uniform shape, no
special-casing. If the spike finds no multisport activities on the account, the
model degenerates harmlessly at zero cost.

**Detail is a component selector, not a level.** `include=["summary", "laps",
"streams", "gps"]`, applied per segment.

- `summary` and `laps` default **on**. A forty-lap swim is forty rows carrying
  SWOLF, stroke type, stroke count and split time — small, and nearly pure signal.
- `streams` and `gps` default **off**, and downsample when requested. A forty-minute
  swim's raw stream is thousands of samples that are mostly redundant.

Per-segment selection makes *"the swim leg's lap splits but not the bike leg's GPS"*
expressible, which for a five-hour race is necessary rather than convenient.

**Sport aggregates belong in the index.** `zepp_list_workouts` returns
sport-specific summary metrics and accepts a sport filter, so *"how has my SWOLF
trended over three months"* and *"swim vs bike vs run volume this month"* are one
call rather than forty detail fetches. Whether `history.json` already carries that
richness is a spike probe.

**Cross-discipline training load.** `SPORT_LOAD` and `VO2_MAX` are the triathlon
picture rather than footnotes. VO₂ max may be reported per discipline rather than
globally, which would make it two-dimensional; the spike resolves this.

**Known cost of the storage decision.** Workout detail is immutable — a ride from
2024 will never change — which makes it the ideal cache target, unlike a step count
that is still accumulating. Combined with 200-record cursor pagination and a
multi-year training archive, *"show my season"* is expensive on every ask. The
opt-in archive covers this. Recorded here as an accepted cost, not a surprise.

## 9. Testing

The spike's captured JSON is the fixture corpus. Decoders are pure functions over
it, so the decoder suite runs with no network, no token, and no clock.

Regression tests written from real bytes for each defect found in prior art:

- `data_hr` sentinels at both `0` and `>= 200`, with gaps represented.
- Sleep totals and REM derived from `stage[]`, not `dp + lt`.
- Skin temperature as a baseline delta in hundredths of a degree, not absolute °C.
- Millisecond epochs (`/users/{uid}/events`) versus second epochs (`band_data`)
  normalised on decode.
- Double-encoded JSON in event `extra` and `data` fields.

Client-layer tests run against recorded responses. The case that matters most: an
empty-200 must produce `unknown`, never `empty`.

Every workout decoder is tested against a real captured activity of that sport,
including one multisport activity if the account has one.

## 10. Out of scope

- **Zepp OS device app + side service** (ARCHITECTURE.md Phase 5). Separate
  TypeScript toolchain, requires sideloading, shares nothing with this design. A
  different project.
- **`ThirdPartyOAuthProvider`.** Speculative; a timeboxed spike at best.
- **Export to GPX/FIT/TCX.** Natural follow-on, but requires writing files and is
  not needed to answer questions.
- **Hosted multi-user deployment.** Would invert the entire privacy stance.
- **The official Open API.** Gated to institutional applicants and scoped too
  narrowly, as ARCHITECTURE.md §1 correctly establishes.

## 11. Questions the spike resolves

Every one of these is answered by Phase 0 before any production code is written.

1. Which region host the account binds to, and whether the login response carries it.
2. How far back the cloud actually serves — the empirical replacement for §2.
3. Whether the password login flow still works in August 2026.
4. Which of the candidate metrics this watch actually produces.
5. `query_type=summary` versus `detail` — which is correct, and how they differ.
6. Whether `history.json` covers all sports or requires per-sport enumeration.
7. The workout detail endpoint, TrackID format, and pagination behaviour.
8. Whether the workout index carries sport-specific summary metrics.
9. Whether VO₂ max is global or per discipline.
10. Whether the account holds any multisport activities.
11. Whether more than one device is paired, and whether streams collide.
12. The real payload shape of event `data` and `extra` fields.

## 12. References

- `ARCHITECTURE.md` — Draft v1, corrected by §2 above
- [zepp-health/discussions/276](https://github.com/orgs/zepp-health/discussions/276) — on-watch Zepp OS architecture; **not** evidence about cloud retention
- [imastarboy97/zepp-mcp](https://github.com/imastarboy97/zepp-mcp) — reference implementation reviewed in §3
- [micw/hacking-mifit-api](https://github.com/micw/hacking-mifit-api) — original auth flow and summary blob structure
- [m4ary/zepp-health-cli](https://github.com/m4ary/zepp-health-cli) — endpoint inventory
- [bentasker/zepp_to_influxdb](https://github.com/bentasker/zepp_to_influxdb) — blob decoders, with the §5 caveats
- [argrento/huami-token](https://codeberg.org/argrento/huami-token) — token acquisition
- [Roland Szabó — Export Mi Fit and Zepp workout data](https://rolandszabo.com/posts/export-mi-fit-and-zepp-workout-data/) — workout pagination and TrackID semantics
