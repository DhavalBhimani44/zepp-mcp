# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `zepp_body_composition`: smart-scale weight, height and BMI (verified),
  plus body fat/water/muscle %, bone mass, visceral fat rating and basal
  metabolism when synced from a real bio-impedance scale rather than a
  manual entry (#7). `bmi_consistent` flags records whose own weight,
  height and bmi don't reconcile with each other.

### Fixed

- Login no longer registers the app token under `com.huami.midong`, the real
  Zepp Android app's package id. Huami's backend appears to key an active
  session by `(user, app_name)`, so logging in as the phone's own app evicted
  the phone's session (#5). Login now identifies as the retired Mi Fit client
  id, and the device id is held stable across logins instead of being
  randomized on every call.

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
