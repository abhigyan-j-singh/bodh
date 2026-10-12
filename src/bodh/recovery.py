"""Backups and the rebuild: the two ways back from a lost or broken database.

A backup is `VACUUM INTO` a dated file under data/backups/, seven kept; the run makes one a
day. A rebuild reads the band tags, the cut tag and the stars back from BazQux into an empty
(or thinner) database, so nothing already judged is judged and paid for again, and nothing
already cut is cut again. A rebuilt score carries the band's threshold as every sub-score,
`model = "rebuilt"` and a low confidence, so autocut leaves it alone; `bodh prune` counts it
as any other. The unread ones at or above `catch_up.rejudge_at` are read again by the judge
on the next runs, as a quick judge's would be, so the keepers rest on real judgments; the
read and cut ones are never judged again."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import store
from .config import Config

log = logging.getLogger(__name__)

BACKUPS_DIR = "backups"
BACKUPS_KEPT = 7
REBUILT_MODEL = "rebuilt"


def backups_dir(cfg: Config) -> Path:
    return cfg.root / cfg.data_dir / BACKUPS_DIR


def backups(cfg: Config) -> list[Path]:
    """The backups on disk, oldest first (the date is in the name)."""
    folder = backups_dir(cfg)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("bodh-*.db") if p.is_file())


def backup(conn: sqlite3.Connection, cfg: Config, keep: int = BACKUPS_KEPT) -> Path:
    """Copy the database with `VACUUM INTO` to data/backups/bodh-<date>.db (today's file is
    replaced), then keep the newest `keep`. Returns the file written."""
    folder = backups_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    today = datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d")
    target = folder / f"bodh-{today}.db"
    tmp = folder / f".bodh-{today}.db.tmp"
    if tmp.exists():
        tmp.unlink()
    conn.execute("VACUUM INTO ?", (str(tmp),))
    tmp.replace(target)
    for old in backups(cfg)[: -max(1, keep)] if keep else []:
        old.unlink(missing_ok=True)
    return target


def backup_daily(conn: sqlite3.Connection, cfg: Config) -> Path | None:
    """The run's daily backup: made once a day, skipped when today's exists; never raises."""
    try:
        today = datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d")
        if (backups_dir(cfg) / f"bodh-{today}.db").exists():
            return None
        return backup(conn, cfg)
    except (sqlite3.Error, OSError) as exc:  # a full or read-only disk: the run goes on, the log says
        log.warning("backup: could not write today's backup (%s)", exc)
        return None


def rebuild(ctx: Any, dry_run: bool = False) -> dict[str, Any]:
    """Read the band tags, the cut tag and the stars back from BazQux. Articles the database
    does not know are fetched (title, feed, folder, text); a band tag becomes a rebuilt score,
    the cut tag on a read article becomes a cut, a star sets the flag. Nothing BazQux shows
    is changed. With `dry_run`, only the counts."""
    cfg, conn, client = ctx.cfg, ctx.conn, ctx.client
    found: dict[str, tuple[int, str]] = {}  # article id -> (threshold, band): the highest band wins
    for threshold, name in sorted(cfg.tags.bands.items(), reverse=True):
        for aid in client.label_ids(name, strict=True):
            found.setdefault(aid, (threshold, name))
    cut_ids = set(client.label_ids(cfg.tags.cut, strict=True))
    unread = {r.id for r in client.unread_ids()}
    starred = set(client.starred_ids())
    known = {r[0] for r in conn.execute("SELECT id FROM articles")}
    missing = sorted((set(found) | cut_ids | starred) - known)
    stats: dict[str, Any] = {
        "tagged": len(found),
        "cut_tagged": len(cut_ids),
        "starred": len(starred),
        "unknown": len(missing),
        "fetched": 0,
        "vanished": 0,
        "scores": 0,
        "cuts": 0,
        "stars": 0,
        "rejudge": 0,
        "dry_run": dry_run,
    }
    scored = store.scored_ids(conn)
    stats["rejudge"] = sum(
        1
        for aid, (threshold, _name) in found.items()
        if aid in unread
        and aid not in scored
        and cfg.catch_up.rejudge_at is not None
        and threshold >= cfg.catch_up.rejudge_at
    )
    if dry_run:
        stats["scores"] = sum(1 for aid in found if aid not in scored)
        return stats
    _write_rebuild(ctx, found, cut_ids, unread, starred, missing, stats)
    log.info("rebuild: %s", summary(stats))
    return stats


def _rebuilt_verdict(threshold: int) -> dict[str, Any]:
    return {
        "quality": threshold,
        "learning": threshold,
        "fit": threshold,
        "breadth": threshold,
        "relevance": threshold,
        "lesson": "(rebuilt from the band tag in BazQux)",
        "concept": "",
        "kind": "other",
        "flags": [],
        "dupe_of": None,
        "confidence": "low",
    }


def summary(stats: dict[str, Any]) -> str:
    head = "would write" if stats["dry_run"] else "wrote"
    again = (
        f"; the judge reads {stats['rejudge']} unread of them again on the next runs (catch_up.rejudge_at)"
        if stats["rejudge"]
        else ""
    )
    return (
        f"{stats['tagged']} band-tagged, {stats['cut_tagged']} cut-tagged and {stats['starred']} starred in BazQux; "
        f"{stats['unknown']} unknown here ({stats['fetched']} fetched, {stats['vanished']} vanished); "
        f"{head} {stats['scores']} scores, {stats['cuts']} cuts, {stats['stars']} stars{again}"
    )


def _write_rebuild(
    ctx: Any,
    found: dict[str, tuple[int, str]],
    cut_ids: set[str],
    unread: set[str],
    starred: set[str],
    missing: list[str],
    stats: dict[str, Any],
) -> None:
    """The write: the unknown articles fetched, then in one transaction the rebuilt scores,
    the cuts and the stars, and the run row."""
    cfg, conn, client = ctx.cfg, ctx.conn, ctx.client
    items: list[Any] = []
    folders: dict[str, str | None] = {}
    if missing:
        from .run import folder_map

        folders = folder_map(client)
        items, vanished = client.contents(missing)
        stats["fetched"], stats["vanished"] = len(items), len(vanished)
    with store.transaction(conn):
        run_id = store.begin_run(conn, "rebuild")
        for item in items:
            store.upsert_article(conn, item, folders.get(item.feed_stream_id))
        known = {r[0] for r in conn.execute("SELECT id FROM articles")}
        scored = store.scored_ids(conn)
        rebuilt = [aid for aid in found if aid in known and aid not in scored]
        for aid in rebuilt:
            threshold, name = found[aid]
            store.record_score(
                conn,
                aid,
                _rebuilt_verdict(threshold),
                float(threshold),
                float(threshold),
                name,
                cfg.judge.prompt_version,
                REBUILT_MODEL,
            )
        store.mark_band_tagged(conn, rebuilt)
        already_cut = store.status_ids(conn, "cut")
        cuts = [
            aid for aid in sorted(cut_ids) if aid in known and aid not in unread and aid not in already_cut
        ]
        store.mark_cut(conn, cuts, run_id, "rebuild")
        store.set_flags(conn, cuts, cut_tagged=1, read_marked=1)
        stars = [aid for aid in sorted(starred) if aid in known]
        store.set_flags(conn, stars, starred=1)
        stats.update(scores=len(rebuilt), cuts=len(cuts), stars=len(stars))
        store.finish_run(conn, run_id, True, summary=summary(stats))
