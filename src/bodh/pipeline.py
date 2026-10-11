"""One run, start to finish: feedback, ingest, score, tag, floor, prune on request,
dashboard, heartbeat. Everything fails open: an exception leaves the unread list
as it was, records the error, and tells the heartbeat."""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from . import cut as cutmod
from . import dashboard, feedback, progress, store
from .bazqux import BazQuxClient, BazQuxError
from .config import Config, Secrets
from .learner import expansion_topics
from .ops import Heartbeat
from .teacher import LLM, Teacher, concept_context

log = logging.getLogger(__name__)

RUN_TIMEOUT_SECONDS = 15 * 60
RUN_DEADLINE_MARGIN = 60   # no new model call starts this close to the limit; running ones finish
INGEST_BATCH = 200


@dataclass
class RunResult:
    run_id: int
    ok: bool = True
    unread_upstream: int = 0
    new: int = 0
    scored: int = 0
    dupes: int = 0
    pending: int = 0
    submitted: int = 0        # batch mode: articles sent to the Batches API this run
    inflight: int = 0         # batch mode: articles whose judgment is still on its way
    second_looks: int = 0
    saved: int = 0
    held: int = 0             # batch mode: close calls waiting for their second look
    exploration: int = 0
    cut_floor: int = 0
    cut_prune: int = 0
    boundary: float | None = None
    feedback: dict = field(default_factory=dict)
    spend_usd: float = 0.0
    error: str = ""
    notes: list[str] = field(default_factory=list)
    batch_mode: bool = False

    def summary(self) -> str:
        fb = self.feedback
        parts = [f"unread {self.unread_upstream}", f"new {self.new}", f"judged {self.scored}"]
        if self.batch_mode:
            parts += [f"submitted {self.submitted}", f"in flight {self.inflight}"]
        parts += [
            f"dupes {self.dupes}", f"pending {self.pending}",
            f"second looks {self.second_looks} (saved {self.saved}" + (f", held {self.held})" if self.batch_mode else ")"),
            f"cut floor {self.cut_floor}", f"cut prune {self.cut_prune}",
            f"reads {fb.get('reads', 0)}", f"stars {fb.get('stars', 0)}", f"rescues {fb.get('rescues', 0)}",
            *( [f"verdicts {fb['verdicts']}"] if fb.get("verdicts") else [] ),
            f"spend ${self.spend_usd:.2f}",
        ]
        if self.boundary is not None:
            parts.append(f"boundary {self.boundary:.0f}")
        if self.notes:
            parts.append("; ".join(self.notes))
        return " · ".join(parts)


class Context:
    """Clients and configuration shared by the commands."""

    def __init__(self, cfg: Config, secrets: Secrets, conn: sqlite3.Connection) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.conn = conn
        self._client: BazQuxClient | None = None
        self._llm: LLM | None = None
        self.ref_time: dict[str, int] = {}  # article id -> BazQux timestamp, for newest-first ingest
        self.heartbeat = Heartbeat(secrets.value("healthcheck_url"))

    @property
    def client(self) -> BazQuxClient:
        if self._client is None:
            self._client = BazQuxClient(self.secrets.value("bazqux_email"), self.secrets.value("bazqux_password"))
        return self._client

    @property
    def llm(self) -> LLM:
        if self._llm is None:
            self._llm = LLM(self.cfg, self.secrets, self.conn)
        return self._llm

    def teacher(self) -> Teacher:
        return Teacher(self.cfg, self.llm, self.conn, context=concept_context(self.conn, self.cfg),
                       expansion_topics=expansion_topics(self.cfg.read_learner()))


def folder_map(client: BazQuxClient) -> dict[str, str | None]:
    return {s.stream_id: (s.folders[0] if s.folders else None) for s in client.subscriptions()}


