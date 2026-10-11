"""The cut: mark the lowest-scored unread articles read in BazQux.

Two paths. The automatic floor runs with the timer and removes only what two
models agree is not worth the student (a second look saves close calls). The
manual prune is the student's own decision, as deep as they like, with a
concept cap so the survivors span topics. Every cut is a logged run, tags
before read state, and can be undone.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import store
from .bazqux import BazQuxClient
from .config import Config
from .teacher import Teacher, has_more_to_read

log = logging.getLogger(__name__)

BAND = 20  # one scoring band; the concept cap never displaces an article by more than this
BREAKER_KEY = "breaker_tripped"  # kv: the run whose floor tripped the breaker; set until `triage safety reset`


class BreakerTripped(Exception):
    pass


@dataclass
class Selection:
    cut: list[sqlite3.Row] = field(default_factory=list)
    survivors: list[sqlite3.Row] = field(default_factory=list)
    held: list[sqlite3.Row] = field(default_factory=list)  # close calls waiting for a batched second look
    boundary: float | None = None  # the lowest surviving score
    saved_by_second_look: int = 0
    reserved_for_exploration: int = 0
    second_looks: int = 0
    kept_after_failed_look: int = 0  # earlier runs' failed looks: they stay unread, and are not this run's saves
    blocked: str = ""  # why the floor cut nothing this run (the breaker is tripped), else empty

    @property
    def cut_ids(self) -> list[str]:
        return [r["id"] for r in self.cut]


def eligible(row: sqlite3.Row) -> bool:
    """Never cut, by either path: starred, protected (Must read, protected folders), or rescued
    by the student (marked unread again, or tagged `wrong`, after a cut)."""
    return not row["starred"] and not row["protected"] and not row["rescued_at"]


def breaker_tripped(conn: sqlite3.Connection) -> str | None:
    """The run id whose floor tripped the breaker, while the trip stands."""
    return store.kv_get(conn, BREAKER_KEY)


def reset_breaker(conn: sqlite3.Connection) -> bool:
    return store.kv_delete(conn, BREAKER_KEY)


# -- the automatic floor ------------------------------------------------------

def select_floor(conn: sqlite3.Connection, cfg: Config, teacher: Teacher | None, run_id: int | None,
                 newly_scored: set[str] | None = None, client: BazQuxClient | None = None,
                 stop_after: float | None = None) -> Selection:
    """Everything unread, scored with medium or high confidence, below its folder's floor,
    minus the second look's saves, minus the exploration quota, capped per run. Exempt as
    well: what an undo restored (``exempt``), until a prune takes it."""
    pool = store.unread_scored(conn)
    sel = Selection()
    tripped = breaker_tripped(conn)
    if tripped:
        # a trip stands until someone has looked: the same scores would trip it again next run
        sel.blocked = (f"breaker tripped in run {tripped}; the floor cuts nothing until "
                       "`triage safety reset`")
        log.warning("cut: %s", sel.blocked)
        sel.survivors = list(pool)
        sel.boundary = min((float(r["score"]) for r in sel.survivors if eligible(r)), default=None)
        return sel
    candidates: list[sqlite3.Row] = []
    for row in pool:
        floor = cfg.cut.floor_for(row["folder"])
        if (floor is None or not eligible(row) or row["exploration"] or row["exempt"]
                or row["confidence"] == "low"):
            sel.survivors.append(row)
            continue
        if row["score"] < floor:
            candidates.append(row)
        else:
            sel.survivors.append(row)

    # breaker: a model or prompt failure shows up as nearly everything new being junk
    if newly_scored is not None and len(newly_scored) >= cfg.breakers.min_new_for_breaker:
        new_candidates = sum(1 for r in candidates if r["id"] in newly_scored)
        share = new_candidates / len(newly_scored)
        if share > cfg.breakers.max_cut_share:
            store.kv_set(conn, BREAKER_KEY, str(run_id if run_id is not None else "?"))
            raise BreakerTripped(
                f"floor would cut {new_candidates} of {len(newly_scored)} newly scored articles "
                f"({share:.0%}); stopping before any write. The prompt or model has probably broken; "
                "the floor stays off until `triage safety reset`."
            )

    # second look: a close call whose full text holds more than the opening gets the stronger
    # model, once; the higher score stands. In batch mode the look arrives on the next run,
    # so the article is held (unread) until then.
    if teacher is not None:
        saved: list[sqlite3.Row] = []       # this run's second look cleared them, or never got to them
        kept: list[sqlite3.Row] = []        # an earlier run's look failed: kept, but not a save of this run
        remaining: list[sqlite3.Row] = []
        to_look: list[sqlite3.Row] = []
        for row in candidates:
            floor = cfg.cut.floor_for(row["folder"]) or cfg.cut.default_floor
            if float(row["score"]) < floor - cfg.scoring.second_look_window:
                remaining.append(row)
            elif row["second_look_model"] is not None:
                if row["second_look_model"] == "failed":
                    kept.append(row)  # fail open: a look that could not be had keeps the article
                else:
                    remaining.append(row)  # looked at already; the higher score still sits below the floor
            elif not has_more_to_read(row, cfg):
                remaining.append(row)  # the first pass saw everything there was
            elif teacher.batch_mode:
                sel.held.append(row)  # examined by the next run
            else:
                to_look.append(row)
        if to_look:
            results = teacher.second_looks(to_look, run_id, client=client, stop_after=stop_after)
            for row in to_look:
                if row["id"] not in results:
                    saved.append(row)  # never examined (budget, deadline, cap): stays unread
                    continue
                sel.second_looks += 1
                if results[row["id"]] is False:
                    remaining.append(row)  # the stronger model agreed: the cut stands
                else:
                    saved.append(row)  # cleared the floor, or the look failed: fail open
        sel.saved_by_second_look = len(saved)
        sel.kept_after_failed_look = len(kept)
        candidates = remaining
        sel.survivors.extend(saved)
        sel.survivors.extend(kept)
        sel.survivors.extend(sel.held)

    # exploration quota: new ground of decent quality is exempt from the floor
    base = len(newly_scored) - len(candidates) if newly_scored is not None else len(sel.survivors)
    quota = math.ceil(cfg.cut.exploration_share * max(0, base))
    if quota > 0 and candidates:
        since = store.days_ago_iso(cfg.feedback.concept_window_days)
        known = {c for c, _ in store.concept_counts(conn, ["star", "read", "rescue"], since, limit=500)}
        novel = [r for r in sorted(candidates, key=lambda r: -float(r["score"]))
                 if (r["quality"] or 0) >= 60 and (r["concept"] or "").strip().lower() not in known
                 and not r["dupe_of"]]
        reserved = novel[:quota]
        if reserved:
            store.set_flags(conn, [r["id"] for r in reserved], exploration=1)
            reserved_ids = {r["id"] for r in reserved}
            candidates = [r for r in candidates if r["id"] not in reserved_ids]
            sel.survivors.extend(reserved)
            sel.reserved_for_exploration = len(reserved)

    candidates.sort(key=lambda r: (float(r["score"]), r["crawled_msec"] or 0))
    sel.cut = candidates[: cfg.cut.max_cuts_per_run]
    sel.survivors.extend(candidates[cfg.cut.max_cuts_per_run:])
    sel.boundary = min((float(r["score"]) for r in sel.survivors if eligible(r)), default=None)
    return sel


# -- the manual prune ---------------------------------------------------------

def select_prune(conn: sqlite3.Connection, cfg: Config, keep: int | None = None,
                 fraction: float | None = None, below: float | None = None) -> Selection:
    """Your decision: cut to a count, by a fraction, or below a score. Protected and starred
    articles never move. ``keep`` ranks with a concept cap so the survivors span topics."""
    pool = [r for r in store.unread_scored(conn)]
    sel = Selection()
    movable = [r for r in pool if eligible(r)]
    fixed = [r for r in pool if not eligible(r)]
    by_score_desc = sorted(movable, key=lambda r: (-float(r["score"]), -(r["crawled_msec"] or 0)))

    if below is not None:
        sel.cut = [r for r in by_score_desc if float(r["score"]) < below]
        sel.survivors = [r for r in by_score_desc if float(r["score"]) >= below] + fixed
    elif fraction is not None:
        n_cut = math.ceil(max(0.0, min(1.0, fraction)) * len(by_score_desc))
        sel.survivors = by_score_desc[: len(by_score_desc) - n_cut] + fixed
        sel.cut = by_score_desc[len(by_score_desc) - n_cut:]
    else:
        target = cfg.cut.target_unread if keep is None else keep
        cap = max(1, math.ceil(cfg.cut.concept_cap * target))
        counts: Counter[str] = Counter()
        survivors: list[sqlite3.Row] = []
        deferred: list[sqlite3.Row] = []
        for row in by_score_desc:
            # a deferred article more than a band better than this one wins its seat back:
            # diversity spreads topics, it never trades a 90 for a 55
            if deferred and len(survivors) < target and float(deferred[0]["score"]) - float(row["score"]) > BAND:
                survivors.append(deferred.pop(0))
            if len(survivors) >= target:
                deferred.append(row)
                continue
            concept = (row["concept"] or "").strip().lower() or "unknown"
            if counts[concept] >= cap:
                deferred.append(row)
                continue
            counts[concept] += 1
            survivors.append(row)
        # if the cap left seats empty, fill them with the best deferred articles
        while len(survivors) < target and deferred:
            survivors.append(deferred.pop(0))
        sel.survivors = survivors + fixed
        sel.cut = deferred
    sel.cut.sort(key=lambda r: float(r["score"]))
    sel.boundary = min((float(r["score"]) for r in sel.survivors if eligible(r)), default=None)
    return sel


# -- writing to BazQux --------------------------------------------------------

def apply_cut(client: BazQuxClient, conn: sqlite3.Connection, cfg: Config, ids: Iterable[str],
              run_id: int, mode: str) -> int:
    ids = list(ids)
    if not ids:
        return 0
    store.mark_cut(conn, ids, run_id, mode)
    finish_pending_cuts(client, conn, cfg)
    return len(ids)


def finish_pending_cuts(client: BazQuxClient, conn: sqlite3.Connection, cfg: Config) -> tuple[int, int]:
    """Tag first, then mark read. A crash between the two leaves a tagged unread
    article that the next run finishes here. An article starred or protected since it was
    chosen (the student got to it first) is restored instead."""
    changed = store.cut_pending_now_exempt(conn)
    if changed:
        log.info("cut: %d pending cuts restored: starred or protected since", len(changed))
        store.restore_unread(conn, changed, rescued=False)
        try:
            client.untag(changed, cfg.tags.cut)
        except Exception:  # noqa: BLE001 — the tag is cosmetic; the read state is what matters
            log.warning("cut: could not remove the cut tag from %d restored articles", len(changed))
    untagged, unmarked = store.cut_pending(conn)
    if untagged:
        client.tag(untagged, cfg.tags.cut)
        store.set_flags(conn, untagged, cut_tagged=1)
    if unmarked:
        client.mark_read(unmarked)
        store.set_flags(conn, unmarked, read_marked=1)
    return len(untagged), len(unmarked)


def untag_stale(client: BazQuxClient, stale: dict[str, list[str]]) -> int:
    """Bucket tags a re-score (a new prompt version, say) left behind. Cosmetic: never fails a run."""
    done = 0
    for bucket, ids in stale.items():
        try:
            client.untag(ids, bucket)
            done += len(ids)
        except Exception:  # noqa: BLE001
            log.warning("cut: could not remove stale tag %s from %d articles", bucket, len(ids))
    stale.clear()
    return done


def tag_buckets(client: BazQuxClient, conn: sqlite3.Connection) -> int:
    """Every scored article carries its score bucket as a tag in BazQux."""
    done = 0
    for bucket, ids in store.untagged_buckets(conn).items():
        client.tag(ids, bucket)
        store.mark_bucket_tagged(conn, ids)
        done += len(ids)
    return done


def undo(client: BazQuxClient, conn: sqlite3.Connection, cfg: Config, run_id: int) -> int:
    """Reverse one run's cut. The restored articles are exempt from the automatic floor from
    then on (their scores have not changed, so it would only repeat the cut); a prune, the
    student's own decision, may still take them."""
    ids = store.run_cut_ids(conn, run_id)
    if not ids:
        return 0
    client.mark_unread(ids)
    client.untag(ids, cfg.tags.cut)
    store.restore_unread(conn, ids, rescued=False, exempt=True)
    return len(ids)


