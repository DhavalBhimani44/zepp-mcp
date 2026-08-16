import json

from redact import Redactor


def test_registered_value_redacted_in_plain_field():
    r = Redactor()
    r.register("abcdef0123456789", "app_token")
    assert r.scrub({"tok": "abcdef0123456789"}) == {"tok": "<app_token>"}


def test_registered_value_redacted_inside_url():
    r = Redactor()
    r.register("abcdef0123456789", "app_token")
    out = r.scrub({"url": "https://x.zepp.com/a?apptoken=abcdef0123456789&b=1"})
    assert out == {"url": "https://x.zepp.com/a?apptoken=<app_token>&b=1"}


def test_registered_value_redacted_inside_double_encoded_json():
    """Zepp nests JSON-encoded strings inside JSON (spec section 5)."""
    r = Redactor()
    r.register("abcdef0123456789", "app_token")
    inner = json.dumps({"session": "abcdef0123456789"})
    out = r.scrub({"data": inner})
    assert "abcdef0123456789" not in out["data"]
    assert "<app_token>" in out["data"]


def test_sensitive_key_redacted_even_when_value_short():
    r = Redactor()
    assert r.scrub({"user_id": "1234567"}) == {"user_id": "<user_id>"}


def test_short_registered_value_is_not_substring_replaced():
    """Guards against corrupting unrelated numeric data."""
    r = Redactor()
    r.register("1234567", "user_id")
    assert r.scrub({"steps": "1234567 steps"}) == {"steps": "1234567 steps"}


def test_scrubs_through_nested_lists_and_dicts():
    r = Redactor()
    r.register("abcdef0123456789", "app_token")
    out = r.scrub({"items": [{"a": ["abcdef0123456789"]}]})
    assert out == {"items": [{"a": ["<app_token>"]}]}


def test_longer_secret_replaced_before_shorter_overlapping_one():
    r = Redactor()
    r.register("abcdef0123", "short_tok")
    r.register("abcdef0123456789", "long_tok")
    assert r.scrub({"t": "abcdef0123456789"}) == {"t": "<long_tok>"}


def test_sensitive_key_redacted_when_value_is_an_integer():
    """Zepp returns user_id as a JSON integer, not a string. An int that
    skips key-name redaction reaches disk verbatim, because scrub() passes
    non-str/dict/list values straight through."""
    r = Redactor()
    assert r.scrub({"user_id": 12345678}) == {"user_id": "<user_id>"}


def test_zepp_status_envelope_survives_redaction():
    """{"code": 1, "data": [...]} is Zepp's standard envelope. Treating
    "code" as sensitive would blank the status field in every capture."""
    r = Redactor()
    assert r.scrub({"code": 1, "data": [{"a": 2}]}) == {"code": 1, "data": [{"a": 2}]}
