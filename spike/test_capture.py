import json

from capture import Capture
from redact import Redactor


def test_record_writes_redacted_file_and_returns_summary(tmp_path):
    r = Redactor()
    r.register("abcdef0123456789", "app_token")
    cap = Capture(tmp_path, r)

    result = cap.record(
        name="band_data",
        method="GET",
        url="https://api-mifit.zepp.com/v1/data/band_data.json",
        params={"apptoken": "abcdef0123456789", "from_date": "2026-08-01"},
        status=200,
        body_text='{"code":1,"data":[]}',
        elapsed_ms=412,
    )

    assert result["status"] == 200
    assert result["name"] == "band_data"
    written = sorted(tmp_path.glob("*.json"))
    assert len(written) == 1
    on_disk = json.loads(written[0].read_text())
    assert "abcdef0123456789" not in written[0].read_text()
    assert on_disk["params"]["apptoken"] == "<apptoken>"
    assert on_disk["body_parsed"] == {"code": 1, "data": []}


def test_record_keeps_raw_text_when_body_is_not_json(tmp_path):
    cap = Capture(tmp_path, Redactor())
    cap.record(
        name="weird",
        method="GET",
        url="https://x.zepp.com/y",
        params=None,
        status=403,
        body_text="<html>denied</html>",
        elapsed_ms=10,
    )
    on_disk = json.loads(sorted(tmp_path.glob("*.json"))[0].read_text())
    assert on_disk["body_parsed"] is None
    assert on_disk["body_text"] == "<html>denied</html>"


def test_flags_empty_200_as_suspicious(tmp_path):
    """The failure mode spec section 6 is built to defend against."""
    cap = Capture(tmp_path, Redactor())
    result = cap.record(
        name="empty", method="GET", url="https://x.zepp.com/y",
        params=None, status=200, body_text='{"code":1,"data":[]}', elapsed_ms=5,
    )
    assert result["empty_200"] is True


def test_non_empty_200_not_flagged(tmp_path):
    cap = Capture(tmp_path, Redactor())
    result = cap.record(
        name="ok", method="GET", url="https://x.zepp.com/y",
        params=None, status=200, body_text='{"data":[{"a":1}]}', elapsed_ms=5,
    )
    assert result["empty_200"] is False


def test_index_lists_every_capture_in_order(tmp_path):
    cap = Capture(tmp_path, Redactor())
    for n in ("one", "two"):
        cap.record(name=n, method="GET", url="https://x/y", params=None,
                   status=200, body_text='{"data":[1]}', elapsed_ms=1)
    index_path = cap.write_index()
    index = json.loads(index_path.read_text())
    assert [entry["name"] for entry in index] == ["one", "two"]


def test_filenames_are_ordered_and_unique(tmp_path):
    cap = Capture(tmp_path, Redactor())
    for _ in range(3):
        cap.record(name="same", method="GET", url="https://x/y", params=None,
                   status=200, body_text='{"data":[1]}', elapsed_ms=1)
    names = sorted(p.name for p in tmp_path.glob("*same.json"))
    assert names == ["000_same.json", "001_same.json", "002_same.json"]
