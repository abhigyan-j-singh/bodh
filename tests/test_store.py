"""The SQLite store: migrations, bulk writes, the little helpers."""

from __future__ import annotations

import sqlite3

from viveka import store


def test_schema_version_and_migrations_apply_in_order(tmp_path):
    """A database from before migrations existed (no exempt column, no version key) is
    brought to the current shape on connect; a current one is left alone."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    v1 = store.SCHEMA.replace("  exempt INTEGER DEFAULT 0,\n", "").replace("  judge_failures INTEGER DEFAULT 0,\n", "")
    old.executescript(v1)
    old.execute("INSERT INTO state (article_id, status) VALUES ('a', 'unread')")
    old.commit()
    assert {"exempt", "judge_failures"}.isdisjoint(store._columns(old, "state"))
    old.close()
    conn = store.connect(path)
    assert store.schema_version(conn) == store.SCHEMA_VERSION
    assert tuple(conn.execute("SELECT exempt, judge_failures FROM state WHERE article_id = 'a'").fetchone()) == (0, 0)
    assert store.migrate(conn) == store.SCHEMA_VERSION   # idempotent
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
        conn.executemany("INSERT INTO articles (id, title, fetched_at) VALUES (?, 'a', ?)", [(i, now) for i in ids])
        conn.executemany("INSERT INTO state (article_id, status) VALUES (?, 'unread')", [(i,) for i in ids])
    t0 = time.monotonic()
    store.set_flags(conn, ids, protected=1)
    store.mark_cut(conn, ids, 1, "floor")
    took = time.monotonic() - t0
    assert took < 1.0, took
    assert conn.execute("SELECT COUNT(*) FROM state WHERE status = 'cut' AND protected = 1").fetchone()[0] == 20_000
    # a bulk write inside a caller's transaction joins it and rolls back with it
    try:
        with store.transaction(conn):
            store.set_flags(conn, ids[:10], protected=0)
            raise RuntimeError("abort")
    except RuntimeError:
        pass
    assert conn.execute("SELECT COUNT(*) FROM state WHERE protected = 0").fetchone()[0] == 0
