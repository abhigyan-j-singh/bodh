"""What the student's reading and tags teach the system, read back from BazQux state."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import FakeClient, make_item, seed_scored

from bodh import feedback, store


def test_empty_upstream_unread_list_is_not_a_bulk_dismiss(conn, cfg):
    """One empty (or truncated) answer from BazQux used to flip the whole pool to dismissed, a
    negative that taught the profile the pool was disliked. It takes two runs in a row now."""
    cfg.breakers.bulk_dismiss_threshold = 30
    ids = seed_scored(conn, cfg, [dict(n=i, score=60, feed=f"Feed {i % 5}", crawled_msec=None) for i in range(1, 41)])
    client = FakeClient()
    stats = feedback.collect(client, conn, cfg, unread_now=set(), starred_now=set())
    assert stats["dismissed"] == 0 and stats["reads"] == 0 and stats["suspect_flip"] == 40
    assert store.status_ids(conn, "unread") == set(ids)
    # a normal answer in between clears the suspicion
    stats = feedback.collect(client, conn, cfg, unread_now=set(ids), starred_now=set())
    assert stats["suspect_flip"] == 0 and store.kv_get(conn, feedback.SUSPECT_FLIP_KEY) is None
    # the same picture twice is believed: the student really did mark everything read
    feedback.collect(client, conn, cfg, unread_now=set(), starred_now=set())
    stats = feedback.collect(client, conn, cfg, unread_now=set(), starred_now=set())
    assert stats["dismissed"] == 40 and store.status_ids(conn, "dismissed") == set(ids)
    assert store.kv_get(conn, feedback.SUSPECT_FLIP_KEY) is None


def test_small_flips_are_believed_at_once(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=i, score=60, crawled_msec=None) for i in range(1, 31)])
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now=set(ids[10:]), starred_now=set())
    assert stats["reads"] == 10 and stats["suspect_flip"] == 0


def test_read_dismissed_expired_marked_unread_again_return_to_unread(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=60), dict(n=2, score=60), dict(n=3, score=60), dict(n=4, score=60)])
    store.set_flags(conn, ids[:1], status="read", read_at="x")
    store.add_feedback(conn, ids[0], "read")
    store.set_flags(conn, ids[1:2], status="dismissed")
    store.set_flags(conn, ids[2:3], status="expired")
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now=set(ids), starred_now=set())
    assert stats["returned"] == 3 and store.status_ids(conn, "unread") == set(ids)
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
    # only the article the student had read is exempt from the floor: a reversed dismissal or
    # expiry (one wrong answer from BazQux) must not retire the floor for the whole pool
    exempt = {r[0] for r in conn.execute("SELECT article_id FROM state WHERE exempt = 1")}
    assert exempt == {ids[0]}
    # and they are in the pool the cuts select from
    from bodh import cut

    cfg.cut.target_unread = 0
    assert set(cut.select_prune(conn, cfg).cut_ids) == set(ids)


def test_flip_larger_than_param_limit(conn, cfg):
    """A mark-all-read of a big pile is more ids than one IN clause may hold."""
    if not hasattr(conn, "setlimit"):
        pytest.skip("sqlite3.Connection.setlimit needs Python 3.11")
    now = store.utcnow_iso()
    ids = [make_item(n).id for n in range(1, 5001)]
    conn.executemany("INSERT INTO articles (id, title, feed_title, fetched_at) VALUES (?, 'a', 'f', ?)",
                     [(i, now) for i in ids])
    conn.executemany("INSERT INTO state (article_id, status) VALUES (?, 'unread')", [(i,) for i in ids])
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now=set(ids[2000:]), starred_now=set())
    assert stats["reads"] == 2000 and len(store.status_ids(conn, "read")) == 2000


def test_wrong_on_uncut_article_is_not_a_rescue(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=60), dict(n=2, score=10)])
    run_id = store.begin_run(conn, "run")
    store.mark_cut(conn, ids[1:], run_id, "floor")
    store.set_flags(conn, ids[1:], cut_tagged=1, read_marked=1)
    client = FakeClient(labels={cfg.tags.wrong: ids})
    stats = feedback.collect(client, conn, cfg, unread_now={ids[0]}, starred_now=set())
    assert stats["rescues"] == 1
    assert store.feedback_ids(conn, "rescue") == {ids[1]} and store.feedback_ids(conn, "wrong") == set(ids)
    assert client.unread_marked == [[ids[1]]] and (cfg.tags.cut, [ids[1]]) in client.untagged
    assert store.article(conn, ids[0])["rescued_at"] is None and store.article(conn, ids[1])["rescued_at"]


def test_rescue_rate_needs_minimum_cuts_and_ignores_bankruptcy(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=i, score=10) for i in range(1, 31)])
    run_id = store.begin_run(conn, "run")
    store.mark_cut(conn, ids[:10], run_id, "floor")
    store.set_flags(conn, ids[:10], cut_tagged=1, read_marked=1)
    store.restore_unread(conn, ids[:1], rescued=True)
    store.add_feedback(conn, ids[0], "rescue")
    assert feedback.rescue_rate_week(conn) == 0.0          # 10 cuts: too few to mean anything
    assert feedback.rescue_rate_week(conn, min_cuts=1) == 0.1
    store.mark_cut(conn, ids[10:], run_id, "bankruptcy")   # 20 bankruptcy cuts do not count
    assert feedback.rescue_rate_week(conn) == 0.0
    store.mark_cut(conn, ids[10:], run_id, "prune")
    assert feedback.rescue_rate_week(conn) == pytest.approx(1 / 30)


def test_expired_filter_is_linear(conn, cfg):
    import time as _t

    now = store.utcnow_iso()
    ids = [make_item(n).id for n in range(1, 6001)]
    conn.executemany("INSERT INTO articles (id, title, feed_title, crawled_msec, fetched_at) VALUES (?, 'a', 'f', 1, ?)",
                     [(i, now) for i in ids])
    conn.executemany("INSERT INTO state (article_id, status) VALUES (?, 'unread')", [(i,) for i in ids])
    t0 = _t.monotonic()
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now=set(ids[4000:]), starred_now=set())
    assert stats["expired"] == 4000 and _t.monotonic() - t0 < 5
