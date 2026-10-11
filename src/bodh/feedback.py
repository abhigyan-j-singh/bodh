"""Feedback, collected from BazQux state and from the student's tags. With no labelling
chores: stars, inferred reads, rescues (a cut article marked unread again, or tagged
`rescue`), and a `prune` tag asking for one more step. With a tag, the student's verdict:
a grade (`great`, `good`, `meh`, `bad`), the level missed (`basic`, `heavy`), or a steer
(`more`, `less`) that the judge sees from the next run. Verdict tags stay on the article
as the student's own record; each is recorded once."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from . import store
from .config import Config
from .readers.bazqux import BazQuxClient

log = logging.getLogger(__name__)

READ_INFERENCE_MAX_AGE_DAYS = 7  # older unread articles that vanish may have been evicted, not read
SUSPECT_FLIP_SHARE = 0.8  # more of our unread list gone at once than this looks like a bad answer...
SUSPECT_FLIP_MIN = 20  # ...when at least this many articles are involved
SUSPECT_FLIP_CONFIRM_HOURS = 3  # ...unless the previous run, within this long, saw the same thing
SUSPECT_FLIP_KEY = "suspect_flip_at"
PRUNE_REQUEST_KEY = "prune_requested"
MIN_CUTS_FOR_RESCUE_RATE = 20  # the weekly rescue rate means nothing on a handful of cuts


def collect(
    client: BazQuxClient, conn: sqlite3.Connection, cfg: Config, unread_now: set[str], starred_now: set[str]
) -> dict:
    """What the student did since the last run, from BazQux's state: stars, articles marked
    unread again, reads and dismissals, rescues, verdict tags, the prune request."""
    stats = {
        "stars": 0,
        "reads": 0,
        "dismissed": 0,
        "expired": 0,
        "returned": 0,
        "suspect_flip": 0,
        "rescues": 0,
        "verdicts": 0,
        "steers": 0,
        "prune_requested": False,
    }
    known = store.known_ids(conn)
    # stars: explicit, the strongest signal, and a protection
    new_stars = [aid for aid in starred_now & known if store.add_feedback(conn, aid, "star")]
    store.set_flags(conn, [aid for aid in starred_now & known], starred=1)
    stats["stars"] = len(new_stars)
    stats["returned"] = _collect_returned(conn, unread_now)
    ours_unread = store.status_ids(conn, "unread")
    _collect_reads(conn, cfg, ours_unread, unread_now, stats)
    stats["rescues"] = _collect_rescues(client, conn, cfg, known, unread_now)
    stats.update(collect_verdicts(client, conn, cfg, known))
    # prune: any article tagged `prune` asks for one more step. The request is kept until a
    # run with autocut on applies it: scoring-only mode or a failed run must not lose it.
    prune_ids = client.label_ids(cfg.tags.prune)
    if prune_ids:
        client.untag(prune_ids, cfg.tags.prune)
        store.kv_set(conn, PRUNE_REQUEST_KEY, store.utcnow_iso())
    stats["prune_requested"] = store.kv_get(conn, PRUNE_REQUEST_KEY) is not None
    # everything still unread was seen unread now
    store.set_flags(conn, list(ours_unread & unread_now), last_seen_unread_at=store.utcnow_iso())
    return stats


def _collect_returned(conn: sqlite3.Connection, unread_now: set[str]) -> int:
    """Back to unread: what we recorded as read, dismissed or expired that the student marked
    unread again belongs to the pool once more (no feedback: nothing is known about why). One
    the student had read (a `read` row) and kept to finish later is exempt from autocut, which
    would only cut it for its old score; a prune may still take it. A reversed dismissal or
    expiry (BazQux's answer was wrong for an hour) earns no exemption: nothing about those
    articles changed."""
    returned: set[str] = set()
    for status in ("read", "dismissed", "expired"):
        returned |= store.status_ids(conn, status) & unread_now
    if not returned:
        return 0
    kept = returned & store.feedback_ids(conn, "read")
    store.restore_unread(conn, kept, rescued=False, exempt=True)
    store.restore_unread(conn, returned - kept, rescued=False)
    return len(returned)


def _collect_reads(
    conn: sqlite3.Connection, cfg: Config, ours_unread: set[str], unread_now: set[str], stats: dict
) -> None:
    """Reads and dismissals: what we left unread and is now read, by the student's hand. A
    burst across several feeds is mark-all-as-read, not reading; an old article going read is
    expiry, not reading."""
    flipped = ours_unread - unread_now
    if _suspect_flip(conn, ours_unread, flipped, unread_now):
        stats["suspect_flip"] = len(flipped)
        return
    if not flipped:
        return
    rows = store.fetch_in(
        conn, "SELECT id, feed_title, crawled_msec FROM articles WHERE id IN ({marks})", flipped
    )
    now_msec = int(time.time() * 1000)
    recent = [
        r
        for r in rows
        if (r["crawled_msec"] or now_msec) >= now_msec - READ_INFERENCE_MAX_AGE_DAYS * 86400_000
    ]
    recent_ids = {r["id"] for r in recent}
    expired = [r["id"] for r in rows if r["id"] not in recent_ids]
    feeds = Counter(r["feed_title"] for r in recent)
    bulk = len(recent) >= cfg.safety.mark_all_read_threshold and len(feeds) >= 3
    kind = "dismiss" if bulk else "read"
    for r in recent:
        store.add_feedback(conn, r["id"], kind)
    store.set_flags(conn, recent_ids, status=("dismissed" if bulk else "read"), read_at=store.utcnow_iso())
    store.set_flags(conn, expired, status="expired")
    stats["dismissed" if bulk else "reads"] = len(recent)
    stats["expired"] = len(expired)


def _collect_rescues(
    client: BazQuxClient, conn: sqlite3.Connection, cfg: Config, known: set[str], unread_now: set[str]
) -> int:
    """Rescues: a cut article that is unread again was marked unread by the student; a `rescue`
    tag on a cut article asks for the same, whether or not the cut has reached BazQux yet (a
    cut whose read mark failed is still unread there, carrying the tag). `rescue` on anything
    else is a strong positive with no rescue in it (there was no cut to reverse)."""
    ours_cut_marked = {
        r[0] for r in conn.execute("SELECT article_id FROM state WHERE status = 'cut' AND read_marked = 1")
    }
    ours_cut = store.status_ids(conn, "cut")
    # the rescue tag under its name and, for as long as people may have it on articles, the old one
    tagged: dict[str, list[str]] = {}
    for name in cfg.tags.rescue_tags():
        tagged[name] = [aid for aid in client.label_ids(name) if aid in known]
    wrong_ids = sorted({aid for ids in tagged.values() for aid in ids})
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
    for name, ids in tagged.items():
        if ids:
            client.untag(ids, name)
    return len(rescued)


def _suspect_flip(
    conn: sqlite3.Connection, ours_unread: set[str], flipped: set[str], unread_now: set[str]
) -> bool:
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
            log.warning(
                "feedback: %d of %d unread articles gone upstream, as on the previous run too: "
                "recording the reads",
                len(flipped),
                len(ours_unread),
            )
            store.kv_delete(conn, SUSPECT_FLIP_KEY)
            return False
    log.warning(
        "feedback: %d of %d unread articles are gone upstream at once; not believed until the "
        "next run sees the same (an empty or truncated answer from BazQux looks like this)",
        len(flipped),
        len(ours_unread),
    )
    store.kv_set(conn, SUSPECT_FLIP_KEY, store.utcnow_iso())
    return True


def prune_request_done(conn: sqlite3.Connection) -> None:
    store.kv_delete(conn, PRUNE_REQUEST_KEY)


def collect_verdicts(client: BazQuxClient, conn: sqlite3.Connection, cfg: Config, known: set[str]) -> dict:
    """The student's tags, read as verdicts. A tag stays on the article (the student's record)
    and is recorded once. Within a group one verdict holds per article: a different tag added
    later replaces it (when both are present, the later one in the group's order wins, so
    `bad` beats `good`); removing a tag alone retracts nothing. `more` and `less` also write a
    dated steer, with the article's concept, into the learned profile, which the judge reads
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
    steers: list[tuple[str, str]] = []
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
                    steers.append((kind, _steer_phrase(conn, aid)))
    if steers:
        from . import profiler  # here, not at the top: the profiler imports the judge

        profiler.steers(cfg, conn, [("want" if kind == "more" else "meh", phrase) for kind, phrase in steers])
        stats["steers"] = len(steers)
    return stats


