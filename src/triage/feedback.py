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
from datetime import datetime, timedelta

from . import store
from .bazqux import BazQuxClient
from .config import Config

log = logging.getLogger(__name__)

READ_INFERENCE_MAX_AGE_DAYS = 7  # older unread articles that vanish may have been evicted, not read
SUSPECT_FLIP_SHARE = 0.8         # more of our unread list gone at once than this looks like a bad answer...
SUSPECT_FLIP_MIN = 20            # ...when at least this many articles are involved
SUSPECT_FLIP_CONFIRM_HOURS = 3   # ...unless the previous run, within this long, saw the same thing
SUSPECT_FLIP_KEY = "suspect_flip_at"
PRUNE_REQUEST_KEY = "prune_requested"
MIN_CUTS_FOR_RESCUE_RATE = 20    # the weekly rescue rate means nothing on a handful of cuts


def collect(client: BazQuxClient, conn: sqlite3.Connection, cfg: Config, unread_now: set[str],
            starred_now: set[str]) -> dict:
    stats = {"stars": 0, "reads": 0, "dismissed": 0, "expired": 0, "returned": 0, "suspect_flip": 0, "rescues": 0,
             "verdicts": 0, "steers": 0, "prune_requested": False}
    known = store.known_ids(conn)

    # stars: explicit, the strongest signal, and a protection
    new_stars = [aid for aid in starred_now & known if store.add_feedback(conn, aid, "star")]
    store.set_flags(conn, [aid for aid in starred_now & known], starred=1)
    stats["stars"] = len(new_stars)

    # back to unread: what we recorded as read, dismissed or expired that the student marked
    # unread again belongs to the pool once more (no feedback: nothing is known about why). One
    # the student had read (a `read` row) and kept to finish later is exempt from the automatic
    # floor, which would only cut it for its old score; a prune may still take it. A reversed
    # dismissal or expiry (BazQux's answer was wrong for an hour) earns no exemption: nothing
    # about those articles changed.
    returned: set[str] = set()
    for status in ("read", "dismissed", "expired"):
        returned |= store.status_ids(conn, status) & unread_now
    if returned:
        kept = returned & store.feedback_ids(conn, "read")
        store.restore_unread(conn, kept, rescued=False, exempt=True)
        store.restore_unread(conn, returned - kept, rescued=False)
        stats["returned"] = len(returned)

    # reads and dismissals: what we left unread and is now read, by the student's hand
    ours_unread = store.status_ids(conn, "unread")
    flipped = ours_unread - unread_now
    if _suspect_flip(conn, ours_unread, flipped, unread_now):
        stats["suspect_flip"] = len(flipped)
        flipped = set()
    if flipped:
        rows = store.fetch_in(conn, "SELECT id, feed_title, crawled_msec FROM articles WHERE id IN ({marks})", flipped)
        now_msec = int(time.time() * 1000)
        recent = [r for r in rows if (r["crawled_msec"] or now_msec) >= now_msec - READ_INFERENCE_MAX_AGE_DAYS * 86400_000]
        recent_ids = {r["id"] for r in recent}
        expired = [r["id"] for r in rows if r["id"] not in recent_ids]
        feeds = Counter(r["feed_title"] for r in recent)
        bulk = len(recent) >= cfg.breakers.bulk_dismiss_threshold and len(feeds) >= 3
        kind = "dismiss" if bulk else "read"
        for r in recent:
            store.add_feedback(conn, r["id"], kind)
        store.set_flags(conn, recent_ids, status=("dismissed" if bulk else "read"), read_at=store.utcnow_iso())
        store.set_flags(conn, expired, status="expired")
        stats["dismissed" if bulk else "reads"] = len(recent)
        stats["expired"] = len(expired)

    # rescues: a cut article that is unread again was marked unread by the student; a `wrong`
    # tag on a cut article asks for the same, whether or not the cut has reached BazQux yet (a
    # cut whose read mark failed is still unread there, carrying the tag). `wrong` on anything
    # else is a strong positive with no rescue in it (there was no cut to reverse).
    ours_cut_marked = {r[0] for r in conn.execute("SELECT article_id FROM state WHERE status = 'cut' AND read_marked = 1")}
    ours_cut = store.status_ids(conn, "cut")
    wrong_ids = [aid for aid in client.label_ids(cfg.tags.wrong) if aid in known]
    wrong_cut = [aid for aid in wrong_ids if aid in ours_cut]
    to_unread = [aid for aid in wrong_cut if aid not in unread_now]
    if to_unread:
        client.mark_unread(to_unread)
    rescued = sorted((ours_cut_marked & unread_now) | set(wrong_cut))
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

    # prune: any article tagged `prune` asks for one more step. The request is kept until an
    # enforcing run applies it: shadow mode or a failed run must not drop it on the floor.
    prune_ids = client.label_ids(cfg.tags.prune)
    if prune_ids:
        client.untag(prune_ids, cfg.tags.prune)
        store.kv_set(conn, PRUNE_REQUEST_KEY, store.utcnow_iso())
    stats["prune_requested"] = store.kv_get(conn, PRUNE_REQUEST_KEY) is not None

    # everything still unread was seen unread now
    store.set_flags(conn, list(ours_unread & unread_now), last_seen_unread_at=store.utcnow_iso())
    return stats


