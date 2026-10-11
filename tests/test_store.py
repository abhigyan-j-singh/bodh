"""The SQLite store: migrations, bulk writes, the little helpers."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from bodh import store


def test_schema_version_and_migrations_apply_in_order(tmp_path):
    """A database from before migrations existed (no exempt column, no version key) is
    brought to the current shape on connect; a current one is left alone."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    v1 = store.SCHEMA.replace("  exempt INTEGER DEFAULT 0,\n", "").replace(
        "  judge_failures INTEGER DEFAULT 0,\n", ""
    )
    old.executescript(v1)
    old.execute("INSERT INTO state (article_id, status) VALUES ('a', 'unread')")
    # the kv keys before October 2026, as the old code left them
    old.executemany(
        "INSERT INTO kv (key, value) VALUES (?, ?)",
        [("phase", "judging"), ("plain_json", "1"), ("cache_ttl_fallback", "1"), ("breaker_tripped", "42")],
    )
    old.commit()
    assert {"exempt", "judge_failures"}.isdisjoint(store._columns(old, "state"))
    old.close()
    conn = store.connect(path)
    assert store.schema_version(conn) == store.SCHEMA_VERSION
    assert tuple(
        conn.execute("SELECT exempt, judge_failures FROM state WHERE article_id = 'a'").fetchone()
    ) == (0, 0)
    # migration 4: the kv keys carry the glossary's names; the old safety key stays for a rollback
    assert store.kv_get(conn, store.PHASE_KEY) == "judging" and store.kv_get(conn, "phase") is None
    assert store.kv_get(conn, store.PLAIN_JSON_KEY) == "1" and store.kv_get(conn, "plain_json") is None
    assert (
        store.kv_get(conn, store.CACHE_TTL_KEY) == "5m" and store.kv_get(conn, "cache_ttl_fallback") is None
    )
    assert (
        store.kv_get(conn, store.SAFETY_KEY) == "42" and store.kv_get(conn, store.LEGACY_SAFETY_KEY) == "42"
    )
    assert store.migrate(conn) == store.SCHEMA_VERSION  # idempotent
    conn.close()
    fresh = store.connect(tmp_path / "fresh.db")
    assert store.schema_version(fresh) == store.SCHEMA_VERSION
    fresh.close()


def test_kv_delete(conn):
    store.kv_set(conn, "k", "v")
    assert store.kv_delete(conn, "k") and store.kv_get(conn, "k") is None and not store.kv_delete(conn, "k")


def test_bulk_writes_scale(conn):
    """20,000 set_flags rows took 3.5 s in autocommit (one fsync per row); one transaction
    takes a few hundredths."""
    import time

    from conftest import make_item

    ids = [make_item(n).id for n in range(1, 20_001)]
    now = store.utcnow_iso()
    with store.transaction(conn):
        conn.executemany(
            "INSERT INTO articles (id, title, fetched_at) VALUES (?, 'a', ?)", [(i, now) for i in ids]
        )
        conn.executemany("INSERT INTO state (article_id, status) VALUES (?, 'unread')", [(i,) for i in ids])
    t0 = time.monotonic()
    store.set_flags(conn, ids, protected=1)
    store.mark_cut(conn, ids, 1, "floor")
    took = time.monotonic() - t0
    assert took < 1.0, took
    assert (
        conn.execute("SELECT COUNT(*) FROM state WHERE status = 'cut' AND protected = 1").fetchone()[0]
        == 20_000
    )
    # a bulk write inside a caller's transaction joins it and rolls back with it
    try:
        with store.transaction(conn):
            store.set_flags(conn, ids[:10], protected=0)
            raise RuntimeError("abort")
    except RuntimeError:
        pass
    assert conn.execute("SELECT COUNT(*) FROM state WHERE protected = 0").fetchone()[0] == 0


def test_batch_members_keep_the_request_numbering_when_an_article_moves_on(conn):
    """An article resubmitted in a later batch (the first was given up on, say) is moved to the
    new batch by the ON CONFLICT update; the old batch's request keeps a gap at its position so
    the model's numbering still lines up with the remaining articles."""
    store.add_batch(conn, "b1", None, "teacher", "m", [("r1", ["a", "b", "c"])])
    store.add_batch(conn, "b2", None, "teacher", "m", [("r9", ["b"])])
    assert store.batch_members(conn, "b1") == {"r1": ["a", "", "c"]}
    assert store.batch_members(conn, "b2") == {"r9": ["b"]}


def test_text_less_articles_are_refetched_for_a_few_hours_only(conn):
    """A feed that gives no body text used to have its articles fetched again every run for two
    weeks; now for TEXT_RETRY_HOURS after they were first seen."""
    conn.execute(
        "INSERT INTO articles (id, title, text, fetched_at) VALUES ('fresh', 't', '', ?), ('old', 't', '', ?), ('full', 't', 'x', ?)",
        (
            store.utcnow_iso(),
            (store.utcnow() - timedelta(hours=store.TEXT_RETRY_HOURS + 1)).isoformat(),
            store.utcnow_iso(),
        ),
    )
    conn.executemany(
        "INSERT INTO state (article_id, status) VALUES (?, 'unread')", [("fresh",), ("old",), ("full",)]
    )
    assert store.unread_without_text(conn) == ["fresh"]
    assert set(store.unread_without_text(conn, within_hours=10_000)) == {"fresh", "old"}
