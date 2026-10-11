"""The BazQux client against a mock transport: the edges the live account showed."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from triage.bazqux import READ, BazQuxAuthError, BazQuxClient, _int_or_none, _parse_item
from triage.ids import short_to_long


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    import triage.bazqux as bq

    monkeypatch.setattr(bq, "MIN_REQUEST_INTERVAL", 0.0)
    monkeypatch.setattr(bq, "BACKOFF_BASE", 0.0)


def test_retry_after_relogin_uses_fresh_write_token():
    """The write token captured before a 401 was reused on the retry after the re-login."""
    state = {"logins": 0, "tokens": 0, "edits": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/accounts/ClientLogin":
            state["logins"] += 1
            return httpx.Response(200, text=f"Auth=auth{state['logins']}\n")
        auth = request.headers.get("Authorization", "")
        if auth.endswith("auth1") and path == "/reader/api/0/edit-tag":
            return httpx.Response(401, text="Unauthorized")   # the session expired between token and edit
        if path == "/reader/api/0/token":
            state["tokens"] += 1
            return httpx.Response(200, text=f"T{state['tokens']}-for-{auth[-5:]}")
        if path == "/reader/api/0/edit-tag":
            state["edits"].append(parse_qs(request.content.decode())["T"][0])
            return httpx.Response(200, text="OK")
        return httpx.Response(404)

    client = BazQuxClient("e@x", "pw", transport=httpx.MockTransport(handler))
    assert client.mark_read([short_to_long(1)]) == 1
    assert state["logins"] == 2 and state["edits"] == ["T2-for-auth2"]


def test_login_401_and_403_raise_auth_error():
    for code, reason in ((401, ""), (403, "BadAuthentication")):
        def handler(request: httpx.Request, code=code, reason=reason) -> httpx.Response:
            return httpx.Response(code, text="no", headers={"X-BQ-LoginErrorReason": reason} if reason else {})

        client = BazQuxClient("e@x", "hunter2", transport=httpx.MockTransport(handler))
        with pytest.raises(BazQuxAuthError) as exc:
            client.login()
        assert str(code) in str(exc.value) and "hunter2" not in str(exc.value)
        if reason:
            assert reason in str(exc.value)


def test_pagination_bounded_on_repeating_continuation(monkeypatch):
    import triage.bazqux as bq

    monkeypatch.setattr(bq, "MAX_PAGES", 10)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/ClientLogin":
            return httpx.Response(200, text="Auth=a\n")
        calls["n"] += 1
        return httpx.Response(200, json={"itemRefs": [{"id": str(calls["n"])}], "continuation": "same"})

    client = BazQuxClient("e@x", "pw", transport=httpx.MockTransport(handler))
    refs = client.unread_ids()
    assert calls["n"] == 2 and len(refs) == 2          # the second page repeats the continuation: stop

    calls["n"] = 0

    def fresh(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/ClientLogin":
            return httpx.Response(200, text="Auth=a\n")
        calls["n"] += 1
        return httpx.Response(200, json={"itemRefs": [{"id": str(calls["n"])}], "continuation": f"c{calls['n']}"})

    client = BazQuxClient("e@x", "pw", transport=httpx.MockTransport(fresh))
    assert len(client.unread_ids()) == 10 and calls["n"] == 10   # never-ending continuations: the page cap


def test_parse_item_tolerates_bad_timestamps():
    assert _int_or_none("1.7e9") == 1_700_000_000 and _int_or_none("1700") == 1700 and _int_or_none(5) == 5
    assert _int_or_none("soon") is None and _int_or_none(None) is None and _int_or_none("") is None
    raw = {"id": short_to_long(7), "title": "T", "published": "1.7e9", "crawlTimeMsec": "not-a-number",
           "origin": {"streamId": "feed/x", "title": "X"}}
    item = _parse_item(raw)
    assert item.published == 1_700_000_000 and item.crawled_msec is None and item.id == short_to_long(7)


def test_item_refs_tolerate_bad_timestamps():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/ClientLogin":
            return httpx.Response(200, text="Auth=a\n")
        return httpx.Response(200, json={"itemRefs": [{"id": "1", "timestampUsec": "1.7e15"}, {"id": "2", "timestampUsec": "x"}]})

    client = BazQuxClient("e@x", "pw", transport=httpx.MockTransport(handler))
    refs = client.item_refs(READ)
    assert [r.timestamp_usec for r in refs] == [1_700_000_000_000_000, None]
