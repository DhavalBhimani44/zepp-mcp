"""Tests for body-composition scale readings (issue #7).

`017_weight.json` is real project data: two manual/HealthKit-linked entries
whose reported `bmi` does not reconcile with `weight`/`height`. The "real
scale" case instead uses the numbers from a documented Mi Body Composition
Scale 2 capture published by github.com/AlexxIT/SmartScaleConnect (see the
module docstring in zepp_mcp/body.py) -- this project holds no capture of
its own from an account with a real scale, so that case is built inline
rather than added as a fixture file implying an anonymised real capture.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from zepp_mcp import body

FIXTURES = Path(__file__).parent / "fixtures" / "zepp"


def _weight_items() -> list[dict]:
    payload = json.loads((FIXTURES / "017_weight.json").read_text())
    return payload["body_parsed"]["items"]


def _iso(epoch_seconds: int) -> str:
    when = dt.datetime.fromtimestamp(epoch_seconds, dt.UTC)
    return when.isoformat(timespec="seconds")


def test_manual_entry_flags_bmi_inconsistency():
    # Record 1 from the real fixture: weightType 1, no scale-sourced fields.
    reading = body.normalise(_weight_items()[0])

    assert reading["weight_kg"] == 70.0
    assert reading["height_cm"] == 175.0
    assert reading["bmi"] == 27.8
    # 70 / 1.75**2 = 22.86, not 27.8 -- the record's own numbers disagree.
    assert reading["bmi_consistent"] is False
    assert reading["age_years"] == 21
    assert reading["date"] == _iso(1784546800)


def test_manual_entry_inconsistency_note_warns_about_the_whole_record():
    reading = body.normalise(_weight_items()[0])

    assert "do not reconcile" in reading["body_composition_note"]


def test_manual_entry_whole_number_fields_are_ints_not_floats():
    # bodyBalanceScore/oneFootMeasureTime arrive as JSON floats (93.0); a
    # count/score reads oddly as "93.0" to a model summarising it.
    reading = body.normalise(_weight_items()[0])

    composition = reading["body_composition"]
    assert composition["body_balance_score"] == 93
    assert isinstance(composition["body_balance_score"], int)


def test_manual_entry_exposes_present_optional_fields_only():
    # bodyBalanceScore and oneFootMeasureTime are present; fatRate,
    # muscleRate, boneMass etc. are not -- this record never touched a real
    # bio-impedance scale.
    reading = body.normalise(_weight_items()[0])

    composition = reading["body_composition"]
    assert composition["body_balance_score"] == 93
    assert composition["one_foot_measure_time"] == 93.0
    assert "body_fat_percent" not in composition
    assert "muscle_rate" not in composition


def test_manual_entry_zero_encrypt_impedance_is_not_a_reading():
    # encryptImpedance is "0" on both real records -- no scale, no reading.
    reading = body.normalise(_weight_items()[0])

    assert "impedance_ohms" not in reading["body_composition"]


def test_second_manual_entry_also_inconsistent():
    # Record 2: weightType 7, bmi 26.0 -- also does not reconcile.
    reading = body.normalise(_weight_items()[1])

    assert reading["bmi_consistent"] is False
    assert reading["body_composition"]["muscle_age"] == 21
    assert reading["body_composition"]["body_balance_score"] == 87


def test_real_scale_reading_exposes_full_body_composition():
    # Mi Body Composition Scale 2 capture documented by SmartScaleConnect.
    item = {
        "generatedTime": 1735689600,
        "summary": {
            "weight": 64.7, "height": 172.0, "bmi": 21.8,
            "fatRate": 17.01331, "bodyWaterRate": 56.92887,
            "boneMass": 2.7305484, "muscleRate": 50.961838,
            "muscleAge": 25, "proteinRatio": 21.837502,
            "standBodyWeight": 64.4, "visceralFat": 9.0,
            "metabolism": 1358.0, "bodyScore": 89, "bodyStyle": 5,
            "impedance": 451, "encryptImpedance": "451",
        },
    }

    reading = body.normalise(item)

    assert reading["weight_kg"] == 64.7
    assert reading["bmi_consistent"] is True
    assert "do not reconcile" not in reading["body_composition_note"]
    composition = reading["body_composition"]
    assert composition["body_fat_percent"] == 17.01331
    assert composition["body_water_percent"] == 56.92887
    assert composition["muscle_rate"] == 50.961838
    assert composition["bone_mass_kg"] == 2.7305484
    assert composition["basal_metabolism_kcal"] == 1358.0
    assert composition["body_score"] == 89
    assert composition["impedance_ohms"] == 451.0


def test_zero_valued_fields_are_treated_as_not_measured():
    item = {
        "generatedTime": 1735689600,
        "summary": {
            "weight": 64.7, "height": 172.0, "bmi": 21.87,
            "fatRate": 0, "boneMass": 0, "encryptImpedance": "0",
        },
    }

    reading = body.normalise(item)

    assert reading["bmi_consistent"] is True
    assert "body_composition" not in reading


def test_missing_summary_does_not_crash():
    assert body.normalise({"generatedTime": 1735689600}) == {
        "date": _iso(1735689600),
    }
