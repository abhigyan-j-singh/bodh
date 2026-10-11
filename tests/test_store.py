"""The SQLite store: migrations, bulk writes, the little helpers."""

from __future__ import annotations

import sqlite3

from triage import store


def test_schema_version_and_migrations_apply_in_order(tmp_path):
    """A database from before migrations existed (no exempt column, no version key) is
    brought to the current shape on connect; a current one is left alone."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.executescript(store.SCHEMA.replace("  exempt INTEGER DEFAULT 0,\n", ""))
    old.execute("INSERT INTO state (article_id, status) VALUES ('a', 'unread')")
    old.commit()
    assert "exempt" not in store._columns(old, "state")
    old.close()
    conn = store.connect(path)
    assert store.schema_version(conn) == store.SCHEMA_VERSION
    assert conn.execute("SELECT exempt FROM state WHERE article_id = 'a'").fetchone()[0] == 0
    assert store.migrate(conn) == store.SCHEMA_VERSION   # idempotent
    conn.close()
    fresh = store.connect(tmp_path / "fresh.db")
    assert store.schema_version(fresh) == store.SCHEMA_VERSION
    fresh.close()


def test_kv_delete(conn):
    store.kv_set(conn, "k", "v")
    assert store.kv_delete(conn, "k") and store.kv_get(conn, "k") is None and not store.kv_delete(conn, "k")
