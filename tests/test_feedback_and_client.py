from __future__ import annotations

import json

import httpx
from conftest import FakeClient, make_item, seed_scored

from bodh import feedback, store
from bodh.ids import short_to_long
from bodh.readers.bazqux import READ, BazQuxClient, _parse_item, label


def test_reads_vs_bulk_dismiss_and_rescue(conn, cfg):
    cfg.safety.mark_all_read_threshold = 3
    ids = seed_scored(
        conn, cfg, [dict(n=i, score=60, feed=f"Feed {i % 4}", crawled_msec=None) for i in range(1, 8)]
    )
    # the student read two of them (no longer unread upstream), two others got cut by us earlier
    run_id = store.begin_run(conn, "run")
    store.mark_cut(conn, ids[5:7], run_id, "floor")
    store.set_flags(conn, ids[5:7], cut_tagged=1, read_marked=1)
    unread_now = set(ids[2:5]) | {ids[5]}  # ids[0], ids[1] were read; ids[5] (cut) is unread again: a rescue
    client = FakeClient(labels={cfg.tags.rescue: [], cfg.tags.meh: [ids[3]], cfg.tags.prune: [ids[4]]})
    stats = feedback.collect(client, conn, cfg, unread_now, starred_now={ids[2]})
    assert stats["reads"] == 2 and stats["dismissed"] == 0
    assert stats["rescues"] == 1 and stats["verdicts"] == 1 and stats["prune_requested"] is True
    assert store.feedback_ids(conn, "meh") == {ids[3]}
    assert not any(name == cfg.tags.meh for name, _ in client.untagged)  # a verdict stays on the article
    assert stats["stars"] == 1
    assert store.feedback_ids(conn, "read") == set(ids[:2])
    assert store.feedback_ids(conn, "rescue") == {ids[5]}
    assert store.status_ids(conn, "unread") >= {ids[5], ids[2], ids[3], ids[4]}
    assert ("cut", [ids[5]]) in client.untagged
    assert any(name == cfg.tags.prune for name, _ in client.untagged)


def test_bulk_dismiss(conn, cfg):
    cfg.safety.mark_all_read_threshold = 3
    ids = seed_scored(
        conn, cfg, [dict(n=i, score=60, feed=f"Feed {i % 3}", crawled_msec=None) for i in range(1, 6)]
    )
    client = FakeClient()
    stats = feedback.collect(client, conn, cfg, unread_now=set(), starred_now=set())
    assert stats["dismissed"] == 5 and stats["reads"] == 0
    assert store.status_ids(conn, "dismissed") == set(ids)


def test_parse_item_shapes():
    raw = {
        "id": "tag:google.com,2005:reader/item/000000a00000ecc3",
        "title": "T",
        "published": 1700000000,
        "crawlTimeMsec": "1700000000123",
        "alternate": [{"href": "https://x.test/a"}],
        "canonical": {"href": "https://x.test/c"},
        "summary": {"content": "<p>short</p>"},
        "content": {"content": "<p>much longer content here</p>"},
        "origin": {"streamId": "feed/https://x.test/rss", "title": "X", "htmlUrl": "https://x.test"},
        "categories": ["user/1/state/com.google/reading-list", "user/1/label/Folder"],
    }
    item = _parse_item(raw)
    assert item.url == "https://x.test/c" and item.html.startswith("<p>much longer")
    assert item.feed_title == "X" and item.crawled_msec == 1700000000123 and item.published == 1700000000


