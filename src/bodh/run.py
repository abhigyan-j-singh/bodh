"""One run, start to finish: feedback, ingest, judge, tag, autocut, prune on request,
dashboard, heartbeat. Everything fails open: an exception leaves the unread list
as it was, records the error, and tells the heartbeat."""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from . import cutting as cutmod
from . import dashboard, feedback, progress, recovery, store
from .config import Config, Secrets
from .heartbeat import Heartbeat
from .judge import Judge, concept_context
from .llm import LLM
from .profiler import expansion_topics
from .providers.base import model_name, same_model
from .readers.bazqux import BazQuxClient, BazQuxError

log = logging.getLogger(__name__)

RUN_TIMEOUT_SECONDS = 15 * 60
RUN_DEADLINE_MARGIN = 60  # no new model call starts this close to the limit; running ones finish
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
    submitted: int = 0  # batch mode: articles sent to the Batches API this run
    inflight: int = 0  # batch mode: articles whose judgment is still on its way
    reviews: int = 0
    saved: int = 0
    held: int = 0  # batch mode: close calls waiting for their review
    new_ground: int = 0
    cut_autocut: int = 0
    cut_prune: int = 0
    boundary: float | None = None
    feedback: dict[str, Any] = field(default_factory=dict)
    spend_usd: float = 0.0
    error: str = ""
    notes: list[str] = field(default_factory=list)
    batch_mode: bool = False

    def summary(self) -> str:
        fb = self.feedback
        parts = [f"unread {self.unread_upstream}", f"new {self.new}", f"judged {self.scored}"]
        if self.batch_mode:
            parts += [f"submitted {self.submitted}", f"with the model {self.inflight}"]
        parts += [
            f"dupes {self.dupes}",
            f"pending {self.pending}",
            f"reviews {self.reviews} (saved {self.saved}"
            + (f", waiting for review {self.held})" if self.batch_mode else ")"),
            f"autocut {self.cut_autocut}",
            f"pruned {self.cut_prune}",
            f"reads {fb.get('reads', 0)}",
            f"stars {fb.get('stars', 0)}",
            f"rescues {fb.get('rescues', 0)}",
            *([f"verdicts {fb['verdicts']}"] if fb.get("verdicts") else []),
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
            self._client = BazQuxClient(
                self.secrets.value("bazqux_email"), self.secrets.value("bazqux_password")
            )
        return self._client

    @property
    def llm(self) -> LLM:
        if self._llm is None:
            self._llm = LLM(self.cfg, self.secrets, self.conn)
        return self._llm

    def judge(self) -> Judge:
        return Judge(
            self.cfg,
            self.llm,
            self.conn,
            context=concept_context(self.conn, self.cfg),
            expansion_topics=expansion_topics(self.cfg.read_learned()),
        )


def folder_map(client: BazQuxClient) -> dict[str, str | None]:
    return {s.stream_id: (s.folders[0] if s.folders else None) for s in client.subscriptions()}


def protected_ids(ctx: Context) -> set[str] | None:
    """Members of the Must read smart stream and of protected folders, or None when BazQux
    could not say: nothing may then be cut on that answer, and no protection is withdrawn."""
    try:
        ids: set[str] = set(
            ctx.client.label_ids(ctx.cfg.protect.must_read_stream, unread_only=True, strict=True)
        )
        for name, policy in ctx.cfg.folders.items():
            if policy.protected:
                ids |= set(ctx.client.label_ids(name, unread_only=True, strict=True))
    except BazQuxError as exc:
        log.warning("bazqux: could not read the protected streams (%s); nothing is cut this run", exc)
        return None
    return ids


def ingest(ctx: Context, unread_now: set[str], folders: dict[str, str | None]) -> int:
    """Fetch text for unread articles we have not seen, in batches."""
    new_ids = sorted(
        (unread_now - store.known_ids(ctx.conn)) | (set(store.unread_without_text(ctx.conn)) & unread_now)
    )
    if not new_ids:
        return 0
    # newest first, and written batch by batch, so a timeout on a huge first pile keeps its progress
    if ctx.ref_time:
        new_ids.sort(key=lambda i: -ctx.ref_time.get(i, 0))
    done = 0
    for start in range(0, len(new_ids), INGEST_BATCH):
        items, _vanished = ctx.client.contents(new_ids[start : start + INGEST_BATCH])
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
    rows = ctx.conn.execute(
        "SELECT a.id, a.folder FROM articles a JOIN state st ON st.article_id = a.id "
        "WHERE st.status = 'unread' "
        "OR (st.status = 'cut' AND (st.cut_tagged = 0 OR st.read_marked = 0))"
    ).fetchall()
    prot = {
        r["id"]
        for r in rows
        if (protected is not None and r["id"] in protected) or ctx.cfg.is_protected(r["folder"])
    }
    store.set_flags(ctx.conn, prot, protected=1)
    if protected is not None:
        store.set_flags(ctx.conn, [r["id"] for r in rows if r["id"] not in prot], protected=0)
    store.set_flags(ctx.conn, [r["id"] for r in rows if r["id"] in starred], starred=1)


@dataclass
class _Run:
    """What one run's phases hand each other."""

    ctx: Context
    res: RunResult
    enforce: bool
    sync: bool
    score_limit: int | None
    workers: int | None
    stop_after: float
    live: Any
    folders: dict[str, str | None] = field(default_factory=dict)
    unread_now: set[str] = field(default_factory=set)
    starred: set[str] = field(default_factory=set)
    protected: set[str] | None = None
    judge: Judge | None = None
    newly_scored: set[str] = field(default_factory=set)
    budget_stop: bool = False  # the daily budget stopped the judging: this run cuts nothing
    rejudge: bool = False  # every unread article another model judged is pending for the judge

    @property
    def cfg(self) -> Config:
        return self.ctx.cfg

    @property
    def conn(self) -> sqlite3.Connection:
        return self.ctx.conn

    @property
    def run_id(self) -> int:
        return self.res.run_id

    def phase(self, text: str) -> None:
        """The phase goes to the database at once (the console reads it there); the live file
        follows unless it was written in the last second (a snapshot walks the whole store)."""
        progress.set_phase(self.conn, text)
        progress.write_live(self.cfg, self.conn, min_interval=0.0 if not text else 1.0)


def run(
    ctx: Context,
    score_limit: int | None = None,
    enforce: bool = True,
    workers: int | None = None,
    sync: bool = False,
    stop_after: float | None = None,
    rejudge: bool = False,
) -> RunResult:
    """The timer's job. ``enforce=False`` is scoring-only mode: everything but the cuts.
    ``sync=True`` waits for every model call instead of using the Batches API. No model call
    starts after ``stop_after`` (a time.monotonic() value; by default a minute short of the
    run's limit), so the next scheduled run is not skipped because this one overran.
    ``rejudge`` has the judge read again every unread article another model judged (after a
    switch of ``models.judge``, on request; a switch alone re-judges nothing)."""
    cfg, conn = ctx.cfg, ctx.conn
    cfg.check_about()  # before any network: nothing is fetched or recorded for a student who does not exist
    if stop_after is None:
        stop_after = time.monotonic() + RUN_TIMEOUT_SECONDS - RUN_DEADLINE_MARGIN
    run_id = store.begin_run(conn, "run")
    res = RunResult(run_id=run_id)
    ctx.heartbeat.start()
    st = _Run(
        ctx,
        res,
        enforce=enforce,
        sync=sync,
        score_limit=score_limit,
        workers=workers,
        stop_after=stop_after,
        live=progress.live_writer(cfg, conn),
        rejudge=rejudge,
    )
    try:
        _collect_feedback(st)
        _fetch(st)
        _judge(st)
        _tag(st)
        _autocut(st)
        _finish(st)
        return res
    except KeyboardInterrupt:
        res.ok = False
        res.error = "stopped"
        log.warning("run %d stopped: %s", run_id, res.summary())
        store.finish_run(conn, run_id, False, error=res.error, summary=res.summary())
        st.phase("")
        raise
    except Exception as exc:
        res.ok = False
        res.error = f"{type(exc).__name__}: {exc}"
        log.exception("run %d failed", run_id)
        store.finish_run(conn, run_id, False, error=res.error, summary=res.summary())
        st.phase("")
        ctx.heartbeat.fail(res.error)
        raise


def _collect_feedback(st: _Run) -> None:
    """What is unread, starred and protected now, and what the student did since the last run."""
    ctx, res = st.ctx, st.res
    st.phase("talking to BazQux")
    st.folders = folder_map(ctx.client)
    refs = ctx.client.unread_ids()
    st.unread_now = {r.id for r in refs}
    ctx.ref_time = {r.id: (r.timestamp_usec or 0) for r in refs}
    res.unread_upstream = len(st.unread_now)
    st.starred = set(ctx.client.starred_ids())
    st.protected = protected_ids(ctx)
    if st.protected is None and st.enforce:
        st.enforce = False
        res.notes.append("could not read the Must read stream; nothing cut this run")
    res.feedback = feedback.collect(ctx.client, st.conn, st.cfg, st.unread_now, st.starred)


def _fetch(st: _Run) -> None:
    """New articles with their text; the protection flags; the cuts a run before left half-done."""
    st.phase("fetching new articles")
    st.res.new = ingest(st.ctx, st.unread_now, st.folders)
    refresh_protection(st.ctx, st.protected, st.starred)
    cutmod.finish_pending_cuts(st.ctx.client, st.conn, st.cfg)


def _judge(st: _Run) -> None:
    """Collect the judgments the Batches API finished, then submit what is pending (batch
    mode) or judge it now (synchronous)."""
    ctx, res, conn = st.ctx, st.res, st.conn
    judge = st.judge = ctx.judge()
    if st.sync:
        judge.batch_mode = False
    if st.rejudge:
        judge.rejudge = (model_name(st.cfg.models.judge), 0.0)
        res.notes.append("re-judging what other models judged (--rejudge)")
    res.batch_mode = judge.batch_mode
    before = store.scored_ids(conn)
    collected = 0
    if store.open_batches(conn):
        # judgments submitted last time come back now, whatever mode this run is in: a batch
        # submitted before `models.batch` was turned off must not strand its articles
        st.phase("collecting finished batches")
        col = judge.collect(st.run_id, client=ctx.client, on_progress=st.live)
        res.scored, res.reviews, res.saved = col["scored"], col["second_looks"], col["saved"]
        res.notes.extend(col["notes"])
        if col["failed"]:
            res.notes.append(f"{col['failed']} review(s) failed; those articles stay unread")
        collected = col["scored"]
    if judge.batch_mode:
        _submit_batch(st, collected)
    else:
        _judge_now(st)
    st.newly_scored = store.scored_ids(conn) - before


def _submit_batch(st: _Run, collected: int) -> None:
    """Batch mode: what arrived since the last run goes out to the Batches API."""
    res, conn, judge = st.res, st.conn, st.judge
    assert judge is not None
    st.phase("submitting a batch")
    sub = judge.submit_pending(st.run_id, limit=st.score_limit)
    res.submitted, res.dupes = sub["submitted"], sub["dupes"]
    if sub["budget_stop"]:
        st.budget_stop = True
        res.notes.append("daily budget reached; the rest waits for tomorrow")
    if sub["waiting"]:
        res.notes.append(f"{sub['waiting']} articles wait for a fuller batch")
    res.pending = judge.pending_count(exclude_inflight=True)
    res.inflight = len(store.inflight_ids(conn, "teacher"))
    if collected or sub["submitted"]:
        log.info(
            "judge: judged %d, submitted %d, with the model %d, pending %d",
            collected,
            sub["submitted"],
            res.inflight,
            res.pending,
        )


def _judge_now(st: _Run) -> None:
    """Synchronous mode: every model call now, up to the limit and the deadline."""
    res, cfg, judge = st.res, st.cfg, st.judge
    assert judge is not None
    st.phase("judging")
    stats = judge.score_pending(
        st.run_id,
        limit=st.score_limit or cfg.limits.per_run_sync,
        workers=st.workers or cfg.limits.parallel_calls,
        on_progress=st.live,
        stop_after=st.stop_after,
    )
    if stats["stopped_early"]:
        res.notes.append("time limit near: the rest waits for the next run")
    res.scored += stats["scored"]
    res.dupes, res.pending = stats["dupes"], stats["pending_after"]
    if stats["budget_stop"]:
        st.budget_stop = True
        res.notes.append("daily budget reached; unscored articles stay unread")
    if stats["failed_batches"]:
        res.notes.append(f"{stats['failed_batches']} batch(es) failed and stay pending")
    if stats.get("halted"):
        res.notes.append(stats["halted"])


def _tag(st: _Run) -> None:
    """Every judged article carries its band in BazQux; stale bands from a re-score go."""
    assert st.judge is not None
    st.phase("tagging in BazQux")
    cutmod.untag_stale(st.ctx.client, st.judge.stale_tags)
    cutmod.tag_bands(st.ctx.client, st.conn)


def _autocut(st: _Run) -> None:
    """The automatic cut below the cut-off, with the reviews first; the prune the student
    asked for by tag. In scoring-only mode nothing is cut and the notes say so."""
    ctx, res, cfg, conn, judge = st.ctx, st.res, st.cfg, st.conn, st.judge
    assert judge is not None
    if not st.enforce:
        res.notes.append("scoring-only mode: nothing cut")
        if res.feedback.get("prune_requested"):
            res.notes.append("prune requested by tag; it waits for a run with autocut on")
        return
    if st.budget_stop:
        # a run the budget cut short is a half-judged run: the close calls could not have their
        # reviews, so nothing is cut on its scores; tomorrow's runs cut as usual
        res.notes.append("daily budget reached: nothing cut this run")
        return
    st.phase("autocut")
    if feedback.rescue_rate_week(conn) > cfg.safety.rescue_rate_week:
        res.notes.append(
            f"rescues above {cfg.safety.rescue_rate_week:.0%} this week; cut-off lowered to "
            f"{cfg.safety.lowered_cutoff}"
        )
        cfg.autocut.cutoff = min(cfg.autocut.cutoff, cfg.safety.lowered_cutoff)
    sel = cutmod.select_autocut(
        conn, cfg, judge, st.run_id, newly_scored=st.newly_scored, client=ctx.client, stop_after=st.stop_after
    )
    if sel.blocked:
        res.notes.append(sel.blocked)
    res.reviews += sel.reviews
    res.saved += sel.saved_by_review
    res.new_ground = sel.reserved_for_new_ground
    res.cut_autocut = cutmod.apply_cut(ctx.client, conn, cfg, sel.cut_ids, st.run_id, "autocut")
    res.boundary = sel.boundary
    if sel.reviews:  # a synchronous review that moved an article's band retags it now, not next run
        cutmod.untag_stale(ctx.client, judge.stale_tags)
        cutmod.tag_bands(ctx.client, conn)
    if sel.held:
        res.held = judge.submit_reviews(st.run_id, sel.held)
    if res.feedback.get("prune_requested") and sel.blocked:
        res.notes.append("prune requested by tag; it waits for `bodh safety reset`")
    elif res.feedback.get("prune_requested"):
        psel = cutmod.select_prune(conn, cfg)
        res.cut_prune = cutmod.apply_cut(ctx.client, conn, cfg, psel.cut_ids, st.run_id, "prune")
        res.boundary = psel.boundary
        feedback.prune_request_done(conn)
        res.notes.append("prune tag seen: pruned to target")


def _finish(st: _Run) -> None:
    """The run row, the dashboard, the heartbeat."""
    ctx, res, cfg, conn = st.ctx, st.res, st.cfg, st.conn
    if res.feedback.get("suspect_flip"):
        res.notes.append(
            f"{res.feedback['suspect_flip']} unread articles gone upstream at once: not recorded "
            "as read until the next run sees the same"
        )
    res.spend_usd = store.spent_since(conn, store.local_midnight_iso(cfg.timezone))
    store.finish_run(
        conn,
        st.run_id,
        True,
        unread_upstream=res.unread_upstream,
        new=res.new,
        scored=res.scored,
        second_looks=res.reviews,
        cut_autocut=res.cut_autocut,
        cut_prune=res.cut_prune,
        rescues=res.feedback.get("rescues", 0),
        reads=res.feedback.get("reads", 0),
        spend_usd=res.spend_usd,
        boundary=res.boundary,
        summary=res.summary(),
    )
    st.phase("rendering the dashboard")
    render_dashboard(cfg, conn, res)
    st.phase("backing up")
    recovery.backup_daily(conn, cfg)  # once a day; a failure is a log line
    st.phase("")
    log.info(
        "run %d: %s", st.run_id, res.summary()
    )  # the same line the heartbeat, the run row and the dashboard carry
    ctx.heartbeat.ok(res.summary())


def render_dashboard(cfg: Config, conn: sqlite3.Connection, res: RunResult | None = None) -> None:
    try:
        dashboard.render(cfg, conn)
    except Exception:  # the dashboard is a view: a run that judged and cut has done its work without it
        log.exception("dashboard: render failed")
        if res is not None:
            res.notes.append("dashboard render failed")


def catch_up(
    ctx: Context,
    workers: int,
    chunk: int = 1000,
    all_ages: bool = False,
    budget: float | None = None,
    on_progress: Any = None,
    model: str | None = None,
    max_seconds: float | None = None,
    rejudge: bool = False,
) -> dict[str, Any]:
    """One deliberate pass over everything pending, as fast as the API allows: synchronous,
    many calls at once, round after round until nothing is pending. Scores and tags only;
    nothing is cut. Resumable: stop it and run it again. With ``model`` the quick judge (a faster
    one) judges what nobody has judged yet and leaves the judge's judgments alone; the
    judge later re-judges what the quick judge rated promising (catch_up.rejudge_at)."""
    cfg, conn = ctx.cfg, ctx.conn
    if all_ages:
        cfg.limits.max_age_days = None
    if budget is not None:
        cfg.models.daily_budget_usd = max(cfg.models.daily_budget_usd, budget)
    quick = bool(model) and not same_model(model, cfg.models.judge)
    if model:
        cfg.models.judge = model
    judge = ctx.judge()
    judge.batch_mode = False
    if quick:
        judge.rejudge = None
    elif rejudge:
        judge.rejudge = (
            model_name(cfg.models.judge),
            0.0,
        )  # everything another model judged, whatever it rated
    ctx.llm.retries(8)
    run_id = store.begin_run(conn, "catch-up")
    total = _catch_up_totals(run_id, judge.pending_count(exclude_inflight=True))
    deadline = total["started"] + max_seconds if max_seconds else None
    live = progress.live_writer(cfg, conn)
    current: dict[str, Any] = {}

    def tick(stats: dict[str, Any]) -> None:
        current.clear()
        current.update(stats)
        live(stats)
        if on_progress is not None:
            on_progress(total, stats)

    try:
        _catch_up_rounds(ctx, judge, run_id, total, current, chunk, workers, tick, deadline)
    except KeyboardInterrupt:
        total["interrupted"] = True
        if current:  # the round that was cut short still counts what landed
            total["rounds"] += 1
            total["scored"] += current.get("scored", 0)
            total["dupes"] += current.get("dupes", 0)
            total["failed"] += current.get("failed_batches", 0)
        total["pending"] = judge.pending_count(exclude_inflight=True)
    except Exception as exc:
        total["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _catch_up_close(ctx, judge, run_id, total)
    return total


def _catch_up_totals(run_id: int, pending: int) -> dict[str, Any]:
    return {
        "scored": 0,
        "dupes": 0,
        "failed": 0,
        "rounds": 0,
        "pending": pending,
        "budget_stop": False,
        "started": time.monotonic(),
        "interrupted": False,
        "run_id": run_id,
        "timed_out": False,
        "halted": "",
    }


def _catch_up_rounds(
    ctx: Context,
    judge: Judge,
    run_id: int,
    total: dict[str, Any],
    current: dict[str, Any],
    chunk: int,
    workers: int,
    tick: Any,
    deadline: float | None,
) -> None:
    """Round after round of ``chunk`` articles until nothing is pending, the budget is reached,
    the deadline passes, or the API keeps refusing."""
    cfg, conn = ctx.cfg, ctx.conn
    idle_rounds = 0
    while total["pending"] > 0:
        progress.set_phase(
            conn,
            f"catch-up round {total['rounds'] + 1}: judging {min(chunk, total['pending']):,} "
            f"of {total['pending']:,} pending",
        )
        progress.write_live(cfg, conn)
        current.clear()
        stats = judge.score_pending(
            run_id, limit=chunk, workers=workers, on_progress=tick, stop_after=deadline
        )
        current.clear()
        total["rounds"] += 1
        total["scored"] += stats["scored"]
        total["dupes"] += stats["dupes"]
        total["failed"] += stats["failed_batches"]
        total["pending"] = stats["pending_after"]
        if stats["budget_stop"]:
            total["budget_stop"] = True
            return
        if stats.get("halted"):  # five failed calls in a row: the provider is down; stop, do not spin
            total["halted"] = stats["halted"]
            return
        if stats["stopped_early"] or (deadline is not None and time.monotonic() > deadline):
            total["timed_out"] = True
            return
        if stats["scored"] == 0 and stats["dupes"] == 0:
            idle_rounds += 1
            if idle_rounds >= 2:
                return  # the API keeps refusing; stop rather than spin
            time.sleep(10)
        else:
            idle_rounds = 0


def _catch_up_close(ctx: Context, judge: Judge, run_id: int, total: dict[str, Any]) -> None:
    """Whatever happened: the tags, the run row with its summary, the dashboard."""
    cfg, conn = ctx.cfg, ctx.conn
    progress.set_phase(conn, "tagging in BazQux")
    progress.write_live(cfg, conn)
    try:
        cutmod.untag_stale(ctx.client, judge.stale_tags)
        cutmod.tag_bands(ctx.client, conn)
    except Exception:  # the judgments are stored; the next run tags what this one could not
        log.exception("catch-up: tagging failed; the next run finishes it")
    total["elapsed"] = time.monotonic() - total["started"]
    total["spend"] = store.spent_since(conn, store.local_midnight_iso(cfg.timezone))
    summary = (
        f"catch-up ({cfg.models.judge}): judged {total['scored']} (+{total['dupes']} dupes) in {total['rounds']} rounds, "
        f"{total['elapsed'] / 60:.1f} min, {total['failed']} failed batches, pending {total['pending']}"
        + (", interrupted" if total["interrupted"] else "")
        + (", time limit" if total["timed_out"] else "")
        + (", budget reached" if total["budget_stop"] else "")
        + (f", stopped: {total['halted']}" if total.get("halted") else "")
    )
    store.finish_run(
        conn,
        run_id,
        not total["interrupted"] and not total.get("error"),
        scored=total["scored"],
        spend_usd=total["spend"],
        summary=summary,
        error=total.get("error"),
    )
    render_dashboard(cfg, conn)
    progress.set_phase(conn, "")
    progress.write_live(cfg, conn)


def prune(
    ctx: Context,
    keep: int | None = None,
    fraction: float | None = None,
    below: float | None = None,
    dry_run: bool = False,
    confirmed: set[str] | None = None,
) -> tuple[int, cutmod.Selection]:
    """The student's cut. Refused while the safety limit is tripped: the scores it would rank by are
    the ones the safety limit declared suspect. ``confirmed`` (the ids shown and agreed to) bounds a
    real prune to them, so a run between the question and the answer cannot widen the cut."""
    cfg, conn = ctx.cfg, ctx.conn
    tripped = cutmod.safety_tripped(conn)
    if tripped:
        raise cutmod.SafetyTripped(
            f"the safety limit tripped in run {tripped}: the latest scores may be wrong; "
            "check a few with `bodh why ID`, then `bodh safety reset`"
        )
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
        store.finish_run(
            conn,
            run_id,
            True,
            cut_prune=n,
            boundary=sel.boundary,
            summary=f"prune: cut {n}, boundary {sel.boundary}",
        )
        return n, sel
    except KeyboardInterrupt:
        store.finish_run(
            conn,
            run_id,
            False,
            cut_prune=len(sel.cut_ids),
            boundary=sel.boundary,
            error="stopped; the next run finishes the tags and read marks, `bodh undo` reverses it",
        )
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
        "SELECT id FROM runs WHERE kind IN ('run','prune','clear-old','bankruptcy') "
        "AND id IN (SELECT DISTINCT cut_run_id FROM state WHERE cut_run_id IS NOT NULL) ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None, 0
    run_id = int(row[0])
    n = cutmod.undo(ctx.client, ctx.conn, ctx.cfg, run_id)
    undo_id = store.begin_run(ctx.conn, "undo")
    store.finish_run(ctx.conn, undo_id, True, summary=f"undo run {run_id}: {n} restored")
    return run_id, n


def clear_old(ctx: Context, older_than_days: int, dry_run: bool) -> tuple[int, int]:
    """A dry run counts and records nothing; a real one is a run of its own, undoable."""
    refusal = "could not read the Must read stream; clear-old refused rather than cut a protected article"
    if dry_run:
        protected = protected_ids(ctx)
        if protected is None:
            raise BazQuxError(refusal)
        return cutmod.clear_old(
            ctx.client, ctx.conn, ctx.cfg, older_than_days, protected, run_id=0, dry_run=True
        )
    run_id = store.begin_run(ctx.conn, "clear-old")
    try:
        protected = protected_ids(ctx)
        if protected is None:
            raise BazQuxError(refusal)
        candidates, done = cutmod.clear_old(ctx.client, ctx.conn, ctx.cfg, older_than_days, protected, run_id)
        store.finish_run(
            ctx.conn,
            run_id,
            True,
            cut_prune=done,
            summary=f"clear-old: {candidates} older than {older_than_days}d, {done} marked read",
        )
        return candidates, done
    except Exception as exc:
        store.finish_run(ctx.conn, run_id, False, error=f"{type(exc).__name__}: {exc}")
        raise
