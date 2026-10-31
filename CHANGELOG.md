# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Four tools. `zepp_health_metrics` covers readiness, HRV, resting heart
  rate, respiratory rate, stress, SpO₂, PAI, Body Charge and daily goals.
  `zepp_workout_stats` gives volume, records and progression series.
  `zepp_period_summary` is a one-call overview. `zepp_profile` returns the
  body profile and paired devices. Every new decoder is pinned to a second
  quantity in the fixture corpus; see `docs/capability-matrix.md`.
- `zepp_sleep` accepts a date range and joins each night with that
  morning's readiness (overnight HRV), sleep-window respiratory rate and
  overnight SpO₂ (ODI). Nights also carry sleep latency, time in bed and
  efficiency.
- `zepp_daily_summary` takes any range (fetched in 31-day chunks). It adds
  `activity_periods`, and hourly steps with `include_hourly`.
- `zepp_list_workouts` follows history pagination and accepts `offset` and
  numeric sport codes. Workouts now carry moving, paused and total time,
  high-precision distance, a derived pace, strength sets and reps,
  DEM-corrected elevation and `sport_name_confidence`.
- `zepp_workout_detail` returns strength `sets` (reps, load, rest per set),
  km `splits`, `pauses` and a decoded `gait` stream (cadence, stride).
- A sport registry (`zepp_mcp/sports.py`) naming 141 workout codes. 9 are
  confirmed on this project's data, 7 corroborated by independent clients,
  11 reported once, and 112 catalogued from the Zepp OS sport list via the
  cloud-code == watch-id rule (verified on 18, 21 and 52). Contested codes
  (13, 17) stay `unknown_sport_<code>` with candidate names. Every workout
  now carries `sport_name_confidence`, `sport_family` and, for codes short
  of confirmed, `other_metrics`. Sport filters accept families
  (`sport="cycling"`), and `zepp_workout_stats` groups by
  `sport_family`. `ZEPP_SPORT_CODES` names codes locally.
- `zepp_training_thresholds` calls `WatchSportStatistics` with the
  parameters other clients use and returns the result undecoded.
- Metric catalogue (`zepp_mcp/metrics.py`) with verification levels 1–5,
  served by `zepp_describe_schema`.
- Client: in-memory response cache (5 min for the last week and for empty
  answers, 1 h for older ranges, never on disk), request pacing,
  error classification (`error_kind`), paginated events, and
  partial-result reporting.
- `tools/probe_candidates.py`, a controlled probe of open endpoint
  hypotheses, and opt-in live tests (`ZEPP_LIVE=1`).
- `ZEPP_TIMEZONE`, `ZEPP_RESPONSE_CACHE` and `ZEPP_MIN_REQUEST_INTERVAL`
  settings.

- `zepp_body_composition`: smart-scale weight, height and BMI (verified),
  plus body fat/water/muscle %, bone mass, visceral fat rating and basal
  metabolism when synced from a real bio-impedance scale rather than a
  manual entry (#7). `bmi_consistent` flags records whose own weight,
  height and bmi don't reconcile with each other.
- Two more sport codes: 21 (`rope_skipping`), confirmed from the row's own
  `rope_skipping_`-prefixed `pb` keys, surfacing total jump count and
  longest unbroken streak; and 16 (`free_training`), confirmed by
  elimination rather than a positive payload signature — see
  `zepp_describe_schema` known_gaps.

### Fixed

- `run_distance_metres` was `stp.runDist`, which is about 80% of the day's
  distance even on days with no run. It is now `runDist_raw_unverified`.
- Strength workouts' laps were decoded with swim column names. They now
  come back as `sets`.
- Legacy timezone names (`Asia/Calcutta`) fell back to UTC on systems
  without the alias, shifting times by the UTC offset.
- Error notes no longer echo the user id in request paths.
- On Windows, which ships no timezone database, every IANA zone silently
  fell back to UTC and moved workouts and daily scores onto the wrong day.
  `tzdata` is now a Windows-only dependency.
- Without `ZEPP_TIMEZONE`, the server now uses the machine's named zone
  ($TZ or /etc/localtime) instead of a fixed UTC offset, which was wrong
  across DST changes.

- Login no longer registers the app token under `com.huami.midong`, the real
  Zepp Android app's package id. Huami's backend appears to key an active
  session by `(user, app_name)`, so logging in as the phone's own app evicted
  the phone's session (#5). Login now identifies as the retired Mi Fit client
  id, and the device id is held stable across logins instead of being
  randomized on every call.

### Security

- `kilo_pace`/`mile_pace` embed a geohash of each split's end location. They
  were passed through raw by `include=["streams"]`. They are now decoded
  with that column removed and never returned as raw streams.
- Device records carry the Bluetooth pairing key (`auth_key`), so device
  output goes through a field allowlist. Profile output omits name, avatar
  and location.

## [0.1.0] - 2026-08-16

First public release.

### Added

- MCP server over stdio exposing eight tools: `zepp_daily_summary`,
  `zepp_sleep`, `zepp_heart_rate`, `zepp_list_workouts`,
  `zepp_workout_detail`, `zepp_describe_schema`, `zepp_raw_request` and
  `zepp_auth_status`.
- Password login with region discovery read from the login response, and a
  token cache at `~/.zepp-mcp/token.json` (mode `0600`).
- Decoders for daily band data, sleep stages, per-minute heart rate, workout
  index rows and workout detail streams and laps.
- Sport-aware workout summaries for swimming, running, walking, hiking and
  strength training.
- 28 tests running against an anonymised corpus of real API captures, with no
  network or credentials required.
- `tools/check_fixtures.py`, a CI-enforced privacy gate over the corpus.
- `tools/anonymize_fixtures.py` and `tools/smoke_test.py`.

### Security

- Fixture corpus anonymised: identity, biometrics and hardware identifiers
  removed, including values hidden inside base64-wrapped JSON.
- GPS-bearing workouts excluded from the corpus entirely.
- HTTP request logging disabled by default, since the request line carries the
  user id.

[Unreleased]: https://github.com/DhavalBhimani44/zepp-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/DhavalBhimani44/zepp-mcp/releases/tag/v0.1.0