def protected_ids(ctx: Context) -> set[str] | None:
    """Members of the Must read smart stream and of protected folders, or None when BazQux
    could not say: nothing may then be cut on that answer, and no protection is withdrawn."""
    try:
        ids: set[str] = set(ctx.client.label_ids(ctx.cfg.cut.must_read_stream, unread_only=True, strict=True))
        for name, policy in ctx.cfg.cut.folders.items():
            if policy.protected:
                ids |= set(ctx.client.label_ids(name, unread_only=True, strict=True))
    except BazQuxError as exc:
        log.warning("bazqux: could not read the protected streams (%s); nothing is cut this run", exc)
        return None
    return ids


def ingest(ctx: Context, unread_now: set[str], folders: dict[str, str | None]) -> int:
    """Fetch text for unread articles we have not seen, in batches."""
    new_ids = sorted((unread_now - store.known_ids(ctx.conn)) | (set(store.unread_without_text(ctx.conn)) & unread_now))
    if not new_ids:
        return 0
    # newest first, and written batch by batch, so a timeout on a huge first pile keeps its progress
    if ctx.ref_time:
        new_ids.sort(key=lambda i: -ctx.ref_time.get(i, 0))
    done = 0
    for start in range(0, len(new_ids), INGEST_BATCH):
        items, _vanished = ctx.client.contents(new_ids[start: start + INGEST_BATCH])
        with store.transaction(ctx.conn):  # one fsync per batch of 200, not per article
            for item in items:
                folder = folders.get(item.feed_stream_id)
                if folder is None:
                    # BazQux also lists folders among an item's categories as user/<id>/label/<name>
                    for cat in item.categories:
                        if "/label/" in cat:
                            folder = cat.rsplit("/label/", 1)[-1]
                            break
                store.upsert_article(ctx.conn, item, folder)
        done += len(items)
        if done and done % 2000 == 0:
            log.info("ingest: %d of %d fetched", done, len(new_ids))
    return done


def refresh_protection(ctx: Context, protected: set[str] | None, starred: set[str]) -> None:
    """Mark the unread articles BazQux says are protected (and those in protected folders),
    and clear the rest. With no answer from BazQux (``protected`` is None) a protection is
    never withdrawn: the flags from the last good answer stand."""
    rows = ctx.conn.execute("SELECT a.id, a.folder FROM articles a JOIN state st ON st.article_id = a.id "
                            "WHERE st.status = 'unread' "
                            "OR (st.status = 'cut' AND (st.cut_tagged = 0 OR st.read_marked = 0))").fetchall()
    prot = {r["id"] for r in rows
            if (protected is not None and r["id"] in protected) or ctx.cfg.cut.is_protected(r["folder"])}
    store.set_flags(ctx.conn, prot, protected=1)
    if protected is not None:
        store.set_flags(ctx.conn, [r["id"] for r in rows if r["id"] not in prot], protected=0)
    store.set_flags(ctx.conn, [r["id"] for r in rows if r["id"] in starred], starred=1)


