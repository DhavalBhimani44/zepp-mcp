"""The sport-code registry: every tier, the rules that keep a name honest,
overrides, and how a code drives decoding, filtering and analytics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zepp_mcp import analysis, sports, workouts
from zepp_mcp.client import parse_rows
from zepp_mcp.codes import SPORT_CODES, sport_name

FIXTURES = Path(__file__).parent / "fixtures" / "zepp"


@pytest.fixture(autouse=True)
def no_overrides(monkeypatch):
    monkeypatch.delenv("ZEPP_SPORT_CODES", raising=False)
    sports._override_cache = None


@pytest.fixture(scope="module")
def rows():
    return parse_rows(
        json.loads((FIXTURES / "040_workout_history_run.json").read_text())[
            "body_parsed"
        ]
    )


def as_code(rows, source_type: int, code: int) -> dict:
    row = dict(next(r for r in rows if r["type"] == source_type))
    row["type"] = code
    return workouts.normalise(row)


# -- the table itself ----------------------------------------------------------

# What the nine confirmed codes decoded with before the registry existed.
# Moving them into sports.py must not change a single one.
_PREVIOUS_BLOCKS = {
    1: "foot",
    8: "foot",
    9: "ride",
    14: "swim",
    16: "free_training",
    18: "field_sport",
    21: "rope_skipping",
    22: "hike",
    52: "strength",
}


def test_confirmed_codes_are_unchanged():
    for code, name in SPORT_CODES.items():
        assert sports.name(code) == name
        assert sports.confidence(code) == "confirmed"
        assert sports.metric_block(code) == _PREVIOUS_BLOCKS[code]


def test_table_covers_the_zepp_sport_list():
    described = sports.describe()
    assert len(described["codes"]) >= 140
    assert described["count_by_confidence"]["confirmed"] == 9
    for name in (
        "basketball",
        "tennis",
        "yoga",
        "boxing",
        "kayaking",
        "ice_skating",
        "table_tennis",
        "pilates",
        "hiit",
        "soccer",
    ):
        assert any(
            r["name"] == name or name in r.get("candidates", [])
            for r in described["codes"]
        ), name


def test_device_id_rule_is_never_applied_below_18():
    """Low cloud codes disagree with the watch's ids (cloud 8 is a walk,
    device 0x08 indoor cycling), so a catalogued name below 18 is only
    allowed for the explicitly flagged device-protocol codes."""
    for row in sports.describe()["codes"]:
        if row["confidence"] == "catalogued" and row["code"] < 18:
            assert row["code"] in (2, 3, 4, 5)
            assert "watch-protocol" in row["note"]


def test_rule_holds_on_every_confirmed_code_it_covers():
    """18 = Huami 0x12 soccer, 21 = Zepp OS 0x15 jump rope, 52 = 0x34
    strength: the evidence the catalogue tier rests on."""
    assert (0x12, 0x15, 0x34) == (18, 21, 52)
    assert sports.family(18) == "team_sport"
    assert sports.family(21) == "rope_skipping"
    assert sports.family(52) == "strength"


def test_every_family_has_a_block_entry_and_codes_are_unique():
    codes = [r["code"] for r in sports.describe()["codes"]]
    assert len(codes) == len(set(codes))
    for row in sports.describe()["codes"]:
        assert row["family"] in sports.FAMILY_BLOCK


def test_contested_codes_never_assert_a_name():
    for code in (13, 17):
        assert sports.name(code) == f"unknown_sport_{code}"
        assert sports.confidence(code) == "contested"
        assert sports.family(code) is None
        assert sports.metric_block(code) is None
        assert len(sports.lookup(code).candidates) == 2


def test_absent_codes_stay_unknown():
    assert sports.name(999) == "unknown_sport_999"
    assert sports.confidence(999) == "unknown"
    assert sport_name("junk") == "unknown_sport_junk"


# -- overrides -----------------------------------------------------------------


def test_inline_override_names_a_code_and_infers_family(monkeypatch):
    monkeypatch.setenv("ZEPP_SPORT_CODES", '{"17": "tennis", "301": "padel"}')
    assert sports.name(17) == "tennis"
    assert sports.confidence(17) == "user"
    assert sports.family(17) == "racket_sport"  # inferred from the catalogue
    assert sports.family(301) == "other"


def test_file_override_with_family(monkeypatch, tmp_path):
    path = tmp_path / "sports.json"
    path.write_text(json.dumps({"8": {"name": "treadmill", "family": "running"}}))
    monkeypatch.setenv("ZEPP_SPORT_CODES", str(path))
    assert sports.name(8) == "treadmill"
    assert sports.confidence(8) == "user"
    assert sports.metric_block(8) == "foot"


def test_malformed_override_is_ignored(monkeypatch):
    monkeypatch.setenv("ZEPP_SPORT_CODES", "{not json")
    assert sports.name(17) == "unknown_sport_17"
    monkeypatch.setenv("ZEPP_SPORT_CODES", '{"x": "a", "5": 3, "6": ""}')
    assert sports.name(6) == "walking"


# -- decoding --------------------------------------------------------------------


def test_catalogued_team_sport_reads_the_field_sport_block(rows):
    item = as_code(rows, 1, 85)  # a run row relabelled basketball
    assert item["sport"] == "basketball"
    assert item["sport_name_confidence"] == "catalogued"
    assert item["sport_family"] == "team_sport"
    assert "unclassified_metrics" not in item


def test_non_confirmed_codes_keep_other_blocks_data(rows):
    """A family guess picks the block; whatever else the row populated is
    kept under other_metrics instead of being hidden by the guess."""
    item = as_code(rows, 22, 60)  # a hike row relabelled yoga
    assert item["sport"] == "yoga"
    assert item["other_metrics"]["hike"]["elevation_gain_metres"] > 0


def test_confirmed_codes_get_no_other_metrics(rows):
    for row in rows:
        assert "other_metrics" not in workouts.normalise(row)


def test_contested_code_exposes_candidates_and_every_block(rows):
    item = as_code(rows, 22, 17)
    assert item["sport"] == "unknown_sport_17"
    assert item["sport_candidates"] == ["tennis", "jump_rope"]
    assert item["unclassified_metrics"]


def test_corroborated_running_variant_gets_running_metrics(rows):
    item = as_code(rows, 1, 7)
    assert item["sport"] == "trail_running"
    assert item["foot"]["running_dynamics"]
    assert "pace_sec_per_km" in item["derived"]


def test_open_water_swim_gets_swim_pace(rows):
    item = as_code(rows, 14, 15)
    assert item["sport_family"] == "swimming"
    assert "pace_sec_per_100m" in item["derived"]


def test_stale_strength_groups_never_become_sets_on_other_sports(rows):
    item = as_code(rows, 14, 49)  # a swim row relabelled HIIT
    assert item["sport_family"] == "strength"
    assert "sets" not in item.get("strength", {})


# -- filtering and analytics -----------------------------------------------------


def test_matches_by_code_name_and_family():
    ride = {"sport": "indoor_cycling", "sport_code": 10, "sport_family": "cycling"}
    assert sports.matches(ride, "10")
    assert sports.matches(ride, "indoor")
    assert sports.matches(ride, "cycling")
    assert sports.matches(ride, "Indoor Cycling")
    assert not sports.matches(ride, "running")


def test_family_grouping_and_shared_pace(rows):
    outdoor = workouts.normalise(next(r for r in rows if r["type"] == 1))
    trail = as_code(rows, 1, 7)
    grouped = {
        g["group"]: g for g in analysis.grouped([outdoor, trail], "sport_family")
    }
    assert grouped["running"]["workouts"] == 2
    # Two running-family sports still get an aggregate pace...
    assert "avg_pace_sec_per_km" in analysis.totals([outdoor, trail])
    # ...a run and a swim do not.
    swim = workouts.normalise(next(r for r in rows if r["type"] == 14))
    assert "avg_pace" not in analysis.totals([outdoor, swim])


def test_records_cover_catalogued_distance_sports(rows):
    trail = as_code(rows, 1, 7)
    records = analysis.records([trail])["within_range"]
    assert "fastest_avg_pace_sec_per_km" in records["trail_running"]