def rescue_one(client: BazQuxClient, conn: sqlite3.Connection, cfg: Config, article_id: str, row: Any) -> str:
    """`bodh mark ID rescue`: the same as the rescue tag, now. A cut article comes back unread in
    BazQux and here, is never cut again by any path, and counts as the strongest positive; an
    article that was never cut gets the positive only. Returns the line to print."""
    if row["status"] == "cut":
        client.mark_unread([article_id])
        try:
            client.untag([article_id], cfg.tags.cut)
        except Exception:  # cosmetic: the rescue stands with or without the tag gone
            log.warning("feedback: could not remove the cut tag from %s", article_id)
        store.restore_unread(conn, [article_id], rescued=True)
        store.add_feedback(conn, article_id, "rescue")
        store.add_feedback(conn, article_id, "wrong")
        return f"rescued: {(row['title'] or '')[:70]} is unread again and will not be cut"
    store.add_feedback(conn, article_id, "wrong")
    return f"recorded as a strong positive (it was not cut): {(row['title'] or '')[:70]}"


def steer_phrase(conn: sqlite3.Connection, article_id: str) -> str:
    """The phrase a steer by article carries into the learned profile: its concept, with the title."""
    return _steer_phrase(conn, article_id)


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
    """Rescues as a share of the week's autocut and prune cuts (the cuts still standing plus the
    rescued ones). Clear-old cuts and undone runs do not count: the student's own sweep and
    reversal say nothing about autocut; nor does a week with fewer than ``min_cuts`` cuts."""
    since = store.days_ago_iso(7)
    cuts = conn.execute(
        "SELECT COUNT(*) FROM state WHERE (cut_at >= ? AND cut_mode IN ('autocut', 'floor', 'prune')) OR rescued_at >= ?",
        (since, since),
    ).fetchone()[0]
    rescues = conn.execute(
        "SELECT COUNT(*) FROM feedback WHERE kind = 'rescue' AND at >= ?", (since,)
    ).fetchone()[0]
    if cuts < min_cuts:
        return 0.0
    return rescues / cuts
