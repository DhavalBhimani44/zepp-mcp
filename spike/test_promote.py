import json

from promote import carries_gps
from redact import Redactor


def test_detects_gps_stream():
    assert carries_gps({"data": {"longitude_latitude": "1854240229,7379252091;151,39"}})


def test_empty_gps_field_is_not_a_gps_capture():
    """Pool swims have the key present but empty — they must still promote."""
    assert not carries_gps({"data": {"longitude_latitude": "", "lap": "0,43,21"}})


def test_non_dict_body_is_safe():
    assert not carries_gps(None)
    assert not carries_gps([1, 2, 3])
    assert not carries_gps({"data": "not-a-dict"})


def test_location_geohash_and_city_are_scrubbed():
    r = Redactor()
    out = r.scrub({"location": "tefqf26w8sjz", "city": "Pune", "swolf": 40})
    assert out == {"location": "<location>", "city": "<city>", "swolf": 40}


def test_scrubbing_location_leaves_sport_metrics_intact():
    r = Redactor()
    row = {"swolf": 40, "total_strokes": 481, "swim_pool_length": 21,
           "location": "tefqf26w8sjz"}
    out = r.scrub(row)
    assert out["swolf"] == 40 and out["total_strokes"] == 481
    assert out["swim_pool_length"] == 21 and out["location"] == "<location>"


def test_packed_geohash_is_scrubbed_from_kilo_pace():
    from promote import scrub_packed_geohashes
    row = {"kilo_pace": "0,347,tek3rpwnjm,-1,140,347,347539,61;0,599,tek3x0qfbp,-1,145"}
    out = scrub_packed_geohashes(row)
    assert "tek3rpwnjm" not in out["kilo_pace"]
    assert "tek3x0qfbp" not in out["kilo_pace"]
    assert out["kilo_pace"].startswith("0,347,<geohash>,-1,140,")


def test_packed_scrub_leaves_numeric_fields_alone():
    from promote import scrub_packed_geohashes
    row = {"kilo_pace": "0,347,tek3rpwnjm,-1,140,347,347539,61"}
    out = scrub_packed_geohashes(row)
    assert ",347,", out["kilo_pace"]
    assert out["kilo_pace"].endswith(",347,347539,61")


def test_packed_scrub_ignores_other_keys():
    from promote import scrub_packed_geohashes
    row = {"heart_rate": "22,117;1,-1;1,2", "lap": "0,43,21,,136,98"}
    assert scrub_packed_geohashes(row) == row


def test_harvest_finds_location_values_for_substring_registration():
    from promote import harvest_location_values
    rec = {"body_parsed": {"data": {"summary": [
        {"location": "tefqf26w8sjz", "city": "Pune", "swolf": 40}]}}}
    assert harvest_location_values(rec) == {"tefqf26w8sjz", "Pune"}
