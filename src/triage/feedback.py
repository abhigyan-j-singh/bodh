"""Feedback, collected from BazQux state with no labelling chores: stars, inferred
reads, rescues (a cut article marked unread again, or tagged `wrong`), `meh` on a
kept article, and a `prune` tag asking for one more step."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter

from . import store
from .bazqux import BazQuxClient
from .config import Config

log = logging.getLogger(__name__)

READ_INFERENCE_MAX_AGE_DAYS = 7  # older unread articles that vanish may have been evicted, not read


def collect(client: BazQuxClient, conn: sqlite3.Connection, cfg: Config, unread_now: set[str],
            starred_now: set[str]) -> dict:
    stats = {"stars": 0, "reads": 0, "dismissed": 0, "expired": 0, "rescues": 0, "meh": 0, "prune_requested": False}
    known = store.known_ids(conn)

    # stars: explicit, the strongest signal, and a protection
    new_stars = [aid for aid in starred_now & known if store.add_feedback(conn, aid, "star")]
    store.set_flags(conn, [aid for aid in starred_now & known], starred=1)
    stats["stars"] = len(new_stars)

    # reads and dismissals: what we left unread and is now read, by the student's hand
    ours_unread = store.status_ids(conn, "unread")
    flipped = list(ours_unread - unread_now)
    if flipped:
        rows = conn.execute(
            f"SELECT id, feed_title, crawled_msec FROM articles WHERE id IN ({','.join('?' * len(flipped))})", flipped
        ).fetchall()
        now_msec = int(time.time() * 1000)
        recent = [r for r in rows if (r["crawled_msec"] or now_msec) >= now_msec - READ_INFERENCE_MAX_AGE_DAYS * 86400_000]
        expired = [r["id"] for r in rows if r not in recent]
        feeds = Counter(r["feed_title"] for r in recent)
        bulk = len(recent) >= cfg.breakers.bulk_dismiss_threshold and len(feeds) >= 3
        kind = "dismiss" if bulk else "read"
        for r in recent:
            store.add_feedback(conn, r["id"], kind)
        store.set_flags(conn, [r["id"] for r in recent], status=("dismissed" if bulk else "read"),
                        read_at=store.utcnow_iso())
        store.set_flags(conn, expired, status="expired")
        stats["dismissed" if bulk else "reads"] = len(recent)
        stats["expired"] = len(expired)

    # rescues: a cut article that is unread again was marked unread by the student
    ours_cut = {r[0] for r in conn.execute("SELECT article_id FROM state WHERE status = 'cut' AND read_marked = 1")}
    rescued = list(ours_cut & unread_now)
    wrong_ids = [aid for aid in client.label_ids(cfg.tags.wrong) if aid in known]
    to_unread = [aid for aid in wrong_ids if aid not in unread_now]
    if to_unread:
        client.mark_unread(to_unread)
    rescued = list(set(rescued) | set(wrong_ids))
    if rescued:
        client.untag(rescued, cfg.tags.cut)
        store.restore_unread(conn, rescued, rescued=True)
        for aid in rescued:
            store.add_feedback(conn, aid, "rescue")
        for aid in wrong_ids:
            store.add_feedback(conn, aid, "wrong")
    if wrong_ids:
        client.untag(wrong_ids, cfg.tags.wrong)
    stats["rescues"] = len(rescued)

    # meh: a kept article that was not worth it
    meh_ids = [aid for aid in client.label_ids(cfg.tags.meh) if aid in known]
    for aid in meh_ids:
        store.add_feedback(conn, aid, "meh")
    if meh_ids:
        client.untag(meh_ids, cfg.tags.meh)
    stats["meh"] = len(meh_ids)

    # prune: any article tagged `prune` asks for one more step
    prune_ids = client.label_ids(cfg.tags.prune)
    if prune_ids:
        client.untag(prune_ids, cfg.tags.prune)
        stats["prune_requested"] = True

    # everything still unread was seen unread now
    store.set_flags(conn, list(ours_unread & unread_now), last_seen_unread_at=store.utcnow_iso())
    return stats


def rescue_rate_week(conn: sqlite3.Connection) -> float:
    since = store.days_ago_iso(7)
    cuts = conn.execute(
        "SELECT COUNT(*) FROM state WHERE cut_at >= ? OR rescued_at >= ?", (since, since)
    ).fetchone()[0]
    rescues = conn.execute("SELECT COUNT(*) FROM feedback WHERE kind = 'rescue' AND at >= ?", (since,)).fetchone()[0]
    return (rescues / cuts) if cuts else 0.0
