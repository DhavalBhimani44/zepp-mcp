"""Every Zepp workout `type` code this project can put a name to, and how
sure it is about each one.

The cloud history row names its sport with a bare integer and nothing else,
so every name here comes from evidence outside the code itself. The table
ranks that evidence rather than flattening it -- a name the model restates
as fact should be one the data supports:

  confirmed     Verified on this project's own data: the account owner
                matched it against the Zepp app, or the row carries a
                sport-prefixed field (ride_*, rope_skipping_* pb keys).
  corroborated  At least two independent open-source clients that read the
                CLOUD history agree, and nothing here contradicts them.
  reported      Exactly one external cloud-side observation.
  catalogued    Not seen in cloud data by anyone. Named from the watch's own
                Zepp OS sport list, using the rule that cloud codes from 18
                upward equal the watch's sport id (18 soccer = 0x12, 21 jump
                rope = 0x15, 52 strength = 0x34 -- three of three on this
                account, and 23/60/92 agree in external cloud captures).
  contested     Sources disagree. Reported as unknown_sport_<code> with the
                candidate names listed, never as one of them.
  user          Supplied by the user through ZEPP_SPORT_CODES (below), e.g.
                after checking a workout in the Zepp app.

Codes below 18 do NOT follow the device-id rule (cloud 8 is a walk here,
device 0x08 is indoor cycling; cloud 9 is a ride, device 0x09 an
elliptical), so the catalogue rule is never applied to them.

Sources, facts only (no code copied): ZeppBridge's workout catalogue (MIT,
lingcang728/ZeppBridge), amazfit-swift (alan13367), zepp-life-mcp
(kubulashvili), the Mi Fit / Zepp workout exporter (MIT, rolandsz), dofek
(Asherlc), and Gadgetbridge's ZeppOsActivityType / HuamiSportsActivityType
enums (AGPL; ids only). See docs/capability-matrix.md "Sport codes".

Local overrides: set ZEPP_SPORT_CODES to a JSON object, or to the path of a
JSON file holding one, mapping code -> name or code -> {"name", "family"}:

    ZEPP_SPORT_CODES='{"17": "tennis", "301": {"name": "padel", "family": "racket_sport"}}'

Overrides win over every tier and report confidence "user".
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Sport families group sports for filtering ("all cycling"), for picking
# which metric block a workout row is read with, and for deciding where a
# pace is meaningful. A family is a classification of the NAME, so it is
# only as reliable as the name's confidence.
FAMILY_BLOCK: dict[str, str | None] = {
    "running": "foot",
    "walking": "foot",
    "cycling": "ride",
    "swimming": "swim",
    "hiking": "hike",
    "strength": "strength",
    "rope_skipping": "rope_skipping",
    "team_sport": "field_sport",
    "racket_sport": "field_sport",
    "free_training": "free_training",
    "cardio_machine": None,
    "mind_body": None,
    "dance": None,
    "martial_arts": None,
    "water_sport": None,
    "winter_sport": None,
    "wheel_sport": None,
    "other": None,
}

# Families where distance over time is the point, so pace is meaningful.
DISTANCE_FAMILIES = frozenset({"running", "walking", "cycling", "swimming", "hiking"})

TIERS: tuple[str, ...] = (
    "confirmed",
    "corroborated",
    "reported",
    "catalogued",
    "contested",
    "user",
)


@dataclass(frozen=True)
class Sport:
    code: int
    name: str
    family: str
    confidence: str
    sources: tuple[str, ...] = ()
    note: str = ""
    candidates: tuple[str, ...] = ()
    # Pin the metric block when the family default is wrong for this code.
    block: str | None = field(default=None)

    @property
    def metric_block(self) -> str | None:
        return self.block or FAMILY_BLOCK.get(self.family)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "name": self.display_name,
            "family": self.family,
            "confidence": self.confidence,
        }
        if self.sources:
            out["sources"] = list(self.sources)
        if self.candidates:
            out["candidates"] = list(self.candidates)
        if self.note:
            out["note"] = self.note
        return out

    @property
    def display_name(self) -> str:
        return (
            f"unknown_sport_{self.code}"
            if self.confidence == "contested"
            else self.name
        )


_ZB = "zeppbridge"
_SWIFT = "amazfit-swift"
_LIFE = "zepp-life-mcp"
_ROLAND = "mi-fit-zepp-exporter"
_DOFEK = "dofek"
_GB_OS = "gadgetbridge:ZeppOsActivityType"
_GB_HUAMI = "gadgetbridge:HuamiSportsActivityType"
_HERE = "this project"


def _c(code: int, name: str, family: str) -> Sport:
    """A catalogued code: Zepp OS sport id == cloud code (see module doc)."""
    return Sport(code, name, family, "catalogued", (_ZB, _GB_OS))


_TABLE: tuple[Sport, ...] = (
    # -- confirmed on this project's data ------------------------------
    Sport(
        1,
        "outdoor_running",
        "running",
        "confirmed",
        (_HERE, _ZB, _SWIFT, _LIFE, _ROLAND),
        "Confirmed in the Zepp app; GPS track at 2.83 m/s.",
    ),
    Sport(
        8,
        "walking",
        "walking",
        "confirmed",
        (_HERE,),
        "Confirmed in the Zepp app; an outdoor walk with a GPS split track "
        "and 94 steps/min. External tables call cloud 8 'treadmill' "
        "(zeppbridge, amazfit-swift, zepp-life-mcp, mi-fit-zepp-exporter); "
        "on this account it is not, so other accounts may differ.",
    ),
    Sport(
        9,
        "outdoor_cycling",
        "cycling",
        "confirmed",
        (_HERE, _ZB, _SWIFT, _LIFE, _ROLAND, _DOFEK),
        "The row's pb keys are ride_-prefixed; every external client agrees.",
    ),
    Sport(
        14,
        "pool_swimming",
        "swimming",
        "confirmed",
        (_HERE, _ZB, _SWIFT, _LIFE),
        "Carries swolf, pool length and stroke counts.",
    ),
    Sport(
        16,
        "free_training",
        "free_training",
        "confirmed",
        (_HERE, _ZB, _SWIFT, _LIFE),
        "Named by elimination on this account; three external clients agree.",
    ),
    Sport(
        18,
        "football",
        "team_sport",
        "confirmed",
        (_HERE, _GB_HUAMI),
        "Confirmed in the Zepp app; Huami's sport id 0x12 is soccer.",
    ),
    Sport(
        21,
        "rope_skipping",
        "rope_skipping",
        "confirmed",
        (_HERE, _SWIFT, _GB_OS),
        "The row's pb keys are rope_skipping_-prefixed; Zepp OS id 0x15.",
    ),
    Sport(
        22,
        "hiking",
        "hiking",
        "confirmed",
        (_HERE, _ZB),
        "Confirmed in the Zepp app; climb_-prefixed pb keys.",
    ),
    Sport(
        52,
        "strength_training",
        "strength",
        "confirmed",
        (_HERE, _ZB, _GB_OS, _GB_HUAMI),
        "Per-set reps; Zepp OS id 0x34.",
    ),
    # -- corroborated: two or more independent cloud-side sources ---------
    Sport(
        6, "walking", "walking", "corroborated", (_ZB, _SWIFT, _LIFE, _ROLAND, _DOFEK)
    ),
    Sport(7, "trail_running", "running", "corroborated", (_ZB, _SWIFT)),
    Sport(
        10,
        "indoor_cycling",
        "cycling",
        "corroborated",
        (_ZB, _SWIFT, _LIFE, _ROLAND, _DOFEK),
    ),
    Sport(12, "elliptical", "cardio_machine", "corroborated", (_ZB, _SWIFT, _LIFE)),
    Sport(
        23,
        "rowing_machine",
        "cardio_machine",
        "corroborated",
        (_ZB, _ROLAND, _DOFEK, _GB_HUAMI),
    ),
    Sport(60, "yoga", "mind_body", "corroborated", (_SWIFT, _ZB, _GB_OS, _GB_HUAMI)),
    Sport(
        92,
        "badminton",
        "racket_sport",
        "corroborated",
        (_ZB, _ROLAND, _DOFEK, _GB_OS, _GB_HUAMI),
    ),
    # -- reported: one external cloud-side observation ------------------
    Sport(11, "treadmill", "running", "reported", (_LIFE,)),
    Sport(
        15,
        "open_water_swimming",
        "swimming",
        "reported",
        (_SWIFT,),
        "zeppbridge reports this code only from one watch model.",
    ),
    Sport(46, "cross_country_skiing", "winter_sport", "reported", (_ZB,)),
    Sport(47, "mountain_biking", "cycling", "reported", (_ZB,)),
    Sport(141, "rowing", "water_sport", "reported", (_ZB,)),
    Sport(192, "outdoor_running", "running", "reported", (_ZB,)),
    Sport(193, "fishing", "other", "reported", (_ZB,)),
    Sport(211, "road_cycling", "cycling", "reported", (_ZB,)),
    Sport(
        223,
        "auto_detected_activity",
        "other",
        "reported",
        (_ZB,),
        "An activity the watch detected on its own rather than one started.",
    ),
    Sport(224, "hiking", "hiking", "reported", (_ZB,)),
    Sport(225, "rucking", "walking", "reported", (_ZB,)),
    # -- contested: sources disagree, so no name is asserted --------------
    Sport(
        13,
        "walking_or_rowing",
        "other",
        "contested",
        (_ZB, _LIFE),
        "zeppbridge infers walking; zepp-life-mcp says rowing.",
        candidates=("walking", "rowing"),
    ),
    Sport(
        17,
        "tennis_or_jump_rope",
        "other",
        "contested",
        (_ZB, _LIFE),
        "Zepp OS id 0x11 is tennis, but codes below 18 do not follow the "
        "device-id rule; zepp-life-mcp says jump rope.",
        candidates=("tennis", "jump_rope"),
    ),
    # -- device-protocol codes some clients carry for older watches ------
    Sport(
        2,
        "treadmill",
        "running",
        "catalogued",
        (_ZB, _GB_OS),
        "A watch-protocol id; cloud history is not known to use it.",
    ),
    Sport(
        3,
        "walking",
        "walking",
        "catalogued",
        (_ZB, _GB_OS),
        "A watch-protocol id; cloud history is not known to use it.",
    ),
    Sport(
        4,
        "outdoor_cycling",
        "cycling",
        "catalogued",
        (_ZB, _GB_OS),
        "A watch-protocol id; cloud history is not known to use it.",
    ),
    Sport(
        5,
        "free_training",
        "free_training",
        "catalogued",
        (_ZB, _GB_OS),
        "A watch-protocol id; cloud history is not known to use it.",
    ),
    # -- catalogued: Zepp OS sport id == cloud code (codes >= 18) ----------
    _c(24, "indoor_fitness", "strength"),
    _c(40, "indoor_walking", "walking"),
    _c(41, "curling", "winter_sport"),
    _c(44, "ice_skating", "winter_sport"),
    _c(45, "indoor_ice_skating", "winter_sport"),
    _c(48, "bmx", "cycling"),
    _c(49, "hiit", "strength"),
    _c(50, "core_training", "strength"),
    _c(51, "aerobic_combo", "dance"),
    _c(53, "stretching", "mind_body"),
    _c(54, "stair_climber", "cardio_machine"),
    _c(55, "flexibility", "mind_body"),
    _c(57, "stepper", "cardio_machine"),
    _c(59, "gymnastics", "strength"),
    _c(61, "pilates", "mind_body"),
    _c(64, "fishing", "other"),
    _c(65, "sailing", "water_sport"),
    _c(66, "water_rowing", "water_sport"),
    _c(67, "skateboarding", "wheel_sport"),
    _c(69, "roller_skating", "wheel_sport"),
    _c(70, "rock_climbing", "hiking"),
    _c(71, "ballet", "dance"),
    _c(72, "belly_dance", "dance"),
    _c(73, "square_dance", "dance"),
    _c(74, "street_dance", "dance"),
    _c(75, "ballroom_dance", "dance"),
    _c(76, "dance", "dance"),
    _c(77, "zumba", "dance"),
    _c(78, "cricket", "team_sport"),
    _c(79, "baseball", "team_sport"),
    _c(80, "bowling", "other"),
    _c(81, "squash", "racket_sport"),
    _c(85, "basketball", "team_sport"),
    _c(86, "softball", "team_sport"),
    _c(87, "gateball", "team_sport"),
    _c(88, "volleyball", "team_sport"),
    _c(89, "table_tennis", "racket_sport"),
    _c(91, "handball", "team_sport"),
    _c(93, "archery", "other"),
    _c(94, "horse_riding", "other"),
    _c(95, "kendo", "martial_arts"),
    _c(96, "karate", "martial_arts"),
    _c(97, "boxing", "martial_arts"),
    _c(98, "judo", "martial_arts"),
    _c(99, "wrestling", "martial_arts"),
    _c(100, "tai_chi", "mind_body"),
    _c(101, "muay_thai", "martial_arts"),
    _c(102, "taekwondo", "martial_arts"),
    _c(103, "martial_arts", "martial_arts"),
    _c(104, "kickboxing", "martial_arts"),
    _c(109, "aerobics", "dance"),
    _c(111, "mass_gymnastics", "dance"),
    _c(112, "latin_dance", "dance"),
    _c(113, "jazz_dance", "dance"),
    _c(114, "cardio_combat", "dance"),
    _c(115, "hula_hoop", "other"),
    _c(116, "frisbee", "team_sport"),
    _c(117, "darts", "other"),
    _c(118, "kite_flying", "other"),
    _c(119, "tug_of_war", "team_sport"),
    _c(122, "beach_volleyball", "team_sport"),
    _c(129, "parkour", "other"),
    _c(130, "cross_training", "strength"),
    _c(131, "race_walking", "walking"),
    _c(132, "driving", "other"),
    _c(138, "dragon_boat", "water_sport"),
    _c(140, "kayaking", "water_sport"),
    _c(143, "spinning", "cycling"),
    _c(144, "air_walker", "cardio_machine"),
    _c(145, "wall_ball", "strength"),
    _c(146, "folk_dance", "dance"),
    _c(147, "jujitsu", "martial_arts"),
    _c(148, "fencing", "martial_arts"),
    _c(149, "horizontal_bar", "strength"),
    _c(150, "parallel_bars", "strength"),
    _c(151, "billiards", "other"),
    _c(152, "sepak_takraw", "team_sport"),
    _c(153, "dodgeball", "team_sport"),
    _c(154, "water_polo", "water_sport"),
    _c(155, "finswimming", "water_sport"),
    _c(156, "artistic_swimming", "water_sport"),
    _c(157, "snorkeling", "water_sport"),
    _c(158, "ice_hockey", "team_sport"),
    _c(159, "swinging", "other"),
    _c(160, "shuffleboard", "other"),
    _c(161, "table_football", "other"),
    _c(162, "shuttlecock", "other"),
    _c(163, "somatosensory_game", "other"),
    _c(164, "futsal", "team_sport"),
    _c(165, "hip_hop", "dance"),
    _c(166, "pole_dance", "dance"),
    _c(167, "battle_rope", "strength"),
    _c(168, "breaking", "dance"),
    _c(169, "hacky_sack", "other"),
    _c(170, "bocce", "other"),
    _c(171, "jai_alai", "other"),
    _c(172, "flowriding", "water_sport"),
    _c(173, "chess", "other"),
    _c(174, "checkers", "other"),
    _c(175, "weiqi", "other"),
    _c(176, "bridge", "other"),
    _c(177, "board_game", "other"),
    _c(184, "bouldering", "hiking"),
    _c(185, "modern_dance", "dance"),
    _c(188, "floorball", "team_sport"),
    _c(189, "esports", "other"),
    _c(191, "soccer", "team_sport"),
    _c(206, "e_bike", "cycling"),
)

_BY_CODE: dict[int, Sport] = {s.code: s for s in _TABLE}
assert len(_BY_CODE) == len(_TABLE), "duplicate sport code"

# Families for names that can arrive through an override without one --
# including the contested codes' candidates, which is exactly what a user
# confirming code 13 or 17 will type.
_FAMILY_BY_NAME: dict[str, str] = {
    "tennis": "racket_sport",
    "jump_rope": "rope_skipping",
    "rowing": "water_sport",
}
for _sport in _TABLE:
    if _sport.confidence != "contested":
        _FAMILY_BY_NAME.setdefault(_sport.name, _sport.family)

_override_cache: tuple[tuple[Any, ...], dict[int, Sport]] | None = None


def _overrides() -> dict[int, Sport]:
    """User-supplied codes from ZEPP_SPORT_CODES. A malformed value is
    ignored rather than fatal: a typo must not take the server down."""
    global _override_cache
    raw = os.environ.get("ZEPP_SPORT_CODES", "").strip()
    if not raw:
        return {}
    path = Path(raw).expanduser()
    try:
        key: tuple[Any, ...] = (raw, path.stat().st_mtime) if path.is_file() else (raw,)
    except OSError:
        key = (raw,)
    if _override_cache and _override_cache[0] == key:
        return _override_cache[1]
    try:
        text = path.read_text() if path.is_file() else raw
        blob = json.loads(text)
    except (OSError, ValueError):
        blob = None
    parsed: dict[int, Sport] = {}
    if isinstance(blob, dict):
        for code_text, value in blob.items():
            try:
                code = int(code_text)
            except (TypeError, ValueError):
                continue
            if isinstance(value, str):
                name, family = value, None
            elif isinstance(value, dict) and isinstance(value.get("name"), str):
                name, family = value["name"], value.get("family")
            else:
                continue
            name = name.strip().lower().replace(" ", "_")
            if not name:
                continue
            if family not in FAMILY_BLOCK:
                family = _FAMILY_BY_NAME.get(name, "other")
            parsed[code] = Sport(code, name, family, "user", ("ZEPP_SPORT_CODES",))
    _override_cache = (key, parsed)
    return parsed


def lookup(code: object) -> Sport | None:
    try:
        number = int(code)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return _overrides().get(number) or _BY_CODE.get(number)


def name(code: object) -> str:
    sport = lookup(code)
    return sport.display_name if sport else f"unknown_sport_{code}"


def confidence(code: object) -> str:
    sport = lookup(code)
    return sport.confidence if sport else "unknown"


def family(code: object) -> str | None:
    sport = lookup(code)
    return None if sport is None or sport.confidence == "contested" else sport.family


def metric_block(code: object) -> str | None:
    sport = lookup(code)
    if sport is None or sport.confidence == "contested":
        return None
    return sport.metric_block


def matches(workout: dict[str, Any], query: str) -> bool:
    """A sport filter: numeric code, name fragment, or family name."""
    wanted = query.strip().lower().replace(" ", "_")
    if not wanted:
        return True
    if wanted.isdigit():
        return str(workout.get("sport_code")) == wanted
    return (
        wanted in str(workout.get("sport", "")).lower()
        or wanted == str(workout.get("sport_family") or "").lower()
    )


def describe() -> dict[str, Any]:
    table = {**_BY_CODE, **_overrides()}
    rows = [table[c].as_dict() for c in sorted(table)]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["confidence"]] = counts.get(row["confidence"], 0) + 1
    return {
        "codes": rows,
        "count_by_confidence": counts,
        "families": sorted(FAMILY_BLOCK),
        "confidence_meaning": {
            "confirmed": "verified on this project's own data",
            "corroborated": "two or more independent cloud-reading clients agree",
            "reported": "one external cloud-side observation",
            "catalogued": "named from the Zepp OS sport list via the "
            "cloud-code == device-id rule (codes >= 18); not "
            "yet seen in cloud data",
            "contested": "sources disagree; reported as unknown_sport_<code> "
            "with candidates",
            "user": "supplied via ZEPP_SPORT_CODES",
        },
        "note": "Codes absent from this table report as unknown_sport_<code>: "
        "the activity is real, only its name is unknown. Set "
        "ZEPP_SPORT_CODES to name codes you have checked in the "
        "Zepp app.",
    }
