"""Client-layer behaviour: caching, pacing, error classification, re-auth,
pagination and range chunking. No network: every response comes from an
httpx.MockTransport handler."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import httpx
import pytest

from zepp_mcp import client as api
from zepp_mcp.auth import Credential, LoginError

UID = "1234567890"


def make_client(handler, cache: bool = True) -> api.ZeppClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    c = api.ZeppClient("user@example.com", "pw", http=http, min_interval=0, cache=cache)
    c._credential = Credential(
        app_token="tok",
        user_id=UID,
        region_host="api-mifit.example",
        country_code="US",
        expires_at=time.time() + 86400 * 10,
    )
    return c


@pytest.fixture(autouse=True)
def no_token_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("ZEPP_TOKEN_CACHE", "off")


# -- cache -------------------------------------------------------------------


def test_repeat_request_is_served_from_memory():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"items": [{"timestamp": 1}]})

    c = make_client(handler)
    params = {"from_date": "2020-01-01", "to_date": "2020-01-02"}
    assert c.get("/x", params).status == "ok"
    assert c.get("/x", params).status == "ok"
    assert len(calls) == 1


def test_errors_are_never_cached():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={"code": -50000})

    c = make_client(handler)
    c.get("/x", {"to_date": "2020-01-01"})
    c.get("/x", {"to_date": "2020-01-01"})
    assert len(calls) == 2


def test_cache_can_be_disabled():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"items": [1]})

    c = make_client(handler, cache=False)
    c.get("/x", {"to_date": "2020-01-01"})
    c.get("/x", {"to_date": "2020-01-01"})
    assert len(calls) == 2


def test_ranges_reaching_today_expire_quickly():
    today = dt.date.today().isoformat()
    assert api._cache_ttl({"to_date": today}) == api._CACHE_TTL_CURRENT
    assert api._cache_ttl({}) == api._CACHE_TTL_CURRENT
    assert api._cache_ttl({"to_date": "2020-01-01"}) == api._CACHE_TTL_PAST
    ms = str(int(dt.datetime(2020, 1, 2, tzinfo=dt.UTC).timestamp() * 1000))
    assert api._cache_ttl({"to": ms}) == api._CACHE_TTL_PAST


def test_nothing_is_cached_forever():
    """A watch that syncs late fills in past days, so even old ranges expire."""
    assert api._cache_ttl({"to_date": "2020-01-01"}) < float("inf")
    recent = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    assert api._cache_ttl({"to_date": recent}) == api._CACHE_TTL_CURRENT


def test_empty_answers_are_only_cached_briefly():
    """no_data may be absence, a silent rejection or an unsynced day."""
    old_range = {"to_date": "2020-01-01"}
    assert api._cache_ttl(old_range, "no_data") == api._CACHE_TTL_CURRENT


def test_pacing_sleep_does_not_hold_the_cache_lock(monkeypatch):
    c = make_client(lambda r: httpx.Response(200, json={"items": [1]}), cache=False)
    c._min_interval = 10.0
    held = []
    monkeypatch.setattr(api.time, "sleep", lambda s: held.append(c._lock.locked()))
    c.get("/a")
    c.get("/b")
    assert held == [False]


def test_pacing_waits_between_requests(monkeypatch):
    slept = []
    monkeypatch.setattr(api.time, "sleep", lambda s: slept.append(s))
    c = make_client(lambda r: httpx.Response(200, json={"items": [1]}), cache=False)
    c._min_interval = 10.0
    c.get("/a")
    c.get("/b")
    assert slept and slept[0] > 9


# -- error classification ----------------------------------------------------


@pytest.mark.parametrize(
    "status,kind",
    [
        (400, "bad_request"),
        (403, "forbidden"),
        (404, "not_found"),
        (429, "rate_limited"),
        (500, "server_error"),
        (503, "server_error"),
        (418, "http_error"),
    ],
)
def test_http_errors_are_classified(status, kind):
    c = make_client(lambda r: httpx.Response(status, text="nope"))
    outcome = c.get(f"/users/{UID}/thing")
    assert outcome.status == "error"
    assert outcome.error_kind == kind
    # The user id never travels back to the model in an error note.
    assert UID not in outcome.note
    assert "{uid}" in outcome.note


def test_rate_limit_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429)

    make_client(handler).get("/x")
    assert len(calls) == 1


def test_network_error_is_an_outcome_not_an_exception():
    def handler(request):
        raise httpx.ConnectError("boom")

    outcome = make_client(handler).get("/x")
    assert outcome.status == "error" and outcome.error_kind == "network"


def test_non_json_body_is_an_error():
    outcome = make_client(lambda r: httpx.Response(200, text="<html>")).get("/x")
    assert outcome.status == "error"


def test_empty_200_is_no_data_with_the_ambiguity_attached():
    outcome = make_client(lambda r: httpx.Response(200, json={"items": []})).get("/x")
    assert outcome.status == "no_data"
    assert "not report it as confirmed absence" in outcome.note


# -- re-auth -----------------------------------------------------------------


def test_401_reauthenticates_exactly_once_and_never_loops(monkeypatch):
    logins = []

    def fake_login(*args, **kwargs):
        logins.append(args)
        raise LoginError("rejected")

    monkeypatch.setattr(api, "login", fake_login)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401)

    outcome = make_client(handler).get("/x")
    assert outcome.status == "error" and outcome.error_kind == "unauthorized"
    assert len(logins) == 1
    assert len(calls) == 1


def test_401_then_success_after_one_login(monkeypatch):
    def fake_login(*args, **kwargs):
        return Credential(
            "tok2", UID, "api-mifit.example", "US", time.time() + 86400 * 10
        )

    monkeypatch.setattr(api, "login", fake_login)
    tokens = []

    def handler(request):
        tokens.append(request.headers["apptoken"])
        if request.headers["apptoken"] == "tok":
            return httpx.Response(401)
        return httpx.Response(200, json={"items": [1]})

    assert make_client(handler).get("/x").status == "ok"
    assert tokens == ["tok", "tok2"]


# -- events pagination -------------------------------------------------------


def _event_server(total: int, store: list[dict[str, Any]] | None = None):
    items = [
        {"eventType": "e", "subType": "s", "timestamp": 1_700_000_000_000 + i * 1000}
        for i in range(total)
    ]

    def handler(request):
        params = dict(request.url.params)
        if store is not None:
            store.append(params)
        lo, hi, limit = int(params["from"]), int(params["to"]), int(params["limit"])
        page = [i for i in items if lo <= i["timestamp"] <= hi][:limit]
        return httpx.Response(200, json={"items": page})

    return handler, items


def test_events_walk_every_page_by_advancing_from():
    seen: list[dict[str, Any]] = []
    handler, items = _event_server(45, seen)
    c = make_client(handler)
    got = api._walk_events(
        c, "/v2/users/me/events", {"eventType": "e"}, 0, 2_000_000_000_000, limit=20
    )
    assert got.status == "ok"
    assert len(got.items) == 45 and got.requests == 3
    # Inclusive: the next page starts AT the last timestamp seen.
    assert int(seen[1]["from"]) == items[19]["timestamp"]


def test_items_sharing_a_timestamp_across_a_page_boundary_are_kept():
    """Three items at one timestamp, split 2/1 by a page of 4: stepping past
    the last timestamp would have dropped the third."""
    items = [{"eventType": "e", "timestamp": t, "n": n}
             for n, t in enumerate([1, 2, 5, 5, 5, 9])]

    def handler(request):
        lo = int(request.url.params["from"])
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json={"items": [
            i for i in items if i["timestamp"] >= lo][:limit]})

    got = api._walk_events(make_client(handler), "/p", {}, 0, 10**13, limit=4)
    assert sorted(i["n"] for i in got.items) == list(range(6))


def test_pai_reuploads_with_the_same_timestamp_both_survive():
    """PAI re-states a day under the same timestamp; the later upload must
    reach the decoder, which keeps the latest."""
    first = {"eventType": "PaiHealthInfo", "timestamp": 100, "time": "100",
             "uploadTimestamp": "1", "dailyPai": "3", "totalPai": "3"}
    second = {**first, "uploadTimestamp": "2", "dailyPai": "9", "totalPai": "9"}
    handler = lambda r: httpx.Response(200, json={"items": [first, second]})  # noqa: E731
    got = api._walk_events(make_client(handler), "/p", {}, 0, 10**13, limit=20)
    assert len(got.items) == 2
    from zepp_mcp import events
    assert events.pai({"items": got.items})[0]["pai_earned_today"] == 9.0


def test_a_full_page_of_one_timestamp_steps_past_it_and_says_so():
    items = [{"eventType": "e", "timestamp": 5, "n": n} for n in range(6)]

    def handler(request):
        lo = int(request.url.params["from"])
        return httpx.Response(200, json={"items": [
            i for i in items if i["timestamp"] >= lo][:4]})

    got = api._walk_events(make_client(handler), "/p", {}, 0, 10**13, limit=4)
    assert got.requests == 3 and got.notes


def test_events_dedupe_when_server_treats_from_inclusively():
    items = [{"eventType": "e", "subType": "s", "timestamp": t} for t in range(1, 26)]

    def handler(request):
        lo = int(request.url.params["from"]) - 1  # inclusive overlap of one
        return httpx.Response(
            200, json={"items": [i for i in items if i["timestamp"] >= lo][:20]}
        )

    got = api._walk_events(make_client(handler), "/p", {}, 1, 10**13, limit=20)
    assert [i["timestamp"] for i in got.items] == list(range(1, 26))


def test_events_page_cap_marks_partial(monkeypatch):
    monkeypatch.setattr(api, "MAX_PAGES", 2)
    handler, _ = _event_server(100)
    got = api._walk_events(make_client(handler), "/p", {}, 0, 10**13, limit=20)
    # Two pages of 20 overlapping by the one item at the boundary timestamp.
    assert got.status == "partial" and len(got.items) == 39
    assert got.notes


def test_events_later_page_failure_keeps_earlier_items():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(500)
        return httpx.Response(
            200, json={"items": [{"eventType": "e", "timestamp": t} for t in range(20)]}
        )

    got = api._walk_events(make_client(handler), "/p", {}, 0, 10**13, limit=20)
    assert got.status == "partial" and len(got.items) == 20


def test_events_first_page_failure_is_error():
    got = api._walk_events(
        make_client(lambda r: httpx.Response(404)), "/p", {}, 0, 10**13, limit=20
    )
    assert got.status == "error" and got.error.error_kind == "not_found"


# -- band_data chunking ------------------------------------------------------


def test_band_data_is_chunked_and_deduplicated():
    windows = []

    def handler(request):
        p = request.url.params
        windows.append((p["from_date"], p["to_date"]))
        return httpx.Response(
            200,
            json={"data": [{"date_time": p["from_date"]}, {"date_time": "2026-01-01"}]},
        )

    got = api.band_data_range(make_client(handler), "2026-01-01", "2026-03-15")
    assert windows[0] == ("2026-01-01", "2026-01-31")
    assert windows[-1][1] == "2026-03-15"
    assert len(windows) == 3
    dates = [r["date_time"] for r in got.items]
    assert len(dates) == len(set(dates)) and dates == sorted(dates)


def test_band_data_failed_chunk_is_partial_and_named():
    def handler(request):
        if request.url.params["from_date"] == "2026-02-01":
            return httpx.Response(500)
        return httpx.Response(
            200, json={"data": [{"date_time": request.url.params["from_date"]}]}
        )

    got = api.band_data_range(make_client(handler), "2026-01-01", "2026-03-15")
    assert got.status == "partial"
    assert any("2026-02-01" in n for n in got.notes)


# -- workout history paging --------------------------------------------------


def test_history_follows_next_cursor():
    pages = {
        None: ({"trackid": "300"}, 200),
        "200": ({"trackid": "200"}, 100),
        "100": ({"trackid": "100"}, -1),
    }
    seen = []

    def handler(request):
        cursor = request.url.params.get(api.HISTORY_CURSOR_PARAM)
        seen.append(cursor)
        row, nxt = pages[cursor]
        return httpx.Response(200, json={"data": {"summary": [row], "next": nxt}})

    got = api.workout_history_all(make_client(handler), "2026-01-01", "2026-02-01")
    assert got.status == "ok"
    assert [r["trackid"] for r in got.items] == ["300", "200", "100"]
    assert seen == [None, "200", "100"]


def test_history_stops_when_a_page_repeats():
    def handler(request):
        return httpx.Response(
            200, json={"data": {"summary": [{"trackid": "1"}], "next": 5}}
        )

    got = api.workout_history_all(make_client(handler), "2026-01-01", "2026-02-01")
    assert got.status == "partial" and len(got.items) == 1


def test_history_nested_json_string_rows():
    def handler(request):
        return httpx.Response(
            200, json={"data": {"summary": '[{"trackid": "9"}]', "next": -1}}
        )

    got = api.workout_history_all(make_client(handler), "2026-01-01", "2026-02-01")
    assert got.items == [{"trackid": "9"}]


def test_events_by_date_chunks_and_passes_timezone():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(
            200, json={"items": [{"eventType": "b", "timestamp": len(seen)}]}
        )

    got = api.events_by_date(
        make_client(handler),
        "blood_oxygen",
        "odi",
        "2026-01-01",
        "2026-03-01",
        "Asia/Kolkata",
        limit=31,
    )
    assert got.status == "ok" and len(seen) == 2
    assert seen[0]["timeZone"] == "Asia/Kolkata"
    assert f"/users/{UID}/" not in (got.coverage().get("notes") or [""])[0]
