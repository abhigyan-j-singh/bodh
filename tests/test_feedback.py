"""What the student's reading and tags teach the system, read back from BazQux state."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeClient, make_item, seed_scored

from bodh import feedback, store


def test_empty_upstream_unread_list_is_not_a_bulk_dismiss(conn, cfg):
    """One empty (or truncated) answer from BazQux used to flip the whole pool to dismissed, a
    negative that taught the profile the pool was disliked. It takes two runs in a row now."""
    cfg.safety.mark_all_read_threshold = 30
    ids = seed_scored(
        conn, cfg, [dict(n=i, score=60, feed=f"Feed {i % 5}", crawled_msec=None) for i in range(1, 41)]
    )
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
    ids = seed_scored(
        conn, cfg, [dict(n=1, score=60), dict(n=2, score=60), dict(n=3, score=60), dict(n=4, score=60)]
    )
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
    from bodh import cutting

    cfg.prune.keep = 0
    assert set(cutting.select_prune(conn, cfg).cut_ids) == set(ids)


def test_flip_larger_than_param_limit(conn, cfg):
    """A mark-all-read of a big pile is more ids than one IN clause may hold."""
    if not hasattr(conn, "setlimit"):
        pytest.skip("sqlite3.Connection.setlimit needs Python 3.11")
    now = store.utcnow_iso()
    ids = [make_item(n).id for n in range(1, 5001)]
    conn.executemany(
        "INSERT INTO articles (id, title, feed_title, fetched_at) VALUES (?, 'a', 'f', ?)",
        [(i, now) for i in ids],
    )
    conn.executemany("INSERT INTO state (article_id, status) VALUES (?, 'unread')", [(i,) for i in ids])
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now=set(ids[2000:]), starred_now=set())
    assert stats["reads"] == 2000 and len(store.status_ids(conn, "read")) == 2000


def test_wrong_on_uncut_article_is_not_a_rescue(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=60), dict(n=2, score=10)])
    run_id = store.begin_run(conn, "run")
    store.mark_cut(conn, ids[1:], run_id, "floor")
    store.set_flags(conn, ids[1:], cut_tagged=1, read_marked=1)
    client = FakeClient(labels={cfg.tags.rescue: ids})
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
    assert feedback.rescue_rate_week(conn) == 0.0  # 10 cuts: too few to mean anything
    assert feedback.rescue_rate_week(conn, min_cuts=1) == 0.1
    store.mark_cut(conn, ids[10:], run_id, "bankruptcy")  # 20 bankruptcy cuts do not count
    assert feedback.rescue_rate_week(conn) == 0.0
    store.mark_cut(conn, ids[10:], run_id, "prune")
    assert feedback.rescue_rate_week(conn) == pytest.approx(1 / 30)


def test_expired_filter_is_linear(conn, cfg):
    import time as _t

    now = store.utcnow_iso()
    ids = [make_item(n).id for n in range(1, 6001)]
    conn.executemany(
        "INSERT INTO articles (id, title, feed_title, crawled_msec, fetched_at) VALUES (?, 'a', 'f', 1, ?)",
        [(i, now) for i in ids],
    )
    conn.executemany("INSERT INTO state (article_id, status) VALUES (?, 'unread')", [(i,) for i in ids])
    t0 = _t.monotonic()
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now=set(ids[4000:]), starred_now=set())
    assert stats["expired"] == 4000 and _t.monotonic() - t0 < 5


# -- what collect records --------------------------------------------------------------


def test_nothing_changed_reports_zero_everywhere(conn, cfg):
    """The counters all start at zero and the dictionary carries exactly the known keys."""
    ids = seed_scored(conn, cfg, [dict(n=1, score=60), dict(n=2, score=60)])
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now=set(ids), starred_now=set())
    assert set(stats) == {
        "stars",
        "reads",
        "dismissed",
        "expired",
        "returned",
        "suspect_flip",
        "rescues",
        "verdicts",
        "steers",
        "prune_requested",
    }
    assert (
        all(v == 0 for k, v in stats.items() if k != "prune_requested") and stats["prune_requested"] is False
    )


def test_stars_are_recorded_as_star_feedback_and_a_flag_of_one(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=60), dict(n=2, score=60)])
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now=set(ids), starred_now={ids[0], "unknown"})
    assert stats["stars"] == 1 and store.feedback_ids(conn, "star") == {ids[0]}
    assert store.article(conn, ids[0])["starred"] == 1 and store.article(conn, ids[1])["starred"] == 0


def test_collect_stamps_what_is_still_unread_and_not_what_went_read(conn, cfg):
    ids = seed_scored(
        conn, cfg, [dict(n=1, score=60, crawled_msec=None), dict(n=2, score=60, crawled_msec=None)]
    )
    conn.execute("UPDATE state SET last_seen_unread_at = NULL")
    feedback.collect(FakeClient(), conn, cfg, unread_now={ids[0]}, starred_now=set())
    seen = {r[0]: r[1] for r in conn.execute("SELECT article_id, last_seen_unread_at FROM state")}
    assert seen[ids[0]] and seen[ids[1]] is None


# -- reads, expiry and the bulk dismissal ----------------------------------------------


def test_reads_count_within_seven_days_to_the_millisecond(conn, cfg, monkeypatch):
    """An article crawled exactly seven days ago is a read; a millisecond older, it expired.
    Reads carry a read_at stamp; expired articles get the expired status."""
    now = 1_760_000_000
    monkeypatch.setattr("time.time", lambda: float(now))
    week = 7 * 86400_000
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=60, crawled_msec=now * 1000 - 86400_000),
            dict(n=2, score=60, crawled_msec=now * 1000 - week),
            dict(n=3, score=60, crawled_msec=now * 1000 - week - 1),
            dict(n=4, score=60, crawled_msec=now * 1000),
        ],
    )
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now={ids[3]}, starred_now=set())
    assert stats["reads"] == 2 and stats["expired"] == 1 and stats["dismissed"] == 0
    assert store.status_ids(conn, "read") == {ids[0], ids[1]} and store.status_ids(conn, "expired") == {
        ids[2]
    }
    assert store.feedback_ids(conn, "read") == {ids[0], ids[1]} and store.article(conn, ids[0])["read_at"]
    assert store.article(conn, ids[2])["read_at"] is None


def test_bulk_dismissal_needs_the_threshold_and_three_feeds(conn, cfg):
    """Four reads across four feeds are reads; the threshold's worth across three feeds is a
    mark-all-as-read, recorded as dismissals."""
    cfg.safety.mark_all_read_threshold = 5
    ids = seed_scored(
        conn, cfg, [dict(n=i, score=60, feed=f"Feed {i % 4}", crawled_msec=None) for i in range(1, 31)]
    )
    client = FakeClient()
    stats = feedback.collect(client, conn, cfg, unread_now=set(ids[4:]), starred_now=set())
    assert stats["reads"] == 4 and stats["dismissed"] == 0 and store.feedback_ids(conn, "dismiss") == set()
    gone = ids[4:9]  # feeds 1, 2, 3, 0, 1: three or more
    stats = feedback.collect(client, conn, cfg, unread_now=set(ids[9:]), starred_now=set())
    assert stats["dismissed"] == 5 and stats["reads"] == 0
    assert store.status_ids(conn, "dismissed") == set(gone) and store.feedback_ids(conn, "dismiss") == set(
        gone
    )


# -- the suspect flip --------------------------------------------------------------------


def test_flip_of_exactly_four_fifths_is_believed(conn, cfg):
    """The share must be exceeded: twenty of twenty-five gone is read, not suspected."""
    ids = seed_scored(conn, cfg, [dict(n=i, score=60, crawled_msec=None) for i in range(1, 26)])
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now=set(ids[20:]), starred_now=set())
    assert stats["reads"] == 20 and stats["suspect_flip"] == 0


def test_flip_of_exactly_twenty_articles_is_suspected(conn, cfg):
    """Twenty is the minimum that counts, not twenty-one."""
    ids = seed_scored(conn, cfg, [dict(n=i, score=60, crawled_msec=None) for i in range(1, 21)])
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now=set(), starred_now=set())
    assert (
        stats["suspect_flip"] == 20 and stats["reads"] == 0 and store.status_ids(conn, "unread") == set(ids)
    )


def test_large_flip_is_suspected_even_when_some_remain_upstream(conn, cfg):
    """A truncated answer leaves a few articles: twenty-eight of thirty gone is still suspect."""
    ids = seed_scored(conn, cfg, [dict(n=i, score=60, crawled_msec=None) for i in range(1, 31)])
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now=set(ids[28:]), starred_now=set())
    assert stats["suspect_flip"] == 28 and store.status_ids(conn, "unread") == set(ids)


def test_suspect_flip_is_confirmed_within_three_hours_and_a_bad_stamp_is_ignored(conn, cfg, monkeypatch):
    """A stamp that will not parse counts as no earlier sighting; one exactly three hours old
    confirms the flip."""
    fixed = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(store, "utcnow", lambda: fixed)
    ids = seed_scored(
        conn, cfg, [dict(n=i, score=60, feed=f"Feed {i % 5}", crawled_msec=None) for i in range(1, 41)]
    )
    store.kv_set(conn, feedback.SUSPECT_FLIP_KEY, "not a date")
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now=set(), starred_now=set())
    assert stats["suspect_flip"] == 40 and store.status_ids(conn, "unread") == set(ids)
    store.kv_set(conn, feedback.SUSPECT_FLIP_KEY, (fixed - timedelta(hours=3)).isoformat())
    stats = feedback.collect(FakeClient(), conn, cfg, unread_now=set(), starred_now=set())
    assert stats["suspect_flip"] == 0 and stats["dismissed"] == 40


# -- steers and the rescue rate -----------------------------------------------------------


def test_steer_phrase_is_the_concept_with_the_title_or_whatever_there_is(conn, cfg):
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=60, concept="coral reefs", title="t" * 80),
            dict(n=2, score=60, concept="crypto", title="x"),
            dict(n=3, score=60, concept="", title="Only a title"),
            dict(n=4, score=60, concept="", title="x"),
            dict(n=5, score=60, concept="gpus", title="x"),
        ],
    )
    conn.execute("UPDATE articles SET title = '' WHERE id = ?", (ids[1],))
    conn.execute("UPDATE articles SET title = NULL WHERE id IN (?, ?)", (ids[3], ids[4]))
    assert feedback.steer_phrase(conn, ids[0]) == 'coral reefs (e.g. "' + "t" * 70 + '")'
    assert feedback.steer_phrase(conn, ids[1]) == "crypto"
    assert feedback.steer_phrase(conn, ids[2]) == "Only a title"
    assert feedback.steer_phrase(conn, ids[3]) == ids[3]
    assert feedback.steer_phrase(conn, ids[4]) == "gpus"
    assert feedback.steer_phrase(conn, "unknown") == "unknown"


def test_more_and_less_tags_hand_the_profile_a_want_and_a_meh(conn, cfg, monkeypatch):
    from bodh import profiler

    ids = seed_scored(
        conn,
        cfg,
        [dict(n=1, score=60, concept="coral reefs", title="Fish"), dict(n=2, score=60, concept="crypto")],
    )
    handed = []
    monkeypatch.setattr(profiler, "steers", lambda cfg, conn, items: handed.extend(items))
    client = FakeClient(labels={cfg.tags.more: [ids[0]], cfg.tags.less: [ids[1]]})
    stats = feedback.collect(client, conn, cfg, unread_now=set(ids), starred_now=set())
    assert stats["steers"] == 2 and stats["verdicts"] == 2
    assert sorted(handed) == [("meh", 'crypto (e.g. "Article 2")'), ("want", 'coral reefs (e.g. "Fish")')]


def test_rescue_rate_counts_exactly_a_week_and_exactly_the_minimum(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=i, score=10) for i in range(1, 11)])
    run_id = store.begin_run(conn, "run")
    store.mark_cut(conn, ids, run_id, "autocut")
    store.restore_unread(conn, ids[:1], rescued=True)
    store.add_feedback(conn, ids[0], "rescue")
    assert feedback.rescue_rate_week(conn, min_cuts=10) == pytest.approx(0.1)  # ten cuts, ten is enough
    stale = (store.utcnow() - timedelta(days=7.5)).isoformat()
    conn.execute(f"UPDATE state SET cut_at = ? WHERE article_id IN ({','.join('?' * 5)})", (stale, *ids[1:6]))
    assert feedback.rescue_rate_week(conn, min_cuts=5) == pytest.approx(
        0.2
    )  # four standing cuts and the rescue


def test_rescue_one_reverses_a_cut_and_only_records_a_positive_otherwise(conn, cfg):
    """`bodh mark ID rescue`: a cut article is marked unread and untagged in BazQux, comes
    back unread here with a rescue stamp, and earns both a rescue and a `wrong`; an article
    that was never cut earns the `wrong` alone. A failed untag does not stop the rescue."""
    ids = seed_scored(
        conn, cfg, [dict(n=1, score=10, title="Cut one"), dict(n=2, score=60, title="Kept one")]
    )
    store.mark_cut(conn, ids[:1], store.begin_run(conn, "run"), "autocut")
    store.set_flags(conn, ids[:1], cut_tagged=1, read_marked=1)

    class NoUntag(FakeClient):
        def untag(self, ids, name):
            raise RuntimeError("down")

    client = NoUntag()
    line = feedback.rescue_one(client, conn, cfg, ids[0], store.article(conn, ids[0]))
    assert line.startswith("rescued: Cut one") and client.unread_marked == [[ids[0]]]
    row = store.article(conn, ids[0])
    assert row["status"] == "unread" and row["rescued_at"] and store.rescued_ids(conn) == {ids[0]}
    assert store.feedback_for(conn, ids[0]) and {k for k, _ in store.feedback_for(conn, ids[0])} == {
        "rescue",
        "wrong",
    }
    client = FakeClient()
    line = feedback.rescue_one(client, conn, cfg, ids[1], store.article(conn, ids[1]))
    assert line.startswith("recorded as a strong positive") and "Kept one" in line
    assert client.unread_marked == [] and client.untagged == []
    assert {k for k, _ in store.feedback_for(conn, ids[1])} == {"wrong"} and store.rescued_ids(conn) == {
        ids[0]
    }
    client = FakeClient()
    ids2 = seed_scored(conn, cfg, [dict(n=3, score=10, title="Cut two")])
    store.mark_cut(conn, ids2, store.begin_run(conn, "run"), "autocut")
    feedback.rescue_one(client, conn, cfg, ids2[0], store.article(conn, ids2[0]))
    assert client.untagged == [(cfg.tags.cut, ids2)] and client.unread_marked == [ids2]
