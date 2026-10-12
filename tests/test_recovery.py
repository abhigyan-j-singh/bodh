"""Backups and the rebuild from BazQux's tags."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import seed_scored
from fakes import FakeBazQuxServer, FakeLLM, make_item

import bodh.readers.bazqux as bq
from bodh import recovery, run, store
from bodh.config import Secrets
from bodh.ids import short_to_long


def test_backup_writes_a_dated_copy_and_keeps_seven(cfg, conn, monkeypatch):
    seed_scored(conn, cfg, [dict(n=1, score=90)])
    path = recovery.backup(conn, cfg)
    assert path.name.startswith("bodh-") and path.name.endswith(".db") and path.parent.name == "backups"
    copy = sqlite3.connect(str(path))
    try:
        assert copy.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        copy.close()
    assert recovery.backup(conn, cfg) == path  # the same day: replaced, not doubled
    assert len(recovery.backups(cfg)) == 1
    for day in range(1, 10):
        (path.parent / f"bodh-2026-01-{day:02d}.db").write_bytes(b"old")
    recovery.backup(conn, cfg)
    kept = recovery.backups(cfg)
    assert len(kept) == 7 and kept[-1] == path and kept[0].name == "bodh-2026-01-04.db"
    assert len(recovery.backups(cfg)) == 7


def test_backup_daily_once_and_fails_open(cfg, conn, caplog):
    assert recovery.backup_daily(conn, cfg) is not None
    assert recovery.backup_daily(conn, cfg) is None  # today's exists
    for p in recovery.backups(cfg):
        p.unlink()
    (cfg.root / "data" / "backups").rmdir()
    (cfg.root / "data" / "backups").write_text("not a folder", encoding="utf-8")  # the folder cannot be made
    assert recovery.backup_daily(conn, cfg) is None and "could not write" in caplog.text


def test_the_run_backs_up_once_a_day(cfg, conn):
    from fakes import FakeReader

    cfg.models.batch = False
    item = make_item(1, title="plain", crawled_msec=1_800_000_000_000)
    ctx = run.Context(cfg, Secrets(), conn)
    ctx._client = FakeReader([item])
    ctx._llm = FakeLLM(cfg, conn)
    run.run(ctx, enforce=False)
    assert len(recovery.backups(cfg)) == 1
    stamp = recovery.backups(cfg)[0].stat().st_mtime_ns
    run.run(ctx, enforce=False)
    assert recovery.backups(cfg)[0].stat().st_mtime_ns == stamp  # not rewritten the same day


def _world(cfg, conn, monkeypatch):
    monkeypatch.setattr(bq, "MIN_REQUEST_INTERVAL", 0.0)
    cfg.models.batch = False
    items = [
        make_item(
            n, feed="Feed A" if n % 2 else "Feed B", title=f"Article {n}", crawled_msec=1_800_000_000_000 + n
        )
        for n in range(1, 8)
    ]
    ids = [i.id for i in items]
    # BazQux remembers: 1 and 2 scored high (unread), 3 scored low and cut (read), 4 cut without a
    # band (read), 5 starred and tagged, 6 tagged but read (the student read it), 7 untouched
    server = FakeBazQuxServer(
        items,
        unread=[ids[0], ids[1], ids[4], ids[6]],
        starred=[ids[4]],
        labels={
            "score-90": [ids[0], ids[4]],
            "score-70": [ids[1], ids[5]],
            "score-0": [ids[2]],
            "cut": [ids[2], ids[3]],
            cfg.protect.must_read_stream: [],
        },
        folders={"Feed A": "Essays"},
    )
    ctx = run.Context(cfg, Secrets(), conn)
    ctx._client = server.client()
    ctx._llm = FakeLLM(cfg, conn)
    return ctx, server, ids


def test_rebuild_from_band_tags_against_the_fake_server(cfg, conn, monkeypatch):
    ctx, server, ids = _world(cfg, conn, monkeypatch)
    dry = recovery.rebuild(ctx, dry_run=True)
    assert (
        dry["tagged"] == 5
        and dry["cut_tagged"] == 2
        and dry["unknown"] == 6
        and dry["scores"] == 5
        and dry["dry_run"]
    )
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[
        0
    ] == 0 and "would write 5 scores" in recovery.summary(dry)
    stats = recovery.rebuild(ctx)
    assert (
        stats["fetched"] == 6
        and stats["vanished"] == 0
        and stats["scores"] == 5
        and stats["cuts"] == 2
        and stats["stars"] == 1
    )
    one = store.article(conn, ids[0])
    assert (
        one["score"] == 90.0
        and one["bucket"] == "score-90"
        and one["model"] == "rebuilt"
        and one["confidence"] == "low"
    )
    assert (
        one["title"] == "Article 1"
        and one["folder"] == "Essays"
        and one["status"] == "unread"
        and one["text"]
    )
    assert store.article(conn, ids[2])["status"] == "cut" and store.article(conn, ids[2])["score"] == 0.0
    assert store.article(conn, ids[3])["status"] == "cut" and store.article(conn, ids[3])["score"] is None
    assert store.article(conn, ids[4])["starred"] == 1 and store.article(conn, ids[5])["status"] == "unread"
    assert store.article(conn, ids[6]) is None  # untouched in BazQux: the next run ingests and judges it
    assert store.untagged_bands(conn) == {}  # the tags are already there
    assert store.cut_pending(conn) == ([], [])  # and so are the cut tags and read marks
    assert (
        store.last_runs(conn, 1)[0]["kind"] == "rebuild"
        and "wrote 5 scores" in store.last_runs(conn, 1)[0]["summary"]
    )
    # nothing BazQux shows was changed
    assert server.edits == []
    # a second rebuild writes nothing more
    again = recovery.rebuild(ctx)
    assert again["scores"] == 0 and again["cuts"] == 0 and again["fetched"] == 0 and again["stars"] == 1
    assert stats["rejudge"] == 3 and "reads 3 unread of them again" in recovery.summary(stats)
    # the next run judges the untouched article and reads the three unread rebuilt keepers again
    # (catch_up.rejudge_at: 50); the read and cut ones are never judged again, and autocut leaves
    # the low-confidence rebuilt scores alone
    cfg.autocut.new_ground_share = 0.0
    res = run.run(ctx, enforce=True)
    assert res.new == 1 and res.scored == 4 and res.cut_autocut == 0
    assert (
        store.article(conn, ids[6])["score"] == 55.0
        and store.article(conn, ids[1])["model"] == cfg.models.judge
    )
    assert (
        store.article(conn, ids[2])["model"] == "rebuilt"
        and store.article(conn, ids[5])["model"] == "rebuilt"
    )
    assert store.article(conn, ids[0])["bucket"] == "score-50" and ("score-90", [ids[0], ids[4]]) in [
        (n, sorted(i)) for n, i in server_untags(server)
    ]
    # with the rule off, nothing rebuilt is read again
    cfg.catch_up.rejudge_at = None
    assert run.run(ctx, enforce=True).scored == 0


def server_untags(server):
    """(label, ids) per untag the server saw."""
    return [
        (remove.removeprefix("user/-/label/"), ids)
        for ids, _add, remove in server.edits
        if remove and remove.startswith("user/-/label/")
    ]


def test_rebuild_refuses_when_a_stream_cannot_be_read(cfg, conn, monkeypatch):
    from bodh.errors import ReaderError

    ctx, server, ids = _world(cfg, conn, monkeypatch)
    monkeypatch.setattr(bq.time, "sleep", lambda _s: None)
    server.fail_next["/reader/api/0/stream/items/ids"] = [500] * bq.MAX_RETRIES
    with pytest.raises(ReaderError):
        recovery.rebuild(ctx)
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0  # nothing half written


def test_backup_and_rebuild_commands(cfg, conn, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from bodh.cli import app

    ctx, server, ids = _world(cfg, conn, monkeypatch)
    conn.close()
    (cfg.root / "config.yaml").write_text("timezone: UTC\n", encoding="utf-8")
    monkeypatch.setenv("BODH_HOME", str(cfg.root))
    monkeypatch.setattr(run.Context, "client", property(lambda self: ctx._client))
    runner = CliRunner()
    result = runner.invoke(app, ["rebuild", "--dry-run"])
    assert result.exit_code == 0 and "would write 5 scores" in result.output, result.output
    result = runner.invoke(app, ["rebuild"], input="n\n")
    assert result.exit_code == 0 and "wrote" not in result.output
    result = runner.invoke(app, ["rebuild", "--yes"])
    assert result.exit_code == 0 and "wrote 5 scores, 2 cuts, 1 stars" in result.output, result.output
    result = runner.invoke(app, ["backup", "--keep", "2"])
    assert result.exit_code == 0 and "backup written" in result.output and "(1 kept)" in result.output
    assert len(recovery.backups(cfg)) == 1
    result = runner.invoke(app, ["doctor"])
    assert "backups: 1, newest bodh-" in result.output
    assert short_to_long(1) == ids[0]
