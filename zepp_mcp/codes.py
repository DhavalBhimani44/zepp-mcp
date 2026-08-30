"""Lookup tables for Zepp's numeric codes and sentinel values.

Everything here is either verified against the fixture corpus or explicitly
marked unverified. Nothing is guessed silently: a code we cannot prove is
reported as unknown rather than given a plausible-sounding name, because the
consumer is an LLM that will restate whatever we label as fact.
"""

from __future__ import annotations

# Sleep stage modes. VERIFIED against tests/fixtures/zepp/014: summing stage
# minutes per mode reproduces the summary's own lt/dp/wk/dt totals exactly on
# three separate nights.
#
# Note `dt` is REM. Implementations that report `dp + lt` as "total sleep" drop
# REM entirely -- 73, 53 and 96 minutes on the three verified nights.
SLEEP_STAGE_MODES: dict[int, str] = {
    4: "light",
    5: "deep",
    7: "awake",
    8: "rem",
}

SLEEP_SUMMARY_FIELDS: dict[str, str] = {
    "lt": "light_minutes",
    "dp": "deep_minutes",
    "dt": "rem_minutes",
    "wk": "awake_minutes",
    "wc": "wake_count",
    "ss": "sleep_score",
    "st": "sleep_start_ts",
    "ed": "sleep_end_ts",
    "rhr": "resting_heart_rate",
    "is": "fall_asleep_minutes",
}

# Sport type codes, all confirmed by the account owner against the Zepp app
# on 2026-08-16. The payload itself carries no human-readable sport name, so
# this map cannot be derived from the data alone.
#
#  1: outdoor running     3363 m in 1190 s = 2.83 m/s, GPS track
#  8: walking             1064 m in 909 s = 1.17 m/s at 94 steps/min
#  9: outdoor cycling     2352 m in 484 s = 17.5 km/h. Confirmed by the
#                         payload itself, not inferred: the row's `pb`
#                         object is keyed ride_longest_time,
#                         ride_most_up_m and ride_furthest_km.
# 14: pool swimming       carries swolf / swim_pool_length / stroke counts
# 22: hiking              2298-2624 m over 76-100 min, high calorie burn
#                         with a step count -- slow ground speed, hard effort
# 52: strength training   zero distance, total_group = set count, plus a
#                         strengthAssess JSON stream
#
# Codes outside this map are reported as unknown_sport_<code> rather than
# guessed. A fabricated sport name in front of the model becomes a fact.
SPORT_CODES: dict[int, str] = {
    1: "outdoor_running",
    8: "walking",
    9: "outdoor_cycling",
    14: "pool_swimming",
    22: "hiking",
    52: "strength_training",
}


def sport_name(code: object) -> str:
    try:
        return SPORT_CODES.get(int(code), f"unknown_sport_{code}")
    except (TypeError, ValueError):
        return f"unknown_sport_{code}"


# Sentinels. Zepp does not use a single not-applicable marker; it uses a
# different one per field family, and several are plausible readings in their
# own units. -20000 rendered as an altitude in metres looks like data.
SENTINELS: dict[str, tuple[float, ...]] = {
    "altitude": (-20000.0, -1.0),
    "angle": (-361.0,),
    "elevation": (-100.0, -1.0),
    # -274 C is below absolute zero: it is the "no thermometer reading"
    # marker. Only the pool swims carry a real temperature, because the
    # watch is reading water.
    "temperature": (-274.0, -273.0, -1.0),
    # SpO2 uses TWO markers -- -1 on most activities, 0 on the hikes. A
    # blood oxygen saturation of 0% is not a reading.
    "percentage": (0.0, -1.0),
    # Cadence / stroke rate. Zepp reports it in `avg_frequency`, verified
    # against steps-per-minute on a run (153.0 vs 153.3 implied) and a walk
    # (93.0 vs 94.0). The dedicated `avg_cadence` field is dead -- always 0.
    #
    # -60 is a real marker seen on hiking activities. It is not a low
    # cadence; a negative frequency is not a measurement.
    "frequency": (0.0, -1.0, -60.0),
    # A pace of 0 s/m is infinite speed. `min_pace` reports 0 when the watch
    # did not establish a fastest split.
    "pace": (0.0, -1.0),
    # Gait ratios read 0 when unmeasured. A run necessarily has a flight
    # phase, so a flight ratio of exactly 0 on a run is absence, not data.
    "ratio": (0.0, -1.0),
    "default": (-1.0,),
}

# Per-minute heart rate byte stream (`data_hr`). 254 (0xFE) and 255 dominate
# unworn stretches; 0 means no reading, not a stopped heart.
HR_BYTE_SENTINELS = frozenset({0, 254, 255})


def is_sentinel(value: float, family: str = "default") -> bool:
    return value in SENTINELS.get(family, SENTINELS["default"])


def clean(value: object, family: str = "default") -> float | None:
    """Return the number, or None when it is that field's not-applicable marker."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if is_sentinel(number, family) else number


# Training Effect is reported at ten times its displayed value: a workout the
# Zepp app shows as 3.2 arrives as `te: 32`, and 0.1 as `anaerobic_te: 1`.
# Confirmed by the account holder against the app for a run reporting te 32
# and anaerobic_te 1, displayed as aerobic 3.2 and anaerobic 0.1.
#
# The scale itself is the standard Firstbeat one used across Garmin, Polar
# and Zepp. The bands are an interpretation of the number, not a measurement,
# so they are emitted under their own key rather than replacing the value.
TRAINING_EFFECT_SCALE = 0.1

_TE_BANDS: tuple[tuple[float, str], ...] = (
    (1.0, "no effect"),
    (2.0, "minor"),
    (3.0, "maintaining"),
    (4.0, "improving"),
    (5.0, "highly improving"),
)


def training_effect_band(value: float) -> str:
    """Qualitative band for a Training Effect score on the 0.0-5.0 scale."""
    for upper, label in _TE_BANDS:
        if value < upper:
            return label
    return "overreaching"