def _mock_transport(state: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        state.setdefault("calls", []).append((request.method, path, request.url.params, request.content))
        if path == "/accounts/ClientLogin":
            state["logins"] = state.get("logins", 0) + 1
            return httpx.Response(200, text=f"SID=x\nLSID=y\nAuth=token{state['logins']}\n")
        auth = request.headers.get("Authorization", "")
        if state.get("expire_first") and auth.endswith("token1"):
            return httpx.Response(401, text="Unauthorized")
        if path == "/reader/api/0/token":
            return httpx.Response(200, text="T123")
        if path == "/reader/api/0/stream/items/ids":
            page = request.url.params.get("c")
            if page is None:
                return httpx.Response(
                    200,
                    json={"itemRefs": [{"id": "1", "timestampUsec": "5"}, {"id": "2"}], "continuation": "p2"},
                )
            return httpx.Response(200, json={"itemRefs": [{"id": "-3"}]})
        if path == "/reader/api/0/edit-tag":
            return httpx.Response(200, text="OK")
        if path == "/reader/api/0/stream/items/contents":
            from urllib.parse import parse_qs

            ids = parse_qs(request.content.decode()).get("i", [])
            items = [
                {"id": i, "title": f"t{i[-4:]}", "origin": {"streamId": "feed/x", "title": "X"}} for i in ids
            ]
            return httpx.Response(200, json={"items": items})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_client_pagination_relogin_and_edit_batches(monkeypatch):
    import bodh.readers.bazqux as bq

    monkeypatch.setattr(bq, "MIN_REQUEST_INTERVAL", 0.0)
    monkeypatch.setattr(bq, "EDIT_BATCH", 2)
    state = {"expire_first": True}
    client = BazQuxClient("e@x", "pw", transport=_mock_transport(state))
    refs = client.unread_ids()
    assert [r.id for r in refs] == [short_to_long(1), short_to_long(2), short_to_long(-3)]
    assert state["logins"] == 2  # the first token was rejected once, re-login happened once
    assert refs[0].timestamp_usec == 5
    n = client.edit_tag([short_to_long(i) for i in range(1, 6)], add=READ)
    assert n == 5
    edit_calls = [c for c in state["calls"] if c[1] == "/reader/api/0/edit-tag"]
    assert len(edit_calls) == 3  # 2 + 2 + 1
    assert b"a=user%2F-%2Fstate%2Fcom.google%2Fread" in edit_calls[0][3] and b"T=T123" in edit_calls[0][3]
    items, vanished = client.contents([short_to_long(1), short_to_long(2), short_to_long(9)])
    assert len(items) == 3 and vanished == []  # the mock answers every id
    assert label("Must read") == "user/-/label/Must read"


def test_store_rebuild_marks_and_scores(conn, cfg):
    item = make_item(1)
    store.upsert_article(conn, item, "Core")
    verdict = {
        "quality": 70,
        "learning": 70,
        "fit": 70,
        "breadth": 70,
        "relevance": 70,
        "lesson": "l",
        "concept": "c",
        "kind": "reporting",
        "flags": [],
        "dupe_of": None,
        "confidence": "high",
    }
    store.record_score(conn, item.id, verdict, 70.0, 70.0, "score-70", "teacher.v1", "m")
    assert store.untagged_bands(conn) == {"score-70": [item.id]}
    store.mark_band_tagged(conn, [item.id])
    store.record_review(conn, item.id, 92.0, "opus", 92.0, "score-90")
    assert store.untagged_bands(conn) == {"score-90": [item.id]}
    row = store.article(conn, item.id)
    assert row["score"] == 92.0 and row["second_look_score"] == 92.0 and json.loads(row["flags"]) == []


def test_costs_can_be_recorded_from_worker_threads(conn):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    lock = threading.Lock()

    def write(i):
        with lock:
            store.record_cost(conn, None, "m", "t", {"input_tokens": i}, 0.001)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(20)))
    assert conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 20


def test_duplicate_index(conn, cfg):
    from bodh.judgment import DupIndex

    seed_scored(
        conn,
        cfg,
        [
            dict(
                n=1,
                score=50,
                title="A long enough title to be matched exactly",
                url="https://a.test/x?utm_source=rss",
            )
        ],
    )
    index = DupIndex(conn)
    twin = {"id": short_to_long(2), "url": "https://a.test/x", "title": "different", "feed_title": "Other"}
    assert index.find(twin)["id"] == short_to_long(1)
    same_title = {
        "id": short_to_long(3),
        "url": "https://b.test/y",
        "title": "A long enough title to be matched exactly",
        "feed_title": "Other",
    }
    assert index.find(same_title)["id"] == short_to_long(1)
    same_feed = {
        "id": short_to_long(4),
        "url": "https://b.test/z",
        "title": "A long enough title to be matched exactly",
        "feed_title": "Feed A",
    }
    assert index.find(same_feed) is None


