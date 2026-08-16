> [!WARNING]
> **Superseded and partly wrong.** This is the original draft, written before
> any request was made against the Zepp API. Its central premise — that data
> is available only on the day it is recorded, and therefore a local database
> is structurally required — is incorrect. That describes the on-watch Zepp OS
> sensor API, not the cloud REST API this project uses.
>
> See [../design-spec.md](../design-spec.md) for the corrected design and
> [../api-findings.md](../api-findings.md) for what the API actually does.
> Kept for provenance only.

# Zepp App MCP Server — Architecture Design

**Status:** Draft v1 · **Date:** 2026-08-14 · **Owner:** project maintainer

Goal: an MCP server exposing *every* piece of health data Zepp/Amazfit holds about you — not a
curated subset. Reverse-engineered against the Zepp mobile API, with a local archive so nothing
is lost to upstream retention limits.

---

## 1. Decisions

| Area | Decision | Rationale |
|---|---|---|
| Data path | **Private mobile API** (`api-mifit-*.zepp.com`, `apptoken` header) | Only path with the full metric surface |
| Language | **Python 3.11+** | All usable prior art is Python; decoders port directly |
| Storage | **SQLite — raw blobs *and* normalized tables** | Re-parseable archive; survives token death |
| Auth | **Pluggable `AuthProvider`**, manual-token first | Upstream login flow is actively hostile |
| Transport | MCP stdio (local-first) | Personal single-user server |

### Why not the official Open API

`api-open.huami.com` is proper OAuth 2.0 — registered `client_id`/`client_secret`, auth-code grant,
90-day access tokens, 10-year refresh tokens. It is stable and sanctioned. It is also:

- **Gated to corporate/institutional applicants** (`datapartner@huami.com`, 3–7 day review). Not
  available to an individual.
- **Scoped to** `profile`, `activity`, `sleep`, `heartrate`, `motion`, `sport`, `sportDetail`,
  `notifyme`.

That scope list has no SpO₂, HRV, stress, body battery, skin temperature, readiness, respiratory
rate, emotion, ODI/OSA, or PAI. It cannot satisfy the project goal. Revisit only if this ever needs
to serve users other than you.

### Why the private API's auth is not "just OAuth"

The classic huami-token flow *looks* like OAuth 2.0 — `client_id=HuaMi`, `state=REDIRECTION`,
`redirect_uri`, a `code` returned on the redirect, exchanged for `app_token`/`login_token`/`user_id`.
It is the same choreography, but `client_id` is **Zepp's own first-party app**, not one you can
register. There is no consent screen and no scope negotiation. You are standing in the app's shoes.

---

## 2. The hard constraint that shapes everything

From Zepp OS discussion #276 and confirmed across every tool surveyed:

> **Zepp health sensors expose same-day data only**, and the cloud API's retention for
> high-resolution streams is limited.

MCP is a pull protocol. The data source is same-day-and-gone. Therefore **a pass-through MCP server
is impossible** — a persistent local store is structurally required, not a performance
optimisation. Every design choice below follows from this.

Secondary constraints:

- `app_token` TTL ≈ **30 days**.
- **Region-locked hosts.** Wrong host → `403` or a silently empty `200`. The empty-200 case is the
  nastiest failure mode in this system. Bind the host at auth time; persist it with the credential;
  never guess it per-request.
- Zepp changes auth without notice (see §5).

---

## 3. Architecture

Discussion #276's three-tier Zepp OS pattern maps cleanly onto the server's internals:

| Zepp OS (#276) | This server |
|---|---|
| Device App (sensors) | **Adapters** — per-source fetchers |
| Side Service (internet bridge) | **Sync engine** — auth, region routing, backfill, rate limits |
| `settingsStorage` (K/V bus) | **Store** — SQLite, raw + normalized |
| `this.request()` / `onRequest()` | **MCP tools** |
| `addListener()` | **MCP resources** |

```
┌──────────────────────────────────────────────┐
│  MCP surface   tools · resources · schema    │
├──────────────────────────────────────────────┤
│  Query layer   normalized reads, joins, agg  │
├──────────────────────────────────────────────┤
│  Store         SQLite: raw_response          │
│                      + normalized tables     │
│                      + provenance            │
├──────────────────────────────────────────────┤
│  Decoders      band_data · data_hr · events  │
├──────────────────────────────────────────────┤
│  Sync engine   scheduling · backfill · retry │
├──────────────────────────────────────────────┤
│  Adapters      huami_private │ (openapi)     │
│                              │ (zeppos)      │
├──────────────────────────────────────────────┤
│  Auth          AuthProvider (3 impls)        │
└──────────────────────────────────────────────┘
```

### Two non-negotiable design properties

**A. Raw is archived before it is decoded.** Every HTTP response is written verbatim to
`raw_response` with its endpoint, params, fetch timestamp, and account/region — *then* decoded. When
Zepp ships a new field you haven't parsed, it is already on disk and you re-parse instead of
re-fetching data that no longer exists upstream. This is the mechanism by which "every small piece
of information" is actually achievable rather than aspirational.

**B. Auth failure degrades, never crashes.** The server starts, serves, and answers every historical
query from SQLite with a dead token. `zepp_auth_status` reports the degradation. A month of expired
credentials costs you freshness, not your archive.

---

## 4. Endpoint inventory

Base: `https://{region_host}` · Header: `apptoken: <app_token>` · Method: `GET` unless noted.

Known region hosts: `api-mifit.zepp.com`, `api-mifit-us2.zepp.com`, `api-mifit-us3.zepp.com`,
`api-mifit-de2.zepp.com`, `api-mifit-cn.zepp.com`.

### Core / bulk

| Data | Endpoint | Notes |
|---|---|---|
| Sleep + steps bulk sync | `/v1/data/band_data.json` | `query_type=summary`, `device_type=android_phone`, `userid`, `from_date`, `to_date`. **The backbone.** Contains base64 blobs |
| User profile | `/huami.health.getUserInfo.json` | |
| Manual entries | `/v1/user/manualData.json` | |
| Weight records | `/users/{uid}/members/-1/weightRecords` | `-1` = primary member |
| Blood pressure | `/users/me/bloodPressure` | |
| Heart rate samples | `/users/{uid}/heartRate` | |

### Watch statistics

| Data | Endpoint |
|---|---|
| Sport load | `/v2/watch/users/{uid}/WatchSportStatistics/SPORT_LOAD` |
| VO₂ max | `/v2/watch/users/{uid}/WatchSportStatistics/VO2_MAX` |

### Workouts

| Data | Endpoint | Notes |
|---|---|---|
| Workout history | `/v1/sport/{sport}/history.json` | `sport` defaults to `run`. UTC-midnight window |

### Event streams — three distinct families

These are the long tail. Note carefully: **there are three different event endpoints with different
shapes.** Conflating them is the most likely source of bugs.

**(a) `/users/{uid}/events`** — watch-centric timeline, millisecond epochs, `limit` + `reverse`.

| Preset | `eventType` | `subType` |
|---|---|---|
| all-day stress | `all_day_stress` | — |
| PAI | `PaiHealthInfo` | — |
| SpO₂ spot check | `blood_oxygen` | `click` |
| single stress | `single_stress` | — |
| health data | `health_data` | `blood_pressure` |

**(b) `/users/{uid}/events/dateString`** — ISO date window + explicit timezone.

| Preset | `eventType` | `subType` |
|---|---|---|
| SpO₂ ODI | `blood_oxygen` | `odi` |
| SpO₂ OSA events | `blood_oxygen` | `osa_event` |

**(c) `/v2/users/me/events`** — generic v2 stream, millisecond epochs.

| Preset | `eventType` | `subType` |
|---|---|---|
| readiness / skin temp | `readiness` | `watch_score` |
| daily health summary | `DailyHealth` | `summary` |
| body battery | `Charge` | `real_data` |
| stress | `Charge` | `stress_data` |
| HRV (SDNN) | `hrv_sdnn` | `real_data` |
| HRV (RMSSD) | `HRVRMSSD` | `real_data` |
| respiratory rate | `RespiratoryRate` | `real_data` |
| blood pressure | `blood_pressure` | `real_data` |
| emotion | `Emotion` | `real_data` |
| lactate threshold | `LactateThreshold` | `summary` |

**(d) `/users/me/fileInfo/events`** — per-second HR **file index**
(`eventType=second_heart_rate`, `subType=real_data`). Returns pointers to files, not samples —
a two-stage fetch. Highest-resolution data available.

> This inventory is provably incomplete. Zepp adds endpoints whenever it ships a UI screen. See
> `zepp_raw_request` in §7.

---

## 5. Decoding notes

### `band_data.json` summary blob

`daydata['summary']` is **base64-encoded JSON**:

```json
{ "goal": 8000, "tz": 32, "stp": {...}, "slp": {...} }
```

**`slp` (sleep):** `st`/`ed` = epoch start/end · `dp` = deep minutes · `lt` = **light** minutes ·
`stage[]` = phases with `start`/`stop` as *minute-of-day* offsets and `mode`:

| `mode` | Meaning |
|---|---|
| 4 | light |
| 5 | deep |
| 7 | awake |
| 8 | REM |

> ⚠️ **Prior-art bug to avoid.** `zepp_to_influxdb` maps `slp['lt']` to a field named
> `rem_sleep_min`. `lt` is *light* sleep; REM is `mode: 8` inside `stage[]`. Do not inherit this.
> Total sleep = `dp + lt` excludes REM/awake accounting — derive totals from `stage[]` instead.

**`stp` (steps):** total steps, distance, calories, plus activity stages with `mode`
(1 = walking, 7 = normal).

### `data_hr` blob — minute-resolution heart rate

`daydata['data_hr']` is base64 → **one unsigned byte per minute from local midnight**, 1440 bytes
per full day. Value `>= 200` is a sentinel for "no reading", not a real BPM.

> ⚠️ The prior-art implementation comments this as a "Java short / 2 bytes" but the code path
> converts after a single byte. One byte per minute is correct; the comment is wrong. Validate
> against `len(blob)` ≈ 1440 on a full day.

### Units and gotchas

- **Skin temperature** is returned in **hundredths of a degree Celsius**, as a **delta from the
  user's personal baseline** — not an absolute temperature. Accompanied by `skinTempScore` (0–100)
  and `skinTempBaseLine`. Rendering it as absolute °C is wrong and will look plausible.
- Event `extra` and `data` fields are frequently **JSON-encoded strings nested inside JSON**.
  Double-decode.
- `/users/{uid}/events` uses **millisecond** epochs; `band_data` uses **seconds**. Normalize on
  write.
- `tz` in the summary blob is an offset code, not an IANA zone.

---

## 6. Storage schema (SQLite)

```sql
-- Archive. Written before any decode. Never mutated.
CREATE TABLE raw_response (
  id           INTEGER PRIMARY KEY,
  account_id   TEXT NOT NULL,
  region_host  TEXT NOT NULL,
  endpoint     TEXT NOT NULL,
  params_json  TEXT NOT NULL,
  fetched_at   INTEGER NOT NULL,       -- epoch ms
  status_code  INTEGER NOT NULL,
  body         BLOB NOT NULL,
  body_sha256  TEXT NOT NULL,
  decoder_ver  INTEGER,                -- NULL = not yet decoded
  UNIQUE(endpoint, params_json, body_sha256)
);
CREATE INDEX idx_raw_undecoded ON raw_response(decoder_ver) WHERE decoder_ver IS NULL;

-- Long, narrow fact table. Everything time-series lands here.
CREATE TABLE sample (
  metric       TEXT NOT NULL,          -- 'heart_rate','spo2','stress','hrv_sdnn',...
  ts           INTEGER NOT NULL,       -- epoch ms, UTC
  value        REAL,
  unit         TEXT,
  resolution   TEXT,                   -- 'second','minute','event','daily'
  source_raw   INTEGER REFERENCES raw_response(id),
  attrs_json   TEXT,                   -- everything not modelled; nothing is discarded
  PRIMARY KEY (metric, ts, resolution)
) WITHOUT ROWID;

CREATE TABLE sleep_session (
  date_local   TEXT PRIMARY KEY,
  start_ts     INTEGER, end_ts INTEGER,
  deep_min     INTEGER, light_min INTEGER, rem_min INTEGER, awake_min INTEGER,
  score        INTEGER,
  source_raw   INTEGER REFERENCES raw_response(id)
);
CREATE TABLE sleep_stage (
  date_local   TEXT, start_ts INTEGER, end_ts INTEGER,
  mode         INTEGER, stage TEXT,
  PRIMARY KEY (date_local, start_ts)
) WITHOUT ROWID;

CREATE TABLE daily_summary (
  date_local   TEXT PRIMARY KEY,
  steps INTEGER, distance_m INTEGER, calories INTEGER, goal INTEGER,
  attrs_json TEXT,
  source_raw INTEGER REFERENCES raw_response(id)
);

CREATE TABLE workout (
  workout_id   TEXT PRIMARY KEY,
  sport        TEXT, start_ts INTEGER, end_ts INTEGER,
  summary_json TEXT, track_json TEXT,
  source_raw   INTEGER REFERENCES raw_response(id)
);

CREATE TABLE sync_state (
  scope TEXT PRIMARY KEY,              -- e.g. 'band_data','events:hrv_sdnn:real_data'
  last_success_ts INTEGER, last_attempt_ts INTEGER,
  cursor TEXT, last_error TEXT
);

CREATE TABLE credential (
  account_id TEXT PRIMARY KEY,
  provider TEXT, app_token TEXT, login_token TEXT,
  user_id TEXT, region_host TEXT,
  obtained_at INTEGER, expires_at INTEGER
);
```

**`attrs_json` is the point.** Any field the decoder doesn't recognise goes there verbatim rather
than being dropped. Combined with the `raw_response` archive, nothing observed is ever lost.

`decoder_ver` enables the key operation: bump the version, sweep `WHERE decoder_ver < N`, re-decode
the entire history offline with no network calls.

---

## 7. MCP tool surface

The tension: "every small piece of information" pulls toward 50+ tools; MCP context budget pulls
toward ~12. Resolution — **make the catalogue data, not tool definitions.** A `metric` enum plus a
`describe_schema` tool means depth is discovered by asking, not by preloading.

| Tool | Purpose |
|---|---|
| `zepp_auth_status` | Active provider, token expiry, bound region host, degradation state |
| `zepp_sync` | Trigger fetch. Args: `scope[]`, `from`, `to`, `force` |
| `zepp_list_devices` | Paired devices and their capabilities |
| `zepp_get_profile` | User profile |
| `zepp_query_daily` | Daily rollups across a date range |
| `zepp_get_sleep` | Session + full stage breakdown for a date range |
| `zepp_get_timeseries` | `metric` enum × `from`/`to`/`resolution`. Covers hr, spo2, stress, hrv_sdnn, hrv_rmssd, respiratory, body_battery, skin_temp, emotion, pai, readiness |
| `zepp_list_workouts` | Workout index |
| `zepp_get_workout_detail` | Full detail incl. GPS track |
| `zepp_describe_schema` | Metric catalogue: units, resolutions, coverage windows, enum decodings |
| `zepp_coverage` | What data exists for what dates — answers "do you actually have this?" before a query returns a misleading empty set |
| `zepp_raw_request` | **Escape hatch.** Authenticated passthrough inheriting region routing + token refresh + raw archiving |

**Resources:** `zepp://day/{date}` (normalized day bundle) · `zepp://raw/{date}/{endpoint}` (archived
response) · `zepp://schema` (metric catalogue).

`zepp_raw_request` is what keeps this from rotting. New Zepp endpoints appear whenever they ship a
screen; without a passthrough, each one is a server release. With it, discovery is a runtime
activity and every discovered response is archived automatically.

`zepp_coverage` exists because of the empty-200 failure mode — an agent must be able to distinguish
"you had no stress events on the 4th" from "we never successfully synced the 4th".

---

## 8. Auth design

```python
class AuthProvider(Protocol):
    name: str
    def acquire(self) -> Credential: ...
    def refresh(self, cred: Credential) -> Credential: ...
    def is_viable(self) -> bool: ...

@dataclass
class Credential:
    app_token: str
    user_id: str
    region_host: str        # bound at acquisition; never inferred per-request
    login_token: str | None
    obtained_at: int
    expires_at: int | None
```

Three implementations, resolved in order:

1. **`ManualTokenProvider`** — `app_token` + `user_id` + `region_host` from a proxy capture
   (mitmproxy + apk-mitm to defeat SSL pinning), or a HAR import. Always works. ~30-day cadence.
   **Ship this first.**
2. **`PasswordProvider`** — the huami-token flow against the post-2025 endpoints
   (`api-user.zepp.com/v2/` → `api-mifit.zepp.com/v2/client/login`). Fragile; see below.
3. **`ThirdPartyOAuthProvider`** — a *real* OAuth flow against Google/Apple, exchanging the
   resulting code at Zepp's login endpoint via the `third_name` parameter. Speculative but
   promising: the Sept-2025 hardening targeted the *password* payload, so this route may be
   untouched. Highest ceiling; prototype after v1 works.

### Upstream hostility timeline

| When | What changed |
|---|---|
| ~Sept 2025 | Login payload **encrypted**; `api-user.huami.com` → `api-user.zepp.com/v2/`; token exchange → `api-mifit.zepp.com/v2/client/login` |
| Dec 2025 | Broke again; patched via alternative ("webapp") headers |

The most actively maintained tool in this space (`zepp-health-cli`) has abandoned password login
entirely in favour of proxy capture. That is the signal driving the manual-first sequencing: a
fragile login flow must never be on the critical path to the interesting work.

---

## 9. Sync engine

- **Incremental by scope.** Each `(endpoint, eventType, subType)` triple is an independent scope
  with its own cursor in `sync_state`. One failing stream never blocks the others.
- **Backfill on first run**, oldest-first, bounded concurrency, with resume.
- **Conditional re-fetch.** `body_sha256` dedupe means re-fetching an unchanged day is cheap and
  idempotent — but *today* is always re-fetched, since same-day data is still accumulating.
- **Rate limiting + exponential backoff.** Unknown quotas; be conservative.
- **Empty-200 detection.** An empty payload on a scope that previously returned data is logged as a
  *suspected auth/region fault*, not recorded as "no data". This distinction is load-bearing.

---

## 10. Roadmap

**Phase 1 — walking skeleton.** Package scaffold, `AuthProvider` + `ManualTokenProvider`, SQLite
store with `raw_response`, `zepp_auth_status` / `zepp_raw_request` / `zepp_sync`. Goal: real bytes
from your account into the archive.

**Phase 2 — the backbone.** `band_data.json` fetch + decoders (summary blob, `data_hr`, sleep
stages). `zepp_get_sleep`, `zepp_query_daily`, `zepp_get_timeseries` for HR. Backfill your history.

**Phase 3 — the long tail.** All three event families, per-second HR two-stage fetch, workouts +
GPS, `describe_schema`, `coverage`.

**Phase 4 — durability.** `PasswordProvider`, then `ThirdPartyOAuthProvider`. Scheduled sync.
Decoder-version re-parse sweep.

**Phase 5 — live + write (optional).** The discussion #276 path: a Zepp OS Device App + Side Service
posting to a local receiver. Unlocks real-time sensors and writing *to* the watch (notifications,
toasts) — the only path that does. Deferred because it requires a separate TS toolchain and
sideloading.

---

## 11. Open questions

1. Which region host does your account bind to? Determines the whole base URL. Answered by the
   first proxy capture.
2. Does `third_name` OAuth still work post-hardening? Worth one timeboxed spike in Phase 4.
3. How far back does Zepp actually serve? Determines backfill scope and how urgent the archive is.
4. Per-second HR file index — what format are the referenced files? Needs a live capture to answer.
5. Multi-device: does your account have more than one paired watch, and do the streams collide?

---

## 12. References

- [zepp-health/discussions/276](https://github.com/orgs/zepp-health/discussions/276) — Zepp OS 3-tier architecture, the reference pattern
- [micw/hacking-mifit-api](https://github.com/micw/hacking-mifit-api) — original auth flow + summary blob structure
- [m4ary/zepp-health-cli](https://github.com/m4ary/zepp-health-cli) — most complete modern endpoint inventory
- [bentasker/zepp_to_influxdb](https://github.com/bentasker/zepp_to_influxdb) — blob decoders (with the caveats in §5)
- [argrento/huami-token](https://codeberg.org/argrento/huami-token) — token acquisition; issue #118 tracks the 2025 breakage
- [Gadgetbridge — Huami Server Pairing](https://codeberg.org/Freeyourgadget/Gadgetbridge/wiki/Huami-Server-Pairing) — BLE authkey path (path D)
- [huamitech/rest-api wiki](https://github.com/huamitech/rest-api/wiki) — official Open API (path B)