def bankruptcy(client: BazQuxClient, conn: sqlite3.Connection, cfg: Config, older_than_days: int,
               protected_ids: set[str], run_id: int, dry_run: bool = False) -> tuple[int, int]:
    """Mark read every unread article older than the cutoff, stars, protections and the
    student's rescues exempt. Returns (candidates, done)."""
    import time

    refs = client.unread_ids()
    cutoff_usec = int((time.time() - older_than_days * 86400) * 1_000_000)
    starred = set(client.starred_ids())
    exempt = starred | protected_ids | store.rescued_ids(conn)
    ids = [r.id for r in refs if r.timestamp_usec and r.timestamp_usec < cutoff_usec and r.id not in exempt]
    if dry_run or not ids:
        return len(ids), 0
    now = store.utcnow_iso()
    known = store.known_ids(conn)
    unknown = [aid for aid in ids if aid not in known]
    with store.transaction(conn):
        conn.executemany("INSERT OR IGNORE INTO articles (id, title, fetched_at) VALUES (?, ?, ?)",
                         [(aid, "(bankruptcy: not fetched)", now) for aid in unknown])
        conn.executemany("INSERT OR IGNORE INTO state (article_id, status, first_seen_at) VALUES (?, 'unread', ?)",
                         [(aid, now) for aid in unknown])
        store.mark_cut(conn, ids, run_id, "bankruptcy")
    finish_pending_cuts(client, conn, cfg)
    return len(ids), len(ids)
