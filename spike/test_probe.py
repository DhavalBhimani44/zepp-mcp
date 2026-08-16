import os

from probe import load_dotenv


def _clean(monkeypatch, *keys):
    for key in keys:
        monkeypatch.delenv(key, raising=False)


def test_reads_key_value_pairs(tmp_path, monkeypatch):
    _clean(monkeypatch, "ZEPP_EMAIL", "ZEPP_PASSWORD")
    env = tmp_path / ".env"
    env.write_text("ZEPP_EMAIL=a@b.com\nZEPP_PASSWORD=hunter2\n")
    load_dotenv(env)
    assert os.environ["ZEPP_EMAIL"] == "a@b.com"
    assert os.environ["ZEPP_PASSWORD"] == "hunter2"


def test_real_environment_wins_over_file(tmp_path, monkeypatch):
    """So `ZEPP_EMAIL=x uv run probe.py` overrides a stale .env."""
    monkeypatch.setenv("ZEPP_EMAIL", "from-env@b.com")
    env = tmp_path / ".env"
    env.write_text("ZEPP_EMAIL=from-file@b.com\n")
    load_dotenv(env)
    assert os.environ["ZEPP_EMAIL"] == "from-env@b.com"


def test_skips_comments_and_blank_lines(tmp_path, monkeypatch):
    _clean(monkeypatch, "ZEPP_EMAIL")
    env = tmp_path / ".env"
    env.write_text("# a comment\n\n   \nZEPP_EMAIL=a@b.com\n")
    load_dotenv(env)
    assert os.environ["ZEPP_EMAIL"] == "a@b.com"


def test_strips_quotes_so_a_hash_in_a_password_survives(tmp_path, monkeypatch):
    """A '#' inside a quoted value is part of the password, not a comment."""
    _clean(monkeypatch, "ZEPP_PASSWORD")
    env = tmp_path / ".env"
    env.write_text('ZEPP_PASSWORD="p#ss w0rd"\n')
    load_dotenv(env)
    assert os.environ["ZEPP_PASSWORD"] == "p#ss w0rd"


def test_tolerates_export_prefix(tmp_path, monkeypatch):
    _clean(monkeypatch, "ZEPP_EMAIL")
    env = tmp_path / ".env"
    env.write_text("export ZEPP_EMAIL=a@b.com\n")
    load_dotenv(env)
    assert os.environ["ZEPP_EMAIL"] == "a@b.com"


def test_missing_file_is_not_an_error(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == {}


def test_return_value_never_contains_secret_values(tmp_path, monkeypatch):
    """The return value is logged/inspected; it must not carry credentials."""
    _clean(monkeypatch, "ZEPP_PASSWORD")
    env = tmp_path / ".env"
    env.write_text("ZEPP_PASSWORD=hunter2\n")
    applied = load_dotenv(env)
    assert "hunter2" not in str(applied)
    assert applied == {"ZEPP_PASSWORD": "set"}