def run(ctx: Context, score_limit: int | None = None, enforce: bool = True, workers: int | None = None,
        sync: bool = False, stop_after: float | None = None) -> RunResult:
    """The timer's job. ``enforce=False`` is shadow mode: everything but the cuts.
    ``sync=True`` waits for every model call instead of using the Batches API. No model call
    starts after ``stop_after`` (a time.monotonic() value; by default a minute short of the
    run's limit), so the next scheduled run is not skipped because this one overran."""
    cfg, conn = ctx.cfg, ctx.conn
    cfg.check_seed()   # before any network: nothing is fetched or recorded for a student who does not exist
    if stop_after is None:
        stop_after = time.monotonic() + RUN_TIMEOUT_SECONDS - RUN_DEADLINE_MARGIN
    run_id = store.begin_run(conn, "run")
    res = RunResult(run_id=run_id)
    ctx.heartbeat.start()
    live = progress.live_writer(cfg, conn)

    def phase(text: str) -> None:
        progress.set_phase(conn, text)
        progress.write_live(cfg, conn)

    try:
        phase("talking to BazQux")
        folders = folder_map(ctx.client)
        refs = ctx.client.unread_ids()
        unread_now = {r.id for r in refs}
        ctx.ref_time = {r.id: (r.timestamp_usec or 0) for r in refs}
        res.unread_upstream = len(unread_now)
        starred = set(ctx.client.starred_ids())
        protected = protected_ids(ctx)
        if protected is None and enforce:
            enforce = False
            res.notes.append("could not read the Must read stream; nothing cut this run")

        res.feedback = feedback.collect(ctx.client, conn, cfg, unread_now, starred)
        phase("fetching new articles")
        res.new = ingest(ctx, unread_now, folders)
        refresh_protection(ctx, protected, starred)
        cutmod.finish_pending_cuts(ctx.client, conn, cfg)

        teacher = ctx.teacher()
        if sync:
            teacher.batch_mode = False
        res.batch_mode = teacher.batch_mode
        before = store.scored_ids(conn)
        if store.open_batches(conn):
            # judgments submitted last time come back now, whatever mode this run is in: a batch
            # submitted before `models.batch` was turned off must not strand its articles
            phase("collecting finished batches")
            col = teacher.collect(run_id, client=ctx.client, on_progress=live)
            res.scored, res.second_looks, res.saved = col["scored"], col["second_looks"], col["saved"]
            res.notes.extend(col["notes"])
            if col["failed"]:
                res.notes.append(f"{col['failed']} second look(s) failed; those articles stay unread")
        else:
            col = {"scored": 0}
        if teacher.batch_mode:
            # what arrived since goes out
            phase("submitting a batch")
            sub = teacher.submit_pending(run_id, limit=score_limit)
            res.submitted, res.dupes = sub["submitted"], sub["dupes"]
            if sub["budget_stop"]:
                res.notes.append("daily budget reached; the rest waits for tomorrow")
            if sub["waiting"]:
                res.notes.append(f"{sub['waiting']} articles wait for a fuller batch")
            res.pending = teacher.pending_count(exclude_inflight=True)
            res.inflight = len(store.inflight_ids(conn, "teacher"))
            if col["scored"] or sub["submitted"]:
                log.info("teacher: judged %d, submitted %d, in flight %d, pending %d",
                         col["scored"], sub["submitted"], res.inflight, res.pending)
        else:
            phase("judging")
            stats = teacher.score_pending(run_id, limit=score_limit or cfg.scoring.max_per_run,
                                          workers=workers or cfg.scoring.workers, on_progress=live,
                                          stop_after=stop_after)
            if stats["stopped_early"]:
                res.notes.append("time limit near: the rest waits for the next run")
            res.scored += stats["scored"]
            res.dupes, res.pending = stats["dupes"], stats["pending_after"]
            if stats["budget_stop"]:
                res.notes.append("daily budget reached; unscored articles stay unread")
            if stats["failed_batches"]:
                res.notes.append(f"{stats['failed_batches']} batch(es) failed and stay pending")
        newly_scored = store.scored_ids(conn) - before
        phase("tagging in BazQux")
        cutmod.untag_stale(ctx.client, teacher.stale_tags)
        cutmod.tag_buckets(ctx.client, conn)

        if enforce:
            phase("applying the floor")
            if feedback.rescue_rate_week(conn) > cfg.breakers.rescue_rate_week:
                res.notes.append(f"rescues above {cfg.breakers.rescue_rate_week:.0%} this week; floor lowered to "
                                 f"{cfg.breakers.lowered_floor}")
                cfg.cut.default_floor = min(cfg.cut.default_floor, cfg.breakers.lowered_floor)
            sel = cutmod.select_floor(conn, cfg, teacher, run_id, newly_scored=newly_scored, client=ctx.client,
                                      stop_after=stop_after)
            if sel.blocked:
                res.notes.append(sel.blocked)
            res.second_looks += sel.second_looks
            res.saved += sel.saved_by_second_look
            res.exploration = sel.reserved_for_exploration
            res.cut_floor = cutmod.apply_cut(ctx.client, conn, cfg, sel.cut_ids, run_id, "floor")
            res.boundary = sel.boundary
            if sel.held:
                res.held = teacher.submit_second_looks(run_id, sel.held)
            if res.feedback.get("prune_requested") and sel.blocked:
                res.notes.append("prune requested by tag; it waits for `bodh safety reset`")
            elif res.feedback.get("prune_requested"):
                psel = cutmod.select_prune(conn, cfg)
                res.cut_prune = cutmod.apply_cut(ctx.client, conn, cfg, psel.cut_ids, run_id, "prune")
                res.boundary = psel.boundary
                feedback.prune_request_done(conn)
                res.notes.append("prune tag seen: pruned to target")
        else:
            res.notes.append("shadow mode: nothing cut")
            if res.feedback.get("prune_requested"):
                res.notes.append("prune requested by tag; it waits for an enforcing run")
        if res.feedback.get("suspect_flip"):
            res.notes.append(f"{res.feedback['suspect_flip']} unread articles gone upstream at once: not recorded "
                             "as read until the next run sees the same")

        res.spend_usd = store.spent_since(conn, store.local_midnight_iso(cfg.timezone))
        store.finish_run(conn, run_id, True, unread_upstream=res.unread_upstream, new=res.new, scored=res.scored,
                         second_looks=res.second_looks, cut_floor=res.cut_floor, cut_prune=res.cut_prune,
                         rescues=res.feedback.get("rescues", 0), reads=res.feedback.get("reads", 0),
                         spend_usd=res.spend_usd, boundary=res.boundary, summary=res.summary())
        phase("rendering the dashboard")
        render_dashboard(cfg, conn, res)
        progress.set_phase(conn, "")
        progress.write_live(cfg, conn)
        ctx.heartbeat.ok(res.summary())
        return res
    except KeyboardInterrupt:
        res.ok = False
        res.error = "stopped"
        store.finish_run(conn, run_id, False, error=res.error, summary=res.summary())
        progress.set_phase(conn, "")
        progress.write_live(cfg, conn)
        raise
    except Exception as exc:
        res.ok = False
        res.error = f"{type(exc).__name__}: {exc}"
        log.exception("run %d failed", run_id)
        store.finish_run(conn, run_id, False, error=res.error, summary=res.summary())
        progress.set_phase(conn, "")
        progress.write_live(cfg, conn)
        ctx.heartbeat.fail(res.error)
        raise


