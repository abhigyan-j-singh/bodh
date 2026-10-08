"""Feedback, collected from BazQux state and from the student's tags. With no labelling
chores: stars, inferred reads, rescues (a cut article marked unread again, or tagged
`wrong`), and a `prune` tag asking for one more step. With a tag, the student's verdict:
a grade (`great`, `good`, `meh`, `bad`), the level missed (`basic`, `heavy`), or a steer
(`more`, `less`) that the teacher sees from the next run. Verdict tags stay on the article
as the student's own record; each is recorded once."""

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
    stats = {"stars": 0, "reads": 0, "dismissed": 0, "expired": 0, "rescues": 0, "verdicts": 0, "steers": 0,
             "prune_requested": False}
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

    # verdicts: grades, levels and steers, from the tags left on the articles
    v = collect_verdicts(client, conn, cfg, known)
    stats.update(v)

    # prune: any article tagged `prune` asks for one more step
    prune_ids = client.label_ids(cfg.tags.prune)
    if prune_ids:
        client.untag(prune_ids, cfg.tags.prune)
        stats["prune_requested"] = True

    # everything still unread was seen unread now
    store.set_flags(conn, list(ours_unread & unread_now), last_seen_unread_at=store.utcnow_iso())
    return stats


def collect_verdicts(client: BazQuxClient, conn: sqlite3.Connection, cfg: Config, known: set[str]) -> dict:
    """The student's tags, read as verdicts. A tag stays on the article (the student's record)
    and is recorded once. Within a group one verdict holds per article: a different tag added
    later replaces it (when both are present, the later one in the group's order wins, so
    `bad` beats `good`); removing a tag alone retracts nothing. `more` and `less` also write a
    dated nudge, with the article's concept, into the learner model, which the teacher reads
    from the next run."""
    stats = {"verdicts": 0, "steers": 0}
    tags = cfg.tags.verdict_tags()
    present: dict[str, set[str]] = {}
    for kind, name in tags.items():
        for aid in client.label_ids(name):
            if aid in known:
                present.setdefault(aid, set()).add(kind)
    if not present:
        return stats
    nudges: list[tuple[str, str]] = []
    for group in (store.GRADES, store.LEVELS, store.STEERS):
        recorded = store.verdicts(conn, group)
        for aid, kinds in present.items():
            here = [k for k in group if k in kinds]
            if not here or recorded.get(aid) == here[-1]:
                continue
            kind = here[-1]
            if store.set_verdict(conn, aid, kind, group):
                stats["verdicts"] += 1
                if group is store.STEERS:
                    nudges.append((kind, _steer_phrase(conn, aid)))
    if nudges:
        from . import learner  # here, not at the top: learner imports the teacher

        learner.nudges(cfg, conn, [("want" if kind == "more" else "meh", phrase) for kind, phrase in nudges])
        stats["steers"] = len(nudges)
    return stats


def _steer_phrase(conn: sqlite3.Connection, article_id: str) -> str:
    row = conn.execute(
        "SELECT a.title, s.concept FROM articles a LEFT JOIN scores s ON s.article_id = a.id WHERE a.id = ?",
        (article_id,),
    ).fetchone()
    if row is None:
        return article_id
    concept = (row["concept"] or "").strip()
    title = (row["title"] or "").strip()[:70]
    if concept and title:
        return f'{concept} (e.g. "{title}")'
    return concept or title or article_id


def rescue_rate_week(conn: sqlite3.Connection) -> float:
    since = store.days_ago_iso(7)
    cuts = conn.execute(
        "SELECT COUNT(*) FROM state WHERE cut_at >= ? OR rescued_at >= ?", (since, since)
    ).fetchone()[0]
    rescues = conn.execute("SELECT COUNT(*) FROM feedback WHERE kind = 'rescue' AND at >= ?", (since,)).fetchone()[0]
    return (rescues / cuts) if cuts else 0.0
