"""Normalises smart-scale body-composition readings (issue #7).

The account behind this project's own captures never owned a real
bio-impedance scale: `tests/fixtures/zepp/017_weight.json` (captured in the
original recon spike) carries two records with `weightType` 1 and 7, both
from a manual/HealthKit-linked entry (`source: 2`, `thirdAppName: "Health"`),
neither of which has fatRate/muscleRate/boneMass etc. -- and both have a
`bmi` that does NOT reconcile with `weight`/`height` (record 1: reported
27.8 vs. 70 / 1.75**2 = 22.9). That inconsistency is not a decoding bug --
it is the record itself; `bmi_consistent` below flags it per record rather
than silently trusting a number the API's own arithmetic contradicts.

The fuller schema (`fatRate`, `bodyWaterRate`, `muscleRate`, `boneMass`,
`metabolism`, `proteinRatio`, `visceralFat`, `bodyScore`, `impedance`) comes
from a real Mi Body Composition Scale 2 capture documented by a sibling
open-source Zepp API client, github.com/AlexxIT/SmartScaleConnect -- not
from an account this project holds. See docs/api-findings.md. `weight`,
`height` and `bmi` are verified by the same cross-check used there (bmi
reproduces weight / (height/100)**2); everything else is exposed under
`body_composition` with its provenance noted rather than asserted as fact.
`muscleRate` keeps its ambiguous name un-renamed to a percent-style key --
the source client itself does not know whether it is a percentage or an
absolute mass ("don't know why name is rate?!").
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

# summary key -> output key, for fields whose presence (not just decoding)
# is optional and scale/source-dependent. All are measurements where 0 is
# not a valid reading (nobody has 0% body fat or 0 kg of bone), so a zero or
# missing value is treated as "not measured" -- the same convention this
# project already uses for a zero step count.
_EXTRA_FIELDS: dict[str, str] = {
    "fatRate": "body_fat_percent",
    "bodyWaterRate": "body_water_percent",
    "boneMass": "bone_mass_kg",
    "metabolism": "basal_metabolism_kcal",
    "muscleRate": "muscle_rate",
    "muscleAge": "muscle_age",
    "proteinRatio": "protein_percent",
    "standBodyWeight": "ideal_weight_kg",
    "visceralFat": "visceral_fat_rating",
    "bodyScore": "body_score",
    "bodyStyle": "body_style_code",
    "bodyBalanceScore": "body_balance_score",
    "oneFootMeasureTime": "one_foot_measure_time",
}

_BODY_COMPOSITION_NOTE = (
    "Field names come from a community-documented real-scale capture "
    "(AlexxIT/SmartScaleConnect), not from this project's own verified "
    "data -- see zepp_describe_schema known_gaps. weight_kg/height_cm/bmi "
    "above are independently verified; these are not."
)

_INCONSISTENT_NOTE = (
    " This record's own weight/height/bmi do not reconcile with each "
    "other, which is a signal the rest of this record's numbers may not "
    "be trustworthy either."
)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _epoch(value: object) -> int | None:
    seconds = _number(value)
    return int(seconds) if seconds else None


def _impedance_ohms(summary: dict[str, Any]) -> float | None:
    """`impedance` (int) and `encryptImpedance` (string) carry the same
    value where both are present (verified against the SmartScaleConnect
    capture: 451 == "451"). Our own fixture only ever has the "encrypted"
    string, so it is read as the fallback source rather than dropped as a
    duplicate -- "encrypt" is a misnomer, not a cipher we would need a key
    for."""
    direct = _number(summary.get("impedance"))
    return direct or _number(summary.get("encryptImpedance"))


def normalise(item: dict[str, Any]) -> dict[str, Any]:
    """One `weightRecords` item -> a flat, snake_case reading."""
    summary = item.get("summary")
    summary = summary if isinstance(summary, dict) else {}

    generated = _epoch(item.get("generatedTime"))
    out: dict[str, Any] = {
        "date": (dt.datetime.fromtimestamp(generated, dt.UTC)
                 .isoformat(timespec="seconds") if generated else None),
    }

    weight = _number(summary.get("weight"))
    height = _number(summary.get("height"))
    bmi = _number(summary.get("bmi"))
    if weight:
        out["weight_kg"] = weight
    if height:
        out["height_cm"] = height
    if bmi:
        out["bmi"] = bmi
        if weight and height:
            expected = weight / (height / 100) ** 2
            out["bmi_consistent"] = abs(expected - bmi) < 0.15

    extra: dict[str, Any] = {}
    for src, key in _EXTRA_FIELDS.items():
        value = _number(summary.get(src))
        if value:
            extra[key] = int(value) if value.is_integer() else value
    impedance = _impedance_ohms(summary)
    if impedance:
        extra["impedance_ohms"] = (
            int(impedance) if impedance.is_integer() else impedance)

    if extra:
        note = _BODY_COMPOSITION_NOTE
        if out.get("bmi_consistent") is False:
            note += _INCONSISTENT_NOTE
        out["body_composition"] = extra
        out["body_composition_note"] = note

    age = _number(summary.get("age"))
    if age:
        out["age_years"] = int(age)

    return out


# Profile fields that are returned. Everything else in getUserInfo -- nick
# name, avatar URL, signature, location, alarm clocks, login timestamps -- is
# identifying or irrelevant to interpreting health data, and is dropped
# rather than filtered later.
def profile(data: dict[str, Any], today: dt.date | None = None) -> dict[str, Any]:
    """`huami.health.getUserInfo.json` data -> the inputs Zepp's own
    calculations use (BMR, calories, zone limits)."""
    out: dict[str, Any] = {}
    height = _number(data.get("height"))
    if height:
        out["height_cm"] = int(height) if height.is_integer() else height
    weight = _number(data.get("weight_float")) or _number(data.get("weight"))
    if weight:
        out["weight_kg"] = weight
    birthday = data.get("birthday")
    if isinstance(birthday, str) and len(birthday) >= 7:
        try:
            year, month = int(birthday[:4]), int(birthday[5:7])
        except ValueError:
            year = month = 0
        if year and 1 <= month <= 12:
            today = today or dt.date.today()
            # Birth month only: age is exact except within the birth month.
            out["age_years"] = today.year - year - (1 if today.month < month else 0)
    gender = _number(data.get("gender"))
    if gender is not None:
        # The code's meaning (0/1 -> which gender) is not verified against
        # the app, so the code is passed through rather than named.
        out["gender_code_unverified"] = int(gender)
    config = data.get("config")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            config = None
    if isinstance(config, dict) and isinstance(config.get("wearHand"), str):
        out["wear_hand"] = config["wearHand"].lower()
    out["note"] = "Identifying profile fields are not returned, by design."
    return out


# Device fields that may be returned. An ALLOWLIST, not a blocklist: device
# records carry the Bluetooth pairing secret (`additionalInfo.auth_key`),
# MAC address and serial number, and a blocklist would leak whatever field
# Zepp adds next. Only the names of omitted fields are reported, never
# their values.
_DEVICE_FIELDS = frozenset({
    "deviceSource", "deviceType", "productId", "productVersion", "model",
    "brand", "firmwareVersion", "hardwareVersion", "softwareVersion",
    "activeStatus", "bindingStatus", "applicationTime", "lastSyncTime",
    "lastBindingTime", "bindingTime",
})


def device_summary(payload: Any) -> dict[str, Any]:
    items = payload.get("items") if isinstance(payload, dict) else payload
    if isinstance(payload, dict) and not isinstance(items, list):
        items = payload.get("data")
    if not isinstance(items, list):
        return {"devices": [], "note": "Unrecognised device payload shape."}
    devices, omitted = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        kept = {k: v for k, v in item.items()
                if k in _DEVICE_FIELDS and isinstance(v, (str, int, float, bool))}
        omitted |= {k for k in item if k not in _DEVICE_FIELDS}
        devices.append(kept)
    return {
        "devices": devices,
        "omitted_field_names": sorted(omitted),
        "note": "Allowlisted fields only. Pairing keys, MAC addresses, "
                "serial numbers and device ids are never returned. "
                "deviceSource is the id that workout and event records "
                "reference, for data-source attribution.",
    }
