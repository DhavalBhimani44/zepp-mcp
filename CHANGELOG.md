# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
