<div align="center">

# zepp-mcp

**Read your Zepp / Amazfit health and workout data from any MCP client.**

[![CI](https://github.com/DhavalBhimani44/zepp-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/DhavalBhimani44/zepp-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-server-000000.svg)](https://modelcontextprotocol.io)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![Code of Conduct](https://img.shields.io/badge/Contributor%20Covenant-2.1-4baaaa.svg)](CODE_OF_CONDUCT.md)

</div>

---

Ask your assistant *"how has my SWOLF trended this month?"* or *"did I sleep
worse on days I trained hard?"* and get answers from your own watch data.

```
› How have my swims gone this week?

  Date     Distance   SWOLF   Strokes   Avg HR
  Aug 16      567 m      36       323      131
  Aug 15      756 m      38       481      139
  Aug 14      630 m      38       361      138

  SWOLF is trending down across the week — you're covering
  each length with fewer strokes and less time.
```

Built on the private Zepp mobile API, with every decoder verified against
real captured responses.

> [!IMPORTANT]
> **Unofficial.** This project is not affiliated with, endorsed by, or
> supported by Zepp Health or Huami. It talks to a private API that can change
> or break without notice. See [Legal and safety](#legal-and-safety).

## Contents

- [Features](#features) · [Setup for Users](#setup-for-users) · [Developer Setup](#developer-setup)
- [Tools](#tools) · [Privacy](#privacy) · [How it works](#how-it-works)
- [Known gaps](#known-gaps) · [Contributing](#contributing) · [Legal and safety](#legal-and-safety)

## Features

- **All sports in one call.** Swimming, running, walking, hiking and strength
  work, each with its own metrics — SWOLF and stroke counts for swims, pace
  and cadence for runs, set counts for the gym.
- **Sleep with all four stages.** Light, deep, REM and awake, not just the
  two most implementations report.
- **Per-minute heart rate**, with no-reading markers preserved as `null`
  rather than dropped.
- **Lap and stream decoding** for individual workouts.
- **Honest about uncertainty.** Unverified units are flagged, unknown sport
  codes are named as unknown, and an empty response is never reported as
  confirmed absence.
- **Nothing stored.** No health data touches disk. Only the API token is
  cached, so restarts don't trigger a fresh login.

## Setup for Users

If you just want to use the server with your MCP client, you don't need to clone the repository. You can run it directly using `uvx` (the [uv](https://docs.astral.sh/uv/) tool runner).

<details open>
<summary><b>Claude Desktop</b></summary>

Add the server to your `claude_desktop_config.json` and pass your credentials securely via environment variables:

```json
{
  "mcpServers": {
    "zepp": {
      "command": "uvx",
      "args": ["zepp-mcp"],
      "env": {
        "ZEPP_EMAIL": "your-email@example.com",
        "ZEPP_PASSWORD": "your-password"
      }
    }
  }
}
```

- **macOS** · `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows** · `%APPDATA%\Claude\claude_desktop_config.json`

Restart the app completely afterwards.

</details>

<details>
<summary><b>Claude Code</b></summary>

Install the server globally for Claude Code. Make sure to pass your credentials securely as environment variables:

```bash
export ZEPP_EMAIL="your-email@example.com"
export ZEPP_PASSWORD="your-password"
claude mcp add zepp -s user -- uvx zepp-mcp
```

`-s user` makes it available in every project. Verify with `claude mcp list`.

</details>

<details>
<summary><b>Antigravity CLI</b></summary>

Create or edit `~/.gemini/config/mcp_config.json` (global, applies to all projects):

```json
{
  "mcpServers": {
    "zepp": {
      "command": "uvx",
      "args": ["zepp-mcp"],
      "env": {
        "ZEPP_EMAIL": "your-email@example.com",
        "ZEPP_PASSWORD": "your-password"
      }
    }
  }
}
```

For a single project only, place the same file at `.agents/mcp_config.json` in
your project root instead.

Restart the Antigravity session afterwards — MCP servers are loaded at startup.

</details>

> [!WARNING]
> Zepp counts failed logins against a **shared 10-attempt lockout**. This
> server never retries a failed login, and you shouldn't either. If
> authentication fails, check your credentials carefully before trying again.

## Developer Setup

If you want to contribute, run tests, or modify the code locally. Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
git clone https://github.com/DhavalBhimani44/zepp-mcp.git
cd zepp-mcp
uv sync

cp .env.example .env      # add ZEPP_EMAIL and ZEPP_PASSWORD
chmod 600 .env
```

Verify it works — the test suite needs no network and no credentials:

```bash
uv run pytest              # 28 tests against the fixture corpus
uv run tools/smoke_test.py # starts the server, lists its tools
```

Then check your account connects:

```bash
uv run python -c "from zepp_mcp.server import zepp_auth_status; print(zepp_auth_status())"
```

### Connecting a local clone

If you're testing your local clone, configure your client to use the local directory instead of `uvx`.

<details>
<summary><b>Claude Desktop</b></summary>

```json
{
  "mcpServers": {
    "zepp": {
      "command": "uv",
      "args": ["--directory", "/path/to/zepp-mcp", "run", "zepp-mcp"]
    }
  }
}
```
*(Credentials are read from your local `.env`, so they stay out of the config file)*

</details>

<details>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add zepp-local -s user -- uv --directory /path/to/zepp-mcp run zepp-mcp
```

</details>

<details>
<summary><b>Antigravity CLI</b></summary>

Add to `~/.gemini/config/mcp_config.json` (global) or `.agents/mcp_config.json` (workspace):

```json
{
  "mcpServers": {
    "zepp": {
      "command": "uv",
      "args": ["--directory", "/path/to/zepp-mcp", "run", "zepp-mcp"]
    }
  }
}
```

Credentials are read from your local `.env`.

</details>

## Tools

| Tool | Returns |
| --- | --- |
| `zepp_daily_summary` | Steps, distance, calories and sleep per day |
| `zepp_sleep` | One night: light / deep / REM / awake minutes, score, resting HR |
| `zepp_heart_rate` | Per-minute heart rate for a day, plus statistics |
| `zepp_list_workouts` | All workouts, all sports, with sport-specific metrics |
| `zepp_workout_detail` | Laps, time-series streams and GPS for one workout |
| `zepp_describe_schema` | What the server knows, and where decoding is uncertain |
| `zepp_raw_request` | Arbitrary GET, for endpoints not modelled yet |
| `zepp_auth_status` | Token expiry and region host |

## Privacy

This is health data. The design reflects that.

- **No health data is written to disk.** Every call fetches live.
- **Only the API token is cached**, at `~/.zepp-mcp/token.json` (mode `0600`).
  Set `ZEPP_TOKEN_CACHE=off` to disable and log in every time.
- **Credentials live in `.env`**, never in client config files, never in
  source, never in logs.
- **The fixture corpus is anonymised and CI-gated.** Captures come from a real
  account, so `tools/check_fixtures.py` runs on every push and fails the build
  on any name, MAC address, serial number, email, coordinate stream or
  credential-shaped token. GPS-bearing workouts are excluded entirely — a
  running route starts where you live.

If you contribute a fixture, run `uv run tools/anonymize_fixtures.py` first,
and install the pre-push hook — force pushes are blocked on `main`, so a bad
push cannot be rewritten away:

```bash
ln -sf ../../tools/hooks/pre-push .git/hooks/pre-push
```

See [CONTRIBUTING.md](CONTRIBUTING.md#adding-fixtures).

## How it works

A few decisions are load-bearing, and each came from evidence rather than
assumption.

<details>
<summary><b>An empty 200 is not an answer</b></summary>

Zepp returns HTTP 200 with an empty body both for "no data in this range" and
for requests it silently rejects. The two are indistinguishable from a single
response, so the client reports `status: "no_data"` with that ambiguity
attached, and the server instructs the model not to state it as confirmed
absence. Rendering it as "you didn't exercise that week" turns a fault into a
fact.

</details>

<details>
<summary><b>Stream encoding is per field, not global</b></summary>

`heart_rate` and `temperature` are delta-encoded; `currentDistance` and
`speed` are absolute. Decoding one as the other yields entirely plausible
numbers, so each was settled by decoding both ways and checking against the
workout's own summary totals — `currentDistance` resolves to 75600 cm against
a reported 756 m, and `speed` integrates to 1061 m against a reported 1064 m.

</details>

<details>
<summary><b>Sleep has four stages, not two</b></summary>

Stage modes 4/5/8/7 are light/deep/REM/awake, verified by recomputing each
night's stage minutes and matching the summary's own `lt`/`dp`/`dt`/`wk`
fields across three nights. Reporting deep + light as total sleep silently
drops REM — 73, 53 and 96 minutes on those nights.

</details>

<details>
<summary><b>Sentinels are a set, not a value</b></summary>

`-1` is the general not-applicable marker, but altitude uses `-20000`, angle
`-361`, elevation `-100`, temperature `-274` (below absolute zero), and SpO₂
uses both `-1` and `0`. They are stripped per field family. Values also
arrive as strings about half the time (`dis` is `"756.0"`), so stripping
coerces before comparing — otherwise `swolf: "-1"` survives onto a bike ride.

</details>

<details>
<summary><b>Units travel with the number</b></summary>

`elevationGain` is centimetres: a hike reporting `27961` sits beside its own
`altitude_ascend: 279` in the same row. It is converted and renamed to
`elevation_gain_metres`, because `27961` emitted raw reads as a plausible
metre figure and turns a 280 m hill into an alpine ascent.

</details>

<details>
<summary><b>Times are local</b></summary>

Workouts carry `syncedTimezone`; daily data carries a `tz` offset in seconds.
Rendering in UTC turns an 08:05 swim into 02:35 and moves a 00:20 bedtime to
the previous evening.

</details>

<details>
<summary><b>Unknown stays unknown</b></summary>

Sport codes are numeric with no name anywhere in the payload, so the map was
built by confirming each code against the Zepp app. An unrecognised code
reports as `unknown_sport_<code>` rather than a guess. Streams with
unconfirmed units carry `unit_verified: false`, and unrecognised streams are
returned raw rather than decoded with an assumed encoding.

</details>

## Known gaps

Documented rather than hidden — `zepp_describe_schema` reports these at call
time too.

| Gap | Detail |
| --- | --- |
| Lap column names | Columns 1, 13 and 14 are confirmed (duration, strokes, SWOLF — see [examples](examples/README.md#how-this-example-resolved-a-documented-gap)). The remaining named columns are inferred, and anything outside the named set is returned raw. |
| `pool_swim_pace` | Unit unconfirmed; flagged `unit_verified: false`. |
| VO₂ max, training load | `SPORT_LOAD` and `VO2_MAX` return HTTP 500. |
| Some endpoints | `manualData`, `bloodPressure` and `heartRate` return HTTP 400 — they need parameters not yet worked out. |
| **Cycling** | **No cycling sport code identified** — no ride has been recorded against this code yet. A ride will report as `unknown_sport_<code>`; cadence, power and heart rate still surface via the common block, and the rest under `unclassified_metrics`. Report your code in an issue and it becomes a one-line fix. |
| Multisport | `parent_trackid` / `child_list` handling is built but untested; no triathlon has been recorded yet. |
| Running depth | Confirmed on a single run: pace (s/m), stride length (cm), cadence and power all reconcile against the row's own distance, duration and step count. More runs would broaden that. |
| GPS decoding | Untested. The corpus deliberately excludes GPS-bearing workouts. |
| Training plans / Zepp Coach | No endpoint found across 13 candidate routes ([details](docs/api-findings.md#training-plans-and-zepp-coach--probed-2026-08-17)). Workout rows do carry `coachInsight`, `course_title` and `degreeOfCompletion`, all empty on an account with no plan — so plan data likely arrives through the workout row rather than a separate endpoint. A capture from an active plan would confirm it. |
| Metrics without tools | PAI, SpO₂, stress, HRV, respiratory rate, readiness, Body Charge and weight all return data but are reachable only via `zepp_raw_request`. |

Help with any of these is welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Examples

Three runnable analyses, with real output, live in
[`examples/`](examples/README.md): swim technique against volume, training
load against recovery, and within-session pacing decay.

## Project layout

```
zepp_mcp/           the server
  auth.py           password login, region discovery, token cache
  client.py         HTTP, empty-200 classification, re-auth
  decode.py         band data, workout streams, laps
  workouts.py       index row -> normalised, sport-aware summary
  codes.py          sport codes, sleep stages, sentinels
  server.py         MCP tool definitions
examples/           runnable analyses with real output
tools/              anonymiser, privacy gate, smoke test
tests/fixtures/     anonymised real API captures
docs/               design spec and API reverse-engineering findings
spike/              the throwaway probe that produced the fixtures
```

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
development setup, testing approach, and the rules around fixtures and
personal data. By participating you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md).

Especially useful: sport codes from watches other than the maintainer's, and
confirmation of the units flagged above.

## Legal and safety

- **Not medical advice.** This is a data-access tool. Nothing it returns is a
  diagnosis, and it should not be used to make medical decisions.
- **Unofficial and unsupported.** Not affiliated with Zepp Health or Huami.
  The API is private and may change or break at any time.
- **Your account, your responsibility.** Review Zepp's terms before use. The
  server is read-only and never modifies your account, but automated access
  may not be something they permit.
- **No warranty.** See [LICENSE](LICENSE).

## Security

To report a vulnerability, see [SECURITY.md](SECURITY.md). Please don't open a
public issue for anything credential-related.

## License

[MIT](LICENSE) © 2026 Dhaval Bhimani
