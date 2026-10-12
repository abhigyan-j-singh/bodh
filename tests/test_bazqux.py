"""The BazQux client against a mock transport: the edges the live account showed."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from bodh.ids import short_to_long
from bodh.readers.bazqux import READ, BazQuxAuthError, BazQuxClient, _int_or_none, _parse_item


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    import bodh.readers.bazqux as bq

    monkeypatch.setattr(bq, "MIN_REQUEST_INTERVAL", 0.0)
    monkeypatch.setattr(bq, "BACKOFF_BASE", 0.0)
    monkeypatch.setattr(bq.time, "sleep", lambda _s: None)  # no backoff wait either (0.0**0 is a second)


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
            return httpx.Response(401, text="Unauthorized")  # the session expired between token and edit
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
            return httpx.Response(
                code, text="no", headers={"X-BQ-LoginErrorReason": reason} if reason else {}
            )

        client = BazQuxClient("e@x", "hunter2", transport=httpx.MockTransport(handler))
        with pytest.raises(BazQuxAuthError) as exc:
            client.login()
        assert str(code) in str(exc.value) and "hunter2" not in str(exc.value)
        if reason:
            assert reason in str(exc.value)


def test_pagination_bounded_on_repeating_continuation(monkeypatch):
    """A continuation that repeats, or never ends, is an error: a shorter list than the truth
    would read as "everything else was read" or "nothing else is protected"."""
    import bodh.readers.bazqux as bq
    from bodh.readers.bazqux import BazQuxError

    monkeypatch.setattr(bq, "MAX_PAGES", 10)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/ClientLogin":
            return httpx.Response(200, text="Auth=a\n")
        calls["n"] += 1
        return httpx.Response(200, json={"itemRefs": [{"id": str(calls["n"])}], "continuation": "same"})

    client = BazQuxClient("e@x", "pw", transport=httpx.MockTransport(handler))
    with pytest.raises(BazQuxError):
        client.unread_ids()
    assert calls["n"] == 2
    assert client.label_ids("Must read") == []  # a label: empty, as before
    with pytest.raises(BazQuxError):
        client.label_ids("Must read", strict=True)  # unless the caller must know
    calls["n"] = 0

    def fresh(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/ClientLogin":
            return httpx.Response(200, text="Auth=a\n")
        calls["n"] += 1
        return httpx.Response(
            200, json={"itemRefs": [{"id": str(calls["n"])}], "continuation": f"c{calls['n']}"}
        )

    client = BazQuxClient("e@x", "pw", transport=httpx.MockTransport(fresh))
    with pytest.raises(BazQuxError):
        client.unread_ids()
    assert calls["n"] == 10


def test_parse_item_tolerates_bad_timestamps():
    assert _int_or_none("1.7e9") == 1_700_000_000 and _int_or_none("1700") == 1700 and _int_or_none(5) == 5
    assert _int_or_none("soon") is None and _int_or_none(None) is None and _int_or_none("") is None
    raw = {
        "id": short_to_long(7),
        "title": "T",
        "published": "1.7e9",
        "crawlTimeMsec": "not-a-number",
        "origin": {"streamId": "feed/x", "title": "X"},
    }
    item = _parse_item(raw)
    assert item.published == 1_700_000_000 and item.crawled_msec is None and item.id == short_to_long(7)


def test_item_refs_tolerate_bad_timestamps():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/ClientLogin":
            return httpx.Response(200, text="Auth=a\n")
        return httpx.Response(
            200,
            json={"itemRefs": [{"id": "1", "timestampUsec": "1.7e15"}, {"id": "2", "timestampUsec": "x"}]},
        )

    client = BazQuxClient("e@x", "pw", transport=httpx.MockTransport(handler))
    refs = client.item_refs(READ)
    assert [r.timestamp_usec for r in refs] == [1_700_000_000_000_000, None]


# -- the real client over the fake server ------------------------------------------


def _server(n: int = 5, **kw):
    from fakes import FakeBazQuxServer, make_item

    items = [
        make_item(i, feed="Feed A" if i % 2 else "Feed B", title=f"Article {i}") for i in range(1, n + 1)
    ]
    return FakeBazQuxServer(items, folders={"Feed A": "Essays"}, **kw)


def test_pagination_continuation_and_empty_page():
    server = _server(5, page_size=2)
    client = server.client()
    refs = client.unread_ids()
    assert [r.id for r in refs] == [short_to_long(i) for i in range(1, 6)]
    pages = [p for p in server.requests if p[1] == "/reader/api/0/stream/items/ids"]
    assert len(pages) == 3 and [p[2].get("c") for p in pages] == [None, "2", "4"]
    assert refs[0].direct_stream_ids == ["feed/https://example.com/FeedA/rss"]
    server.unread.clear()
    assert client.unread_ids() == []  # an empty page ends the walk


def test_unread_ids_sends_xt_read_and_n_1000():
    server = _server(3)
    server.unread.discard(short_to_long(2))
    client = server.client()
    assert [r.id for r in client.unread_ids()] == [short_to_long(1), short_to_long(3)]
    _, _, params = next(p for p in server.requests if p[1] == "/reader/api/0/stream/items/ids")
    assert params["xt"] == READ and params["n"] == "1000" and params["output"] == "json"
    assert client.starred_ids() == []
    server.starred.add(short_to_long(3))
    assert client.starred_ids() == [short_to_long(3)]


def test_stale_auth_token_relogs_in_once_and_a_second_401_is_an_error():
    server = _server(3)
    client = server.client()
    client.unread_ids()
    server.expire_auth_after = 1  # the session dies under the client
    assert len(client.unread_ids()) == 3 and server.logins == 2
    server.reject_auth_always = True
    with pytest.raises(BazQuxAuthError) as exc:
        client.unread_ids()
    assert "twice" in str(exc.value) and "hunter2" not in str(exc.value)


def test_bad_login_is_an_auth_error_without_the_password():
    server = _server(1)
    client = server.client(password="wrong-secret")
    with pytest.raises(BazQuxAuthError) as exc:
        client.unread_ids()
    assert "403" in str(exc.value) and "BadAuthentication" in str(exc.value)
    assert "wrong-secret" not in str(exc.value)
    server.login_without_auth = True
    with pytest.raises(BazQuxAuthError, match="no Auth token"):
        server.client().login()


def test_5xx_and_429_retry_bounded_and_transport_errors_too():
    import bodh.readers.bazqux as bq
    from bodh.readers.bazqux import BazQuxError

    server = _server(2)
    client = server.client()
    server.fail_next["/reader/api/0/stream/items/ids"] = [500, 429, 503]  # three failures, then the answer
    assert len(client.unread_ids()) == 2
    server.fail_next["/reader/api/0/stream/items/ids"] = [500] * bq.MAX_RETRIES
    with pytest.raises(BazQuxError, match="after 4 attempts"):
        client.unread_ids()
    server.raise_next["/reader/api/0/stream/items/ids"] = [
        httpx.ConnectError("boom"),
        httpx.ReadTimeout("slow"),
    ]
    assert len(client.unread_ids()) == 2
    server.raise_next["/reader/api/0/stream/items/ids"] = [httpx.ConnectError("boom")] * bq.MAX_RETRIES
    with pytest.raises(BazQuxError, match="boom"):
        client.unread_ids()


def test_edit_tag_chunks_add_and_remove_and_non_ok_body(monkeypatch):
    import bodh.readers.bazqux as bq
    from bodh.readers.bazqux import BazQuxError

    monkeypatch.setattr(bq, "EDIT_BATCH", 2)
    server = _server(5)
    client = server.client()
    ids = [short_to_long(i) for i in range(1, 6)]
    assert client.tag(ids, "score-90") == 5
    assert [len(e[0]) for e in server.edits] == [2, 2, 1] and server.labels["score-90"] == set(ids)
    assert client.untag(ids[:2], "score-90") == 2 and server.labels["score-90"] == set(ids[2:])
    assert client.mark_read(ids[:3]) == 3 and server.unread == set(ids[3:])
    assert client.mark_unread(ids[:1]) == 1 and short_to_long(1) in server.unread
    assert client.edit_tag([], add=READ) == 0 and client.edit_tag(ids, None, None) == 0
    server.edit_reply = "ERR"
    with pytest.raises(BazQuxError, match="edit-tag returned"):
        client.mark_read(ids[:1])


def test_write_token_refreshes_after_20_minutes(monkeypatch):
    import bodh.readers.bazqux as bq

    server = _server(2)
    client = server.client()
    client.mark_read([short_to_long(1)])
    client.mark_read([short_to_long(2)])
    assert server.write_tokens == 1  # cached between writes
    now = bq.time.monotonic()
    monkeypatch.setattr(bq.time, "monotonic", lambda: now + 21 * 60)
    client.mark_unread([short_to_long(1)])
    assert server.write_tokens == 2


def test_contents_batches_vanished_bad_item_and_bad_json(monkeypatch):
    import bodh.readers.bazqux as bq
    from bodh.readers.bazqux import BazQuxError

    monkeypatch.setattr(bq, "CONTENTS_BATCH", 2)
    server = _server(3)
    server.garbage_items = [{"id": "not an id", "title": "garbage"}]  # one bad item per answer
    client = server.client()
    items, vanished = client.contents(
        [short_to_long(1), short_to_long(2), short_to_long(3), short_to_long(99)]
    )
    assert [i.id for i in items] == [short_to_long(1), short_to_long(2), short_to_long(3)]
    assert vanished == [short_to_long(99)]
    assert items[0].feed_title == "Feed A" and items[0].categories[1] == "user/-/label/Essays"
    assert items[1].html == "<p>Some text about things.</p>" and items[1].url.endswith("/FeedB/2")
    posts = [p for p in server.requests if p[1] == "/reader/api/0/stream/items/contents"]
    assert len(posts) == 2  # 2 + 2 ids
    server.bad_json_next.add("/reader/api/0/stream/items/contents")
    with pytest.raises(BazQuxError, match="JSON"):
        client.contents([short_to_long(1)])


def test_subscriptions_folders_tags_and_user_info():
    server = _server(2)
    server.labels["Must read"] = {short_to_long(1)}
    client = server.client()
    subs = {s.title: s for s in client.subscriptions()}
    assert subs["Feed A"].folders == ["Essays"] and subs["Feed B"].folders == []
    assert subs["Feed A"].stream_id.startswith("feed/") and subs["Feed A"].html_url.endswith("/FeedA/")
    assert client.tags() == [
        "user/-/label/Essays",
        "user/-/label/Must read",
        "user/-/state/com.google/starred",
    ]
    assert client.label_ids("Must read") == [short_to_long(1)]
    assert client.label_ids("Essays", unread_only=True) == [short_to_long(1)]  # the folder as a stream
    assert client.user_info()["userEmail"] == "reader@example.com"
    assert client.label_ids("never used") == []


def test_pacing_waits_between_requests(monkeypatch):
    import bodh.readers.bazqux as bq

    waits: list[float] = []
    monkeypatch.setattr(bq, "MIN_REQUEST_INTERVAL", 0.5)
    monkeypatch.setattr(bq.time, "sleep", waits.append)
    server = _server(1)
    client = server.client()
    client.unread_ids()  # the login, then the ids: the second request waits
    assert waits and max(waits) > 0.3