def test_verdict_tags_record_once_and_steer(conn, cfg, tmp_path, monkeypatch):
    cfg.root = tmp_path
    (tmp_path / "profile").mkdir(exist_ok=True)
    # an install from before October 2026 still has learner.md: it is read and written in place
    (tmp_path / "profile" / "learned.md").unlink()
    (tmp_path / "profile" / "learner.md").write_text(
        "## What the student has been learning\nnothing yet\n", encoding="utf-8"
    )
    assert cfg.learned_path.name == "learner.md"
    monkeypatch.setattr("bodh.profiler.git_commit", lambda *a, **k: True)
    ids = seed_scored(conn, cfg, [dict(n=i, score=40 + 10 * i, concept=f"concept {i}") for i in range(1, 7)])
    labels = {
        cfg.tags.great: [ids[0]],
        cfg.tags.good: [ids[1], ids[2]],
        cfg.tags.bad: [ids[2]],
        cfg.tags.basic: [ids[3]],
        cfg.tags.more: [ids[4]],
        cfg.tags.less: [ids[5]],
        cfg.tags.meh: [],
    }
    client = FakeClient(labels=labels)
    stats = feedback.collect(client, conn, cfg, unread_now=set(ids), starred_now=set())
    # one grade per article, the harsher present tag wins; levels and steers each recorded once
    assert store.verdicts(conn, store.GRADES) == {ids[0]: "great", ids[1]: "good", ids[2]: "bad"}
    assert store.verdicts(conn, store.LEVELS) == {ids[3]: "basic"}
    assert store.verdicts(conn, store.STEERS) == {ids[4]: "more", ids[5]: "less"}
    assert stats["verdicts"] == 6 and stats["steers"] == 2
    # the tags stay on the articles: nothing untagged but the commands
    assert all(name in (cfg.tags.cut, cfg.tags.rescue, cfg.tags.prune) for name, _ in client.untagged)
    # the steers reached the learned profile (under its old file name), with the article's concept, in one version
    text = (tmp_path / "profile" / "learner.md").read_text(encoding="utf-8")
    assert not (tmp_path / "profile" / "learned.md").exists()
    assert "wants more on concept 5" in text and "wants less of concept 6" in text and "## Steers" in text
    assert len(store.learned_versions(conn, 10)) == 2  # initial + one nudge version
    # a second run with the same tags changes nothing and nudges nothing
    stats = feedback.collect(client, conn, cfg, unread_now=set(ids), starred_now=set())
    assert stats["verdicts"] == 0 and stats["steers"] == 0
    assert len(store.learned_versions(conn, 10)) == 2
    # changing your mind: the good tag removed, bad added
    client.labels[cfg.tags.good] = [ids[2]]
    client.labels[cfg.tags.bad] = [ids[1], ids[2]]
    feedback.collect(client, conn, cfg, unread_now=set(ids), starred_now=set())
    assert store.verdicts(conn, store.GRADES)[ids[1]] == "bad"
    # removing a tag alone retracts nothing
    client.labels[cfg.tags.great] = []
    feedback.collect(client, conn, cfg, unread_now=set(ids), starred_now=set())
    assert store.verdicts(conn, store.GRADES)[ids[0]] == "great"
    # the teacher's lists and the article's record
    from bodh.judge import concept_context

    ctx = concept_context(conn, cfg)
    assert "Loved" in ctx and "concept 1: 1" in ctx and "Too basic" in ctx and "concept 4: 1" in ctx
    assert "Marked bad or meh" in ctx and "concept 3: 1" in ctx
    assert [k for k, _ in store.feedback_for(conn, ids[4])] == ["more"]


def test_login_and_heartbeat_logs_carry_no_secrets(caplog, monkeypatch):
    """Log lines reach progress.json and the dashboard, so the account email and the ping URL
    (whose path is the check's secret) must never appear in them."""
    import logging

    from bodh.heartbeat import Heartbeat

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("ClientLogin"):
            return httpx.Response(200, text="SID=x\nAuth=token-value\n")
        raise httpx.ConnectError("down")

    client = BazQuxClient("student@example.com", "hunter2", transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.INFO):
        client.login()
        monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
        Heartbeat("https://hc-ping.com/0123abcd-secret-uuid").ok("summary")
    text = caplog.text
    assert "student@example.com" not in text and "hunter2" not in text and "token-value" not in text
    assert "0123abcd-secret-uuid" not in text and "hc-ping.com" in text
