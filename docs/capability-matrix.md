# Capability matrix

The single source of truth for what this server can read from Zepp, how
sure we are, and what is still open. `zepp_mcp/metrics.py` is the
machine-readable twin that `zepp_describe_schema` serves at call time. When
one changes, change the other.

**Last updated:** 2026-10-31 · **Tools:** 14 · **Tests:** 220 offline (+17
opt-in live)

## Verification levels

| Level | Meaning |
|---|---|
| **1 — Live verified** | Confirmed against live API responses from this project's account. |
| **2 — Fixture verified** | Confirmed against a recorded real response, **by a second, independent quantity** in the corpus (another endpoint, the payload's own totals). A name alone is never enough. |
| **3 — Corroborated** | Supported by another credible open-source client, not yet verified here. |
| **4 — Hypothesis** | Plausible from the payload, awaiting a capture that can confirm it. |
| **5 — Unsupported** | Probed. Not accessible, or unresolved. |

Something that appears in another repository is level 3 at best until it
is checked against data here. The 2026-10-31 expansion session had **no
account credentials**, so nothing it added is level 1. Everything it marks
level 2 was checked against the committed corpus. The level-3 and level-4
rows are exactly what [`tools/probe_candidates.py`](../tools/probe_candidates.py)
exists to settle.

---

## Daily activity

| Capability | Endpoint | Lvl | Evidence | Decoder | MCP tool | Units | Tests | Limitations |
|---|---|---|---|---|---|---|---|---|
| Steps, distance, active calories | `band_data.json` `stp.ttl/dis/cal` | 2 | Equals DailyHealth `totalSteps`/`totalCalories` on 7/7 shared days | `decode.summarise_day` | `zepp_daily_summary` | steps, m, kcal | `test_daily_health_totals_equal_band_data` | `cal` is **active** calories (250–500 on rest days), not total energy. |
| Per-minute and hourly steps | `band_data.json` `data`, byte 2 of 3 | 2 | Bytes sum to `ttl` on 8/8 days; reproduce **97/97** step-stage totals | `decode.decode_activity_minutes`, `hourly_steps` | `zepp_daily_summary(include_hourly)` | steps | `test_per_minute_steps_sum_*`, `test_every_step_stage_*` | Bytes 0 and 1 (activity kind and intensity) are returned raw and not decoded. Other clients read byte 0's low nibble as a kind code (level 3). |
| Activity periods by mode | `stp.stage[]` | 2 | Stage totals reproduce from per-minute steps | `decode._activity_periods` | `zepp_daily_summary` | min, steps, m, steps/min | `test_running_stage_has_running_cadence` | Mode **names** are inferred. 4 = running (~150 spm, 2.4 m/s) and 3 = walking (~90–100 spm), both matching three external clients. 1 and 7 are low-cadence and left unnamed. |
| Daily goals and progress | v2 `DailyHealth/summary` | 2 | Totals equal band_data | `events.daily_health` | `zepp_health_metrics(daily_goals)` | steps, kcal, min, m | `test_daily_health_*` | `totalRunningDistance` is the day's **on-foot workout** distance, walks included (1064 m on a walk day), so it is renamed. |
| Active minutes | `DailyHealth totalBurningDuration` | 2 | Same record as above | `events.daily_health` | `zepp_health_metrics(daily_goals)` | min | ″ | Zepp's own "fat-burning minutes"; the definition is not published. |
| `runDist` | `stp.runDist` | 4 | 79–81% of `dis` on every day, **including days with no run** | — | `zepp_daily_summary` (`runDist_raw_unverified`) | ? | `test_rundist_is_no_longer_*` | Was mislabelled `run_distance_metres` before 2026-10-31. Meaning unknown. |
| Standing / sedentary time | — | 5 | No field identified in band_data or the events captured | — | — | — | — | Needs a capture from a watch that reports it. |

## Sleep

| Capability | Endpoint | Lvl | Evidence | Decoder | MCP tool | Units | Tests | Limitations |
|---|---|---|---|---|---|---|---|---|
| Light / deep / REM / awake | `band_data.json` `slp` + `stage[]` | 2 | Stage list recomputes all four totals | `decode._sleep_block` | `zepp_sleep`, `zepp_daily_summary` | min | `test_sleep_stage_modes_*` | — |
| Score, resting HR, wake count, naps | `slp.ss/rhr/wc/odd_stage` | 2 | `rhr` equals readiness `sleepRHR` 6/6 | ″ | `zepp_sleep` | 0–100, bpm | `test_readiness_sleep_rhr_*` | — |
| Latency, time in bed, efficiency | `slp.is/obt/ebt/lb` | 2 | `st == obt + is`, `ebt == ed + lb` and `lt+dp+dt+wk == (ed−st)/60` hold on **7/7 nights** | `decode._bed_window` | `zepp_sleep` | min, % | `test_bed_window_identities_*` | Efficiency is derived (asleep ÷ in bed). |
| Multi-night history and averages | band_data, 31-day chunks | 2 | — | `client.band_data_range` | `zepp_sleep(from_date,to_date)` | — | `test_sleep_range_*` | Missing nights are counted separately, never averaged as zero. |
| Overnight HRV | v2 `readiness` `sleepHRV` | 2 | Equals the mean per-minute RMSSD inside the sleep window, within 1 ms, **6/6 nights** | `events.readiness` | `zepp_sleep`, `zepp_health_metrics(hrv)` | ms | `test_readiness_sleep_hrv_is_mean_*` | — |
| Sleep respiratory rate | v2 `RespiratoryRate` within the sleep window | 2 | First reading of the night lands on band_data sleep onset | `events.respiratory_rate`, `events.within` | `zepp_sleep` | breaths/min | `test_respiratory_rate_minutes_begin_*` | Derived from per-minute samples. |
| Sleep breathing (ODI, score) | `events/dateString` `blood_oxygen/odi` | 2 | `odi == odiNum ÷ (cost/3600)`; `cost` equals the sleep duration | `events.overnight_spo2` | `zepp_sleep`, `zepp_health_metrics(spo2)` | events/h, 0–100 | `test_odi_is_events_per_hour_*` | `valid` is a code with conflicting external readings, so it is passed through raw. |
| OSA events | `blood_oxygen/osa_event` | 5 | Empty 200 on this account | — | `zepp_raw_request` | — | — | An empty 200 is not evidence either way. |
| Sleep regularity | — | 5 | Not identified | — | — | — | — | Can be derived from `start_local` across nights; no Zepp field. |

## Heart and cardiovascular

| Capability | Endpoint | Lvl | Evidence | Decoder | MCP tool | Units | Tests | Limitations |
|---|---|---|---|---|---|---|---|---|
| Per-minute heart rate | `band_data.json` `data_hr` | 2 | 1440 bytes; 0/254/255 are sentinels | `decode.decode_hr_minutes` | `zepp_heart_rate` | bpm | `test_hr_minutes_*` | — |
| Resting HR trend | `slp.rhr` per night | 2 | Equals readiness `sleepRHR` | `server._night` | `zepp_health_metrics(resting_heart_rate)` | bpm | `test_every_health_metric_*` | Includes a derived trend line. |
| HRV RMSSD (per minute) | v2 `HRVRMSSD/real_data` | 2 | See overnight HRV | `events.hrv_rmssd` | `zepp_health_metrics(hrv)` | ms | `test_rmssd_samples_*` | All-day figures are derived and noisier than the overnight value. |
| HRV SDNN | v2 `hrv_sdnn/real_data` | 4 | Unit taken from the field name and its 20–100 range | `events.hrv_sdnn` | `zepp_health_metrics(hrv)` (`sdnn_ms_unit_unverified`) | ms? | `test_sdnn_samples_decode` | Nothing in the corpus to cross-check. |
| Time in HR zones per workout | history `heart_range` | 2 | Zone seconds total 3909 against `run_time` 3921 | `decode.decode_heart_zones` | `zepp_list_workouts` | s | existing | Zone limits are the watch's own. |
| Lactate threshold HR and pace | v2 `LactateThreshold/summary` | 1 | Live capture, 2026-09-03 | `client.parse_lactate_threshold_events` | `zepp_training_thresholds` | bpm, s/km | existing | — |
| VO₂ max | history `VO2_max` | 2 | Per-workout copy of a rolling estimate | `workouts` | `zepp_training_thresholds`, `zepp_workout_stats(metric=vo2_max)` | ml/kg/min | existing | — |
| VO₂ max / training-load series | `WatchSportStatistics/{VO2_MAX,SPORT_LOAD}` + `startDay,endDay,limit,isReverse` | 3 | Four independent clients send these parameters. This project called with **no** parameters and got 500 (the names are real: a fabricated name gives 400). | passed through raw | `zepp_training_thresholds` (`vo2_max_history`, `training_load`) | ? | `test_training_thresholds_*` | Response shape never captured, so items are returned undecoded. |
| Recovery HR | v2 `sport/recovery_hr` (external) | 4 | Named by one external client | — | `zepp_raw_request` | — | — | Listed in `probe_candidates.py`. |
| Per-second HR | `fileInfo/events` `second_heart_rate` | 4 | The file **index** decodes | `events.second_hr_files` | `zepp_raw_request` | — | `test_second_hr_index_*` | The route that serves the files is unknown. |
| ECG, AFib | readiness `afibScore` | 5 | 255 (not computed) on every record | `events.readiness` | — | — | — | No ECG route found. |

## Blood oxygen and respiration

| Capability | Endpoint | Lvl | Evidence | Decoder | MCP tool | Units | Tests | Limitations |
|---|---|---|---|---|---|---|---|---|
| SpO₂ spot checks | v1 `blood_oxygen/click` (`extra`, JSON in JSON) | 2 | 99% reading, with a timestamp and the `isAuto` flag | `events.spo2_spot_checks` | `zepp_health_metrics(spo2)` | % | `test_spo2_*` | 0 and values below 50 are dropped as non-readings. |
| Overnight ODI | see Sleep | 2 | | | | | | |
| Respiratory rate | v2 `RespiratoryRate/real_data` | 2 | 1440 bytes per UTC day; alignment verified against sleep onset | `events.respiratory_rate` | `zepp_health_metrics(respiratory_rate)` | breaths/min | `test_respiratory_rate_*` | Recorded mostly during sleep. |

## Stress, recovery and readiness

| Capability | Endpoint | Lvl | Evidence | Decoder | MCP tool | Units | Tests | Limitations |
|---|---|---|---|---|---|---|---|---|
| All-day stress | v1 `all_day_stress` | 2 | min/max/avg and the four zone proportions recompute **exactly** from the item's own samples (zones <40/40–59/60–79/≥80) | `events.all_day_stress` | `zepp_health_metrics(stress)` | 0–100 | `test_stress_summary_recomputes_*` | v1 items carry no timezone, so the user's zone is used (`ZEPP_TIMEZONE`, else the system zone). |
| Single stress readings | v1 `single_stress` | 2 | — | `events.single_stress` | `zepp_health_metrics(stress)` | 0–100 | `test_single_stress` | — |
| Readiness and sub-scores | v2 `readiness/watch_score` | 2 | The same records carry `sleepRHR`/`sleepHRV`, which reproduce from other payloads | `events.readiness` | `zepp_health_metrics(readiness)`, `zepp_sleep` | 0–100 | `test_readiness_*` | 255 means not computed (phy, ment and afib were never computed in the corpus). `status` is passed through as a raw code. |
| Skin-temperature offset | readiness `skinTempCalibrated` | 4 (3 for unit) | One external client says hundredths of °C from baseline | `events.readiness` | (`skin_temperature_offset_raw_unverified`) | ? | — | Not converted until verified. |
| Body Charge | v2 `Charge/real_data` | 2 | `total` ≈ mean(mental, physical) within 1 | `events.body_charge` | `zepp_health_metrics(body_charge)` | 0–100 | `test_body_charge_*` | Days before pairing hold an app-generated curve with `total` 255; these are excluded. Components are `_unverified`. |
| Body Charge stress blob | `Charge/stress_data` `stressInfo` | 5 | Base64 binary; one external client says protobuf | — | `zepp_raw_request` | — | — | — |
| PAI | v1 `PaiHealthInfo` | 2 | `totalPai` is the running sum of `dailyPai` over the first 7 days; 75/day cap observed | `events.pai` | `zepp_health_metrics(pai)` | PAI | `test_pai_*` | Latest upload per day kept (level 3 rule). |
| ATL / CTL / TSB (exertion) | v2 `exertion/algo_result` (external) | 4 | One external client's docs | — | `zepp_raw_request` | — | — | In `probe_candidates.py`. |
| Training status / recovery time | `WatchSportStatistics/TRAINING_*` | 5 | 400, the same as a fabricated name | — | — | — | — | — |

## Body composition and profile

| Capability | Endpoint | Lvl | Evidence | Decoder | MCP tool | Units | Tests | Limitations |
|---|---|---|---|---|---|---|---|---|
| Weight, height, BMI | `users/{uid}/members/-1/weightRecords` | 2 | BMI cross-check, with `bmi_consistent` flag | `body.normalise` | `zepp_body_composition` | kg, cm | `test_body.py` | — |
| Fat, water, muscle, bone, BMR, visceral fat | ″ | 3 | AlexxIT/SmartScaleConnect capture | ″ | ″ | %, kg, kcal | ″ | No real scale on this project's account. |
| Profile (height, weight, age, wear hand) | `huami.health.getUserInfo.json` | 2 | Fixture 015 | `body.profile` | `zepp_profile` | cm, kg, years | `test_profile_drops_identifying_fields` | Gender is a raw code; name, avatar and location are never returned. |
| Paired devices | `users/{uid}/devices?enableMultiDevice=true` | 3 | Two external clients | `body.device_summary` (**allowlist**) | `zepp_profile` | — | `test_profile_never_returns_device_secrets_*` | Items carry the Bluetooth `auth_key`, which is never returned. |
| Manual BP, women's health, temperature | `users/me/bloodPressure`, `/v4/women…`, `skinTemp/real_data` | 4 | External clients only, with empty or no samples | — | `zepp_raw_request` | — | — | Deliberately not given tools until a real response shape is seen. |

## Workouts

| Capability | Endpoint | Lvl | Evidence | Decoder | MCP tool | Units | Tests | Limitations |
|---|---|---|---|---|---|---|---|---|
| Every sport in one index | `/v1/sport/run/history.json` | 1 | Live and fixture | `workouts.normalise` | `zepp_list_workouts` | — | existing | Other sport slugs return 404. |
| History pagination | `data.next` → `trackid=<next>` | 3 | Two external clients use this legacy form; the app form is `startTrackId/stopTrackId` | `client.workout_history_all` | `zepp_list_workouts`, `zepp_workout_stats` | — | `test_history_follows_next_cursor`, `test_history_stops_when_a_page_repeats` | Every capture so far fitted on one page. The walk stops and reports `partial` if a page repeats. |
| Moving / paused / total time | `exerciseTimeWithMillis`, `pauseTimeWithMillis`, `totalTimeWithMillis` | 2 | total == moving + paused on **8/8** workouts | `workouts._timing` | `zepp_list_workouts` | s | `test_total_time_is_moving_plus_paused` | — |
| Precise distance | `highPrecisionDistance` | 2 | Equals `dis` with decimals | `workouts._distance_metres` | ″ | m | `test_high_precision_distance` | — |
| Derived pace and speed | distance ÷ moving time | 2 | Within 1.5 s/km of Zepp's `avgEquivPace` | `workouts._derived_pace` | ″ | s/km, s/100 m, km/h | `test_derived_pace_*` | Labelled `derived`. |
| Strength sets and reps | `strength_training_group` | 2 | Matches detail lap rows **19/19** on reps and exercise code | `workouts._sets` | ″ | reps | `test_strength_sets_from_the_index_row` | Exercise codes are unnamed. `total_group` can exceed the recorded sets (20 vs 19). |
| Per-set duration, HR, load, rest | detail `lap` (strength) cols 1/4/5/21/22/28 | 2 | Col 22/28 match the index; col 5 ends at `run_time` | `decode.decode_strength_laps` | `zepp_workout_detail` | s, bpm, ? | `test_strength_laps_match_*` | The load column's unit (kg/lb) is unverified. Rest is derived. |
| Swim laps (duration, strokes, SWOLF) | detail `lap` cols 1/13/14 | 2 | SWOLF identity holds 34/36 | `decode.decode_laps` | ″ | s, strokes | existing | Other columns are inferred. |
| Km splits | detail `kilo_pace` | 2 | `split_seconds` matches the walk's own pace | `decode.decode_splits` | `zepp_workout_detail(splits)` | s | `test_splits_never_carry_the_geohash` | HR, stride and cadence columns are confirmed on a single split only. **Column 2 is a geohash (a location) and is never returned.** |
| Gait (cadence, stride per second) | detail `gait` cols 2/3 | 2 | Mean and max equal the row's stride and cadence | `decode.decode_gait` | `zepp_workout_detail(streams)` | cm, spm | `test_gait_matches_*` | Column 1 (external clients say Δsteps) does not sum to `total_step` here, so it is not returned. |
| Pauses | detail `pause` | 2 | Matches `pauseTimeWithMillis` to within 1.5 s | `decode.decode_pauses` | `zepp_workout_detail` | s | `test_pauses_reconcile_*` | Kind code raw (external: 2 manual, 3 auto). |
| DEM-corrected elevation | `add_info.adjust_dem_altitude` | 2 | Avg/max/min sit near the row's own altitude fields | `workouts._dem_elevation` | `zepp_list_workouts` | m | `test_dem_elevation_*` | Kept separate from barometric figures. |
| Running dynamics, power, TE, load, zones, PBs | history row | 2 | See [api-findings.md](api-findings.md) | `workouts` | `zepp_list_workouts` | ms, mm, W | existing | — |
| GPS track | detail `longitude_latitude` | 4 | No GPS capture is committed, for privacy | `decode.decode_stream` | `zepp_workout_detail(gps)` | — | — | Needs a synthetic track fixture. |
| Training plans (Zepp Coach) | history row `runningProgram`, `dailyScore` | 1 | Live, 2026-09-03 | `workouts._training_plan_block` | `zepp_list_workouts` | — | existing | No separate endpoint (22 routes probed). |
| Multisport | `parent_trackid` / `child_list` | 4 | Schema present, never populated | `workouts.normalise` | `zepp_list_workouts` | — | — | No triathlon has been recorded. |

### Workout analytics (all derived)

| Capability | Method | Tool | Tests |
|---|---|---|---|
| Volume by week, month, year or sport | Sums; HR weighted by moving time | `zepp_workout_stats` | `test_grouping_*` |
| Aggregate pace | **Total distance ÷ total moving time**, single-sport groups only | ″ | `test_aggregate_pace_is_not_the_mean_of_paces` |
| Records within a range | Longest, furthest, fastest (≥1 km / ≥100 m), best SWOLF, most reps or jumps, highest load | ″ | `test_records_and_flagged_bests` |
| Zepp's own PB flags | Best per `pb` key across the range | ″ | ″ |
| Per-workout progression and trend | 20 metrics; least-squares slope per 30 days | `zepp_workout_stats(metric=…)` | `test_series_and_trend` |
| 30-day overview | Independent sections; partial on failure | `zepp_period_summary` | `test_period_summary_*` |

No proprietary load, recovery or fitness model is computed. Zepp's numbers
are summed only where they are additive.

---

## Sport codes

| Code | Name | Confidence | Evidence | Sport block |
|---|---|---|---|---|
| 1 | outdoor_running | confirmed | App, GPS, 2.83 m/s | foot |
| 8 | walking | confirmed | App; GPS split track; 94 spm | foot |
| 9 | outdoor_cycling | confirmed | `ride_`-prefixed pb keys | ride |
| 14 | pool_swimming | confirmed | swolf, pool length | swim |
| 16 | free_training | confirmed (by elimination) | See api-findings | free_training |
| 18 | football | confirmed | App | field_sport |
| 21 | rope_skipping | confirmed | `rope_skipping_` pb keys | rope_skipping |
| 22 | hiking | confirmed | App; climb pb keys | hike |
| 52 | strength_training | confirmed | Sets and `strengthAssess` | strength |
| 6 | walking | corroborated | ≥2 external cloud clients | foot |
| 7 | trail_running | corroborated | ≥2 external cloud clients | foot |
| 10 | indoor_cycling | corroborated | ≥2 external cloud clients | ride |
| 23 | rowing_machine | corroborated | External cloud client + Huami device enum 0x17 | — |
| 60 | yoga | corroborated | Codes ≥40 equal the device hex (0x3C) | — |
| 92 | badminton | corroborated | External cloud client + device enum 0x5C | — |

**Conflict on record.** External tables call cloud code 8 "treadmill". On
this account, code 8 is an outdoor walk with a GPS split track, confirmed
in the app, so the confirmed name stands. Cloud `type` and the watch's
Bluetooth sport enum are separate namespaces for low codes (they coincide
from about 18 upward: 18 soccer, 21 jump rope and 52 strength all match the
device enum). Corroborated codes carry `sport_name_confidence:
"corroborated"`. Anything else is `unknown_sport_<code>`.

---

## MCP tools

| Tool | Purpose | Added |
|---|---|---|
| `zepp_period_summary` | One-call overview of activity, sleep, training and recovery | 2026-10-31 |
| `zepp_health_metrics` | readiness, hrv, resting_heart_rate, respiratory_rate, stress, spo2, pai, body_charge, daily_goals, each with summary and trend | 2026-10-31 |
| `zepp_workout_stats` | Volume, records, progression series | 2026-10-31 |
| `zepp_profile` | Body profile and paired devices (allowlisted) | 2026-10-31 |
| `zepp_daily_summary` | Any range (chunked); `include_hourly` | extended |
| `zepp_sleep` | One night or a range; joins readiness, RR, ODI | extended |
| `zepp_list_workouts` | Paginated history; `offset`; sport by code | extended |
| `zepp_workout_detail` | `splits`, strength sets, gait, pauses | extended |
| `zepp_training_thresholds` | Plus WatchSportStatistics pass-through | extended |
| `zepp_heart_rate`, `zepp_body_composition`, `zepp_describe_schema`, `zepp_raw_request`, `zepp_auth_status` | unchanged or extended schema | — |

Every new parameter is optional, so existing calls behave as before. The
only output-level changes are a renamed field (`run_distance_metres` →
`runDist_raw_unverified`) and strength-workout laps, which now come back as
`sets` instead of being forced through swim column names.

---

## External sources

Facts only were taken from every source below; no code was copied.

| Source | Licence | Used for |
|---|---|---|
| ZeppBridge (lingcang728) | MIT | Endpoint params, event types, pagination, cloud sport catalogue |
| m4ary/zepp-health-cli | MIT | WatchSportStatistics params, bloodPressure, manualData |
| AlexxIT/SmartScaleConnect | MIT | Scale schema |
| argrento/huami-token | MIT | Login flow |
| bentasker/zepp_to_influxdb | BSD-3 | band_data blob layout, heartRate params |
| rolandsz exporter, H3llK33p3r/zepp-fit-extractor | MIT / Apache-2.0 | `trackid=<next>` paging |
| micw/hacking-mifit-api, imastarboy97/zepp-mcp | none (all rights reserved) | Facts only |
| Gadgetbridge | AGPL | Device-side sport enum facts only |

## What would move rows up a level

1. **Run `uv run tools/probe_candidates.py`** on a real account. It covers
   WatchSportStatistics with parameters, devices, heartRate, bloodPressure,
   manualData and ten v2 event types, each against a control. A
   non-empty response moves a row from level 3 or 4 to level 1.
2. **A watch with ECG, skin temperature or a real body-composition scale.**
   Those rows are blocked on data this account cannot produce.
3. **A synthetic GPS track fixture**, to test the coordinate decoder
   without publishing a route.
4. **A multisport activity, open-water swim or treadmill run**, to confirm
   `child_list` assembly and the conflicting sport codes.