def render_dashboard(cfg: Config, conn: sqlite3.Connection, res: RunResult | None = None) -> None:
    try:
        dashboard.render(cfg, conn)
    except Exception:  # noqa: BLE001 — the dashboard must never fail a run
        log.exception("dashboard: render failed")
        if res is not None:
            res.notes.append("dashboard render failed")


def blast(ctx: Context, workers: int, chunk: int = 1000, all_ages: bool = False, budget: float | None = None,
          on_progress: Any = None, model: str | None = None, max_seconds: float | None = None) -> dict:
    """One deliberate pass over everything pending, as fast as the API allows: synchronous,
    many calls at once, round after round until nothing is pending. Scores and tags only;
    nothing is cut. Resumable: stop it and run it again. With ``model`` a stand-in (a faster
    one) judges what nobody has judged yet and leaves the teacher's judgments alone; the
    teacher later re-judges what the stand-in rated promising (scoring.rejudge_min_score)."""
    cfg, conn = ctx.cfg, ctx.conn
    if all_ages:
        cfg.scoring.max_age_days = None
    if budget is not None:
        cfg.models.daily_budget_usd = max(cfg.models.daily_budget_usd, budget)
    stand_in = bool(model) and model != cfg.models.teacher
    if model:
        cfg.models.teacher = model
    teacher = ctx.teacher()
    teacher.batch_mode = False
    if stand_in:
        teacher.rejudge = None
    ctx.llm.retries(8)
    run_id = store.begin_run(conn, "blast")
    total = {"scored": 0, "dupes": 0, "failed": 0, "rounds": 0, "pending": teacher.pending_count(exclude_inflight=True),
             "budget_stop": False, "started": time.monotonic(), "interrupted": False, "run_id": run_id,
             "timed_out": False}
    deadline = total["started"] + max_seconds if max_seconds else None
    live = progress.live_writer(cfg, conn)
    current: dict = {}

    def tick(stats: dict) -> None:
        current.clear()
        current.update(stats)
        live(stats)
        if on_progress is not None:
            on_progress(total, stats)

    idle_rounds = 0
    try:
        while total["pending"] > 0:
            progress.set_phase(conn, f"blast round {total['rounds'] + 1}: judging {min(chunk, total['pending']):,} "
                                     f"of {total['pending']:,} pending")
            progress.write_live(cfg, conn)
            current.clear()
            stats = teacher.score_pending(run_id, limit=chunk, workers=workers, on_progress=tick, stop_after=deadline)
            current.clear()
            total["rounds"] += 1
            total["scored"] += stats["scored"]
            total["dupes"] += stats["dupes"]
            total["failed"] += stats["failed_batches"]
            total["pending"] = stats["pending_after"]
            if stats["budget_stop"]:
                total["budget_stop"] = True
                break
            if stats["stopped_early"] or (deadline is not None and time.monotonic() > deadline):
                total["timed_out"] = True
                break
            if stats["scored"] == 0 and stats["dupes"] == 0:
                idle_rounds += 1
                if idle_rounds >= 2:
                    break  # the API keeps refusing; stop rather than spin
                time.sleep(10)
            else:
                idle_rounds = 0
    except KeyboardInterrupt:
        total["interrupted"] = True
        if current:  # the round that was cut short still counts what landed
            total["rounds"] += 1
            total["scored"] += current.get("scored", 0)
            total["dupes"] += current.get("dupes", 0)
            total["failed"] += current.get("failed_batches", 0)
        total["pending"] = teacher.pending_count(exclude_inflight=True)
    except Exception as exc:
        total["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        progress.set_phase(conn, "tagging in BazQux")
        progress.write_live(cfg, conn)
        try:
            cutmod.untag_stale(ctx.client, teacher.stale_tags)
            cutmod.tag_buckets(ctx.client, conn)
        except Exception:  # noqa: BLE001 — tags can be finished by the next run
            log.exception("blast: tagging failed; the next run finishes it")
        total["elapsed"] = time.monotonic() - total["started"]
        total["spend"] = store.spent_since(conn, store.local_midnight_iso(cfg.timezone))
        summary = (f"blast ({cfg.models.teacher}): judged {total['scored']} (+{total['dupes']} dupes) in {total['rounds']} rounds, "
                   f"{total['elapsed'] / 60:.1f} min, {total['failed']} failed batches, pending {total['pending']}"
                   + (", interrupted" if total["interrupted"] else "")
                   + (", time limit" if total["timed_out"] else "")
                   + (", budget reached" if total["budget_stop"] else ""))
        store.finish_run(conn, run_id, not total["interrupted"] and not total.get("error"), scored=total["scored"],
                         spend_usd=total["spend"], summary=summary, error=total.get("error"))
        render_dashboard(cfg, conn)
        progress.set_phase(conn, "")
        progress.write_live(cfg, conn)
    return total


def prune(ctx: Context, keep: int | None = None, fraction: float | None = None,
          below: float | None = None, dry_run: bool = False,
          confirmed: set[str] | None = None) -> tuple[int, cutmod.Selection]:
    """The student's cut. Refused while the breaker is tripped: the scores it would rank by are
    the ones the breaker declared suspect. ``confirmed`` (the ids shown and agreed to) bounds a
    real prune to them, so a run between the question and the answer cannot widen the cut."""
    cfg, conn = ctx.cfg, ctx.conn
    tripped = cutmod.breaker_tripped(conn)
    if tripped:
        raise cutmod.BreakerTripped(f"the breaker tripped in run {tripped}: the latest scores may be wrong; "
                                    "check a few with `bodh why ID`, then `bodh safety reset`")
    sel = cutmod.select_prune(conn, cfg, keep=keep, fraction=fraction, below=below)
    if confirmed is not None:
        unconfirmed = [r for r in sel.cut if r["id"] not in confirmed]
        if unconfirmed:
            sel.cut = [r for r in sel.cut if r["id"] in confirmed]
            sel.survivors.extend(unconfirmed)
            sel.boundary = min((float(r["score"]) for r in sel.survivors if cutmod.eligible(r)), default=None)
    if dry_run or not sel.cut:
        return 0, sel
    run_id = store.begin_run(conn, "prune")
    try:
        progress.set_phase(conn, f"prune: cutting {len(sel.cut_ids):,} in BazQux")
        progress.write_live(cfg, conn)
        n = cutmod.apply_cut(ctx.client, conn, cfg, sel.cut_ids, run_id, "prune")
        store.finish_run(conn, run_id, True, cut_prune=n, boundary=sel.boundary,
                         summary=f"prune: cut {n}, boundary {sel.boundary}")
        return n, sel
    except KeyboardInterrupt:
        store.finish_run(conn, run_id, False, cut_prune=len(sel.cut_ids), boundary=sel.boundary,
                         error="stopped; the next run finishes the tags and read marks, `bodh undo` reverses it")
        raise
    except Exception as exc:
        store.finish_run(conn, run_id, False, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        progress.set_phase(conn, "")
        render_dashboard(cfg, conn)
        progress.write_live(cfg, conn)


def undo_last(ctx: Context) -> tuple[int | None, int]:
    # any run that cut something, finished or interrupted: an interrupted prune has still cut
    row = ctx.conn.execute(
        "SELECT id FROM runs WHERE kind IN ('run','prune','bankruptcy') "
        "AND id IN (SELECT DISTINCT cut_run_id FROM state WHERE cut_run_id IS NOT NULL) ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None, 0
    run_id = int(row[0])
    n = cutmod.undo(ctx.client, ctx.conn, ctx.cfg, run_id)
    undo_id = store.begin_run(ctx.conn, "undo")
    store.finish_run(ctx.conn, undo_id, True, summary=f"undo run {run_id}: {n} restored")
    return run_id, n


def bankruptcy(ctx: Context, older_than_days: int, dry_run: bool) -> tuple[int, int]:
    """A dry run counts and records nothing; a real one is a run of its own, undoable."""
    refusal = "could not read the Must read stream; bankruptcy refused rather than cut a protected article"
    if dry_run:
        protected = protected_ids(ctx)
        if protected is None:
            raise BazQuxError(refusal)
        return cutmod.bankruptcy(ctx.client, ctx.conn, ctx.cfg, older_than_days, protected, run_id=0, dry_run=True)
    run_id = store.begin_run(ctx.conn, "bankruptcy")
    try:
        protected = protected_ids(ctx)
        if protected is None:
            raise BazQuxError(refusal)
        candidates, done = cutmod.bankruptcy(ctx.client, ctx.conn, ctx.cfg, older_than_days, protected, run_id)
        store.finish_run(ctx.conn, run_id, True, cut_prune=done,
                         summary=f"bankruptcy: {candidates} older than {older_than_days}d, {done} marked read")
        return candidates, done
    except Exception as exc:
        store.finish_run(ctx.conn, run_id, False, error=f"{type(exc).__name__}: {exc}")
        raise
