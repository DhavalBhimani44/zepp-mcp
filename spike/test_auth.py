import urllib.parse

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from auth import _AES_IV, _AES_KEY, LOGIN_VARIANTS, build_login_payload


def test_payload_round_trips_to_expected_fields():
    blob = build_login_payload("a@b.com", "hunter2", "us-west-2", "US")
    clear = unpad(
        AES.new(_AES_KEY, AES.MODE_CBC, iv=_AES_IV).decrypt(blob),
        AES.block_size,
    ).decode()
    fields = urllib.parse.parse_qs(clear)
    assert fields["emailOrPhone"] == ["a@b.com"]
    assert fields["password"] == ["hunter2"]
    assert fields["region"] == ["us-west-2"]
    assert fields["country_code"] == ["US"]
    assert fields["client_id"] == ["HuaMi"]
    assert fields["state"] == ["REDIRECTION"]


def test_payload_requests_both_token_types():
    blob = build_login_payload("a@b.com", "hunter2", "us-west-2", "US")
    clear = unpad(
        AES.new(_AES_KEY, AES.MODE_CBC, iv=_AES_IV).decrypt(blob),
        AES.block_size,
    ).decode()
    assert urllib.parse.parse_qs(clear)["token"] == ["access", "refresh"]


def test_payload_is_block_aligned():
    blob = build_login_payload("a@b.com", "hunter2", "us-west-2", "US")
    assert len(blob) % AES.block_size == 0


def test_variants_cover_multiple_regions():
    hosts = {host for host, _, _ in LOGIN_VARIANTS}
    assert len(hosts) > 1, "region discovery needs more than one candidate host"