def _suspect_flip(conn: sqlite3.Connection, ours_unread: set[str], flipped: set[str], unread_now: set[str]) -> bool:
    """An empty or truncated unread list from BazQux would flip our whole pool to read (or to a
    dismissal, which teaches the profile the pool is disliked). A flip that large is not
    believed the first time; the same picture from the next run within a few hours is."""
    large = not unread_now or len(flipped) > SUSPECT_FLIP_SHARE * len(ours_unread)
    if not large or len(flipped) < SUSPECT_FLIP_MIN:
        store.kv_delete(conn, SUSPECT_FLIP_KEY)
        return False
    seen = store.kv_get(conn, SUSPECT_FLIP_KEY)
    if seen:
        try:
            age = store.utcnow() - datetime.fromisoformat(seen)
        except ValueError:
            age = timedelta.max
        if age <= timedelta(hours=SUSPECT_FLIP_CONFIRM_HOURS):
            log.warning("feedback: %d of %d unread articles gone upstream, as on the previous run too: "
                        "recording the reads", len(flipped), len(ours_unread))
            store.kv_delete(conn, SUSPECT_FLIP_KEY)
            return False
    log.warning("feedback: %d of %d unread articles are gone upstream at once; not believed until the "
                "next run sees the same (an empty or truncated answer from BazQux looks like this)",
                len(flipped), len(ours_unread))
    store.kv_set(conn, SUSPECT_FLIP_KEY, store.utcnow_iso())
    return True


def prune_request_done(conn: sqlite3.Connection) -> None:
    store.kv_delete(conn, PRUNE_REQUEST_KEY)


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


def rescue_rate_week(conn: sqlite3.Connection, min_cuts: int = MIN_CUTS_FOR_RESCUE_RATE) -> float:
    """Rescues as a share of the week's floor and prune cuts (the cuts still standing plus the
    rescued ones). Bankruptcy cuts and undone runs do not count: the student's own sweep and
    reversal say nothing about the floor; nor does a week with fewer than ``min_cuts`` cuts."""
    since = store.days_ago_iso(7)
    cuts = conn.execute(
        "SELECT COUNT(*) FROM state WHERE (cut_at >= ? AND cut_mode IN ('floor', 'prune')) OR rescued_at >= ?",
        (since, since),
    ).fetchone()[0]
    rescues = conn.execute("SELECT COUNT(*) FROM feedback WHERE kind = 'rescue' AND at >= ?", (since,)).fetchone()[0]
    if cuts < min_cuts:
        return 0.0
    return rescues / cuts
