"""The one implementation every surface calls: the command line, the console, a script, the
MCP server. `App(root)` opens a project; its methods mirror the command groups (set up, every
run, look, cut, teach, control, housekeeping) and return plain values and dataclasses, never
printed text. The command line holds argument parsing and printing only.

    from bodh.api import App
    app = App()                      # BODH_HOME, else the config.yaml above the working directory
    for row in app.reading_list(band=70):
        print(row.score, row.title)
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import cutting as cutmod
from . import dashboard, feedback, logs, profiler, recovery, report, store
from . import run as runmod
from .config import Config, Secrets, load_config, load_secrets
from .errors import BodhError
from .ids import normalize_item_id
from .judgment import canonical_url, normalize_flags
from .locks import graceful_sigterm, run_lock, time_limit

log = logging.getLogger(__name__)

SCHEMA = 1  # the version field of every JSON object a command prints (docs/json.md)
MARKS = ("great", "good", "meh", "bad", "basic", "heavy", "more", "less", "rescue")
LIST_BAND = 70.0
LIST_LIMIT = 50
FIND_LIMIT = 50


# -- the values ----------------------------------------------------------------------


@dataclass
class Hit:
    """One line of `bodh find`."""

    id: str
    title: str
    url: str
    feed: str
    folder: str | None
    score: float | None
    band: str | None
    status: str | None
    lesson: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ListRow:
    """One line of `bodh list`: an unread article worth the time, best first."""

    id: str
    title: str
    url: str
    feed: str
    folder: str | None
    score: float
    band: str
    lesson: str
    concept: str
    judged_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArticleView:
    """`bodh why`: one article, its judgment, its state and the student's record."""

    id: str
    title: str
    url: str
    feed: str
    folder: str | None
    judged: bool
    score: float | None = None
    base_score: float | None = None
    review_score: float | None = None
    quality: int | None = None
    learning: int | None = None
    fit: int | None = None
    breadth: int | None = None
    relevance: int | None = None
    confidence: str | None = None
    kind: str | None = None
    flags: list[str] = field(default_factory=list)
    lesson: str | None = None
    concept: str | None = None
    judged_by: str | None = None
    prompt_version: str | None = None
    band: str | None = None
    status: str | None = None
    cut_mode: str | None = None
    cut_at: str | None = None
    cut_run_id: int | None = None
    starred: bool = False
    protected: bool = False
    new_ground: bool = False
    record: list[dict[str, str]] = field(default_factory=list)  # the student's marks: kind, at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TagInfo:
    """One tag of the vocabulary: who writes it and what it does."""

    tag: str
    group: str  # band, cut, command, grade, level, steer, stream
    writer: str  # "bodh" or "you"
    meaning: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CatchUpPlan:
    """What a catch-up would do, for the confirmation."""

    pending: int
    judge: str
    quick: bool
    estimate_usd: float
    budget_left: float
    budget_needed: float
    rejudge_at: float | None
    all_ages: bool

    @property
    def over_budget(self) -> bool:
        return self.estimate_usd > self.budget_left


@dataclass
class PrunePlan:
    """`bodh prune --dry-run`: the cut-off, the counts by band and by folder, the ten best that
    would go with their lessons, and the ids the real prune is bound to."""

    cut: int
    unread_scored: int
    boundary: float | None
    by_band: dict[str, int]
    by_folder: dict[str, int]
    best: list[ListRow]
    cut_ids: list[str]
    next_run_id: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "cut": self.cut,
            "unread_scored": self.unread_scored,
            "boundary": self.boundary,
            "by_band": self.by_band,
            "by_folder": self.by_folder,
            "best": [r.to_dict() for r in self.best],
            "next_run_id": self.next_run_id,
        }


def with_schema(payload: dict[str, Any]) -> dict[str, Any]:
    """The object a `--json` command prints: the version first, then the data."""
    return {"schema": SCHEMA, **payload}


def _json_default(value: Any) -> str:
    """Dates and times as ISO 8601 (UTC-aware where the value is), paths and the rest as text."""
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def dumps(payload: Any) -> str:
    return json.dumps(payload, default=_json_default, ensure_ascii=False, indent=2)


# -- the application ---------------------------------------------------------------


class App:
    """A project, opened once: its configuration, secrets and database, and every operation.

    `root` is the project folder (with `config.yaml`); None resolves it the way the command
    line does. `verbose` puts debug lines on the terminal as well as in the log file.
    A test may pass `cfg`, `secrets` and `conn` ready-made."""

    def __init__(
        self,
        root: Path | None = None,
        verbose: bool = False,
        *,
        cfg: Config | None = None,
        secrets: Secrets | None = None,
        conn: sqlite3.Connection | None = None,
        log_to_file: bool = True,
    ) -> None:
        self.cfg = cfg or load_config(root)
        self.secrets = secrets or load_secrets(self.cfg.root)
        if log_to_file:
            logs.setup_logging(self.cfg.root / self.cfg.data_dir, verbose, secrets=self.secrets.values())
        self._conn = conn
        self._ctx: runmod.Context | None = None

    @property
    def root(self) -> Path:
        return self.cfg.root

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = store.connect(self.cfg.db_path)
        return self._conn

    @property
    def ctx(self) -> runmod.Context:
        if self._ctx is None:
            self._ctx = runmod.Context(self.cfg, self.secrets, self.conn)
        return self._ctx

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None
            self._ctx = None

    def __enter__(self) -> App:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextlib.contextmanager
    def lock(self, what: str, timeout: int | None = None) -> Iterator[None]:
        """The run lock (one bodh at a time per project) with SIGTERM turned into Ctrl-C, and a
        time limit when the caller sets one. Raises `LockedError` (exit 5) when held."""
        with contextlib.ExitStack() as stack:
            stack.enter_context(run_lock(self.cfg.root / self.cfg.data_dir, what))
            stack.enter_context(graceful_sigterm())
            if timeout:
                stack.enter_context(time_limit(timeout))
            yield

    # -- set up ---------------------------------------------------------------------------

    def doctor(
        self, live: bool = False, values: bool = False, say: Callable[[str], None] | None = None
    ) -> Any:
        from . import doctor as doctormod

        return doctormod.check(self.cfg, self.secrets, self.conn, live=live, values=values, say=say)

    def keys(self) -> list[dict[str, Any]]:
        """Per provider: the variable it needs and whether it is set; never a value."""
        from .llm import secret_field
        from .providers.base import PROVIDERS, REQUIRED_SECRET

        return [
            {
                "provider": name,
                "variable": REQUIRED_SECRET[name],
                "set": self.secrets.is_set(secret_field(name)),
                "optional": [PROVIDERS[name]] if name == "openai-compatible" else [],
            }
            for name in PROVIDERS
        ]

    def set_key(self, provider: str, value: str, keychain: bool = False) -> str:
        """Store a provider's key (or a local service's URL) in `.env` (owner-only) or the
        keychain; returns where it went. The value is never logged."""
        from .config import keychain_set, write_env_value
        from .providers.base import PROVIDERS

        if provider not in PROVIDERS:
            raise BodhError(f"unknown provider {provider!r}; one of {', '.join(PROVIDERS)}")
        env_name = PROVIDERS[provider]
        if keychain:
            keychain_set(env_name, value)
            return f"{env_name}: stored in the keychain ({len(value)} characters)"
        path = write_env_value(self.cfg.root, env_name, value)
        return f"{env_name}: written to {path} ({len(value)} characters, owner-only)"

    def set_env_value(self, env_name: str, value: str) -> Path:
        from .config import write_env_value

        return write_env_value(self.cfg.root, env_name, value)

    def models_text(self) -> str:
        from . import comparing

        return comparing.render_models(self.cfg, self.secrets)

    def compare_plan(self, with_model: str, sample: int) -> dict[str, Any]:
        from . import comparing

        return comparing.plan(self.ctx, with_model, sample)

    def compare(self, with_model: str, sample: int, on_progress: Any = None) -> dict[str, Any]:
        from . import comparing

        with self.lock("compare"):
            return comparing.compare(self.ctx, with_model, sample, on_progress=on_progress)

    # -- every run ------------------------------------------------------------------------

    def run(
        self,
        shadow: bool = False,
        limit: int | None = None,
        sync: bool = False,
        workers: int | None = None,
        rejudge: bool = False,
    ) -> runmod.RunResult:
        """One full pass, what the schedule runs every 30 minutes. Raises `LockedError` when
        another run holds the lock."""
        with self.lock("run", timeout=runmod.RUN_TIMEOUT_SECONDS):
            return runmod.run(
                self.ctx,
                score_limit=limit,
                enforce=(self.cfg.autocut.enabled and not shadow),
                workers=workers,
                sync=sync,
                rejudge=rejudge,
            )

    def catch_up_plan(
        self,
        model: str | None = None,
        all_ages: bool = False,
        budget: float | None = None,
        rejudge: bool = False,
    ) -> CatchUpPlan:
        """How many articles a catch-up would judge, with which model, and what it would cost."""
        from .judge import Judge
        from .providers.base import model_name, same_model

        cfg = self.cfg
        self.cfg.check_about()
        judge = model or (cfg.models.judge if rejudge else cfg.catch_up.model)
        quick = not same_model(judge, cfg.models.judge)
        if rejudge and quick:
            raise BodhError(
                f"--rejudge is the judge's pass ({cfg.models.judge}): drop --model, or name the judge"
            )
        rule = None if quick else Judge.rejudge_rule(cfg)
        if rejudge and not quick:
            rule = (model_name(judge), 0.0)
        pending = store.unscored_unread_count(
            self.conn,
            cfg.judge.prompt_version,
            None if all_ages else cfg.limits.max_age_days,
            cfg.unjudged_folders(),
            exclude_inflight=True,
            rejudge=rule,
        )
        if pending and not cfg.models.has_price(judge):
            raise BodhError(f"no price for {judge} in models.prices_per_mtok; add it to config.yaml first")
        p_in, p_out = cfg.models.price(judge) if pending else (0.0, 0.0)
        est = pending * (450 * p_in + 65 * p_out) / 1_000_000 * 1.1  # synchronous, prefix cached
        spent = store.spent_since(self.conn, store.local_midnight_iso(cfg.timezone))
        cap = max(cfg.models.daily_budget_usd, budget) if budget is not None else cfg.models.daily_budget_usd
        left = cap - spent
        need = max(cfg.models.daily_budget_usd, spent + est + 1)  # what --budget must say for the whole pass
        return CatchUpPlan(
            pending=pending,
            judge=judge,
            quick=quick,
            estimate_usd=est,
            budget_left=left,
            budget_needed=need,
            rejudge_at=cfg.catch_up.rejudge_at,
            all_ages=all_ages,
        )

    def catch_up(
        self,
        workers: int = 8,
        chunk: int = 1000,
        all_ages: bool = False,
        budget: float | None = None,
        model: str | None = None,
        max_seconds: float | None = None,
        rejudge: bool = False,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        judge = model or (self.cfg.models.judge if rejudge else self.cfg.catch_up.model)
        with self.lock("catch-up"):
            return runmod.catch_up(
                self.ctx,
                workers=workers,
                chunk=chunk,
                all_ages=all_ages,
                budget=budget,
                on_progress=on_progress,
                model=judge,
                max_seconds=max_seconds,
                rejudge=rejudge,
            )

    # -- look -----------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        from . import jobs

        return jobs.status(self.cfg, self.conn)

    def progress(self, log_lines: int = 10) -> dict[str, Any]:
        from . import progress as progressmod

        snap = progressmod.snapshot(self.conn, self.cfg)
        snap["log"] = progressmod.log_tail(logs.log_file(self.cfg.root / self.cfg.data_dir), log_lines)
        return snap

    def resolve(self, ref: str) -> str | None:
        """An article id (either form) or its URL, to the id bodh knows; None when unknown."""
        ref = ref.strip()
        if "://" in ref:
            row = self.conn.execute(
                "SELECT id FROM articles WHERE url = ? OR url = ?", (ref, ref.rstrip("/"))
            ).fetchone()
            if row is not None:
                return str(row[0])
            wanted = canonical_url(ref)
            tail = wanted.split("://", 1)[-1]
            for row in self.conn.execute("SELECT id, url FROM articles WHERE url LIKE ?", (f"%{tail}%",)):
                if canonical_url(str(row["url"] or "")) == wanted:
                    return str(row["id"])
            return None
        try:
            aid = normalize_item_id(ref)
        except ValueError:
            aid = ref
        row = self.conn.execute("SELECT id FROM articles WHERE id = ?", (aid,)).fetchone()
        return str(row[0]) if row is not None else None

    def why(self, ref: str) -> ArticleView | None:
        aid = self.resolve(ref)
        row = store.article(self.conn, aid) if aid else None
        if row is None:
            return None
        return article_view(self.cfg, row, store.feedback_for(self.conn, str(row["id"])))

    def find(self, words: str, unread: bool = False, limit: int = FIND_LIMIT) -> list[Hit]:
        """Articles whose title or lesson holds every word, best score first."""
        terms = [w for w in words.split() if w]
        if not terms:
            return []
        where = " AND ".join("(a.title LIKE ? OR COALESCE(s.lesson, '') LIKE ?)" for _ in terms)
        params: list[Any] = []
        for term in terms:
            params += [f"%{term}%", f"%{term}%"]
        if unread:
            where += " AND st.status = 'unread'"
        rows = self.conn.execute(
            f"""SELECT a.id, a.title, a.url, a.feed_title, a.folder, s.score, st.status, s.lesson
                FROM articles a LEFT JOIN scores s ON s.article_id = a.id LEFT JOIN state st ON st.article_id = a.id
                WHERE {where}
                ORDER BY (s.score IS NULL), s.score DESC, a.crawled_msec DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [
            Hit(
                id=str(r["id"]),
                title=str(r["title"] or ""),
                url=str(r["url"] or ""),
                feed=str(r["feed_title"] or ""),
                folder=r["folder"],
                score=float(r["score"]) if r["score"] is not None else None,
                band=self.cfg.tags.band_for(float(r["score"])) if r["score"] is not None else None,
                status=r["status"],
                lesson=str(r["lesson"] or ""),
            )
            for r in rows
        ]

    def reading_list(
        self, band: float = LIST_BAND, folder: str | None = None, limit: int = LIST_LIMIT
    ) -> list[ListRow]:
        """Today's reading list: unread articles at `band` and above, best first."""
        where = "st.status = 'unread' AND s.score >= ? AND s.model != 'dedup'"
        params: list[Any] = [band]
        if folder:
            where += " AND a.folder = ?"
            params.append(folder)
        rows = self.conn.execute(
            f"""SELECT a.id, a.title, a.url, a.feed_title, a.folder, s.score, s.lesson, s.concept, s.scored_at
                FROM state st JOIN scores s ON s.article_id = st.article_id JOIN articles a ON a.id = st.article_id
                WHERE {where} ORDER BY s.score DESC, a.crawled_msec DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [list_row(self.cfg, r) for r in rows]

    def tags(self) -> list[TagInfo]:
        """The vocabulary: every tag bodh writes or reads, and what each does."""
        t = self.cfg.tags
        out = []
        for threshold in sorted(t.bands, reverse=True):
            out.append(
                TagInfo(t.bands[threshold], "band", "bodh", f"the judge's score is {threshold} or more")
            )
        out.append(
            TagInfo(
                t.cut,
                "cut",
                "bodh",
                "bodh marked it read (autocut, a prune or clear-old); `bodh undo` reverses the last cut",
            )
        )
        out.append(
            TagInfo(
                t.rescue,
                "command",
                "you",
                "a cut article you wanted: back to unread, never cut again (the run removes the tag; `wrong` still works)",
            )
        )
        out.append(
            TagInfo(
                t.prune,
                "command",
                "you",
                f"on any article: the next run with autocut prunes to prune.keep ({self.cfg.prune.keep})",
            )
        )
        meanings = {
            "great": "the best kind: more like this; the eval gate protects it",
            "good": "worth the time",
            "meh": "not worth it",
            "bad": "junk; the judge was wrong to keep it",
            "basic": "too elementary for you",
            "heavy": "too dense or too specialist for you now",
            "more": "more of this article's concept, from the next run",
            "less": "less of it",
        }
        for kind, name in t.verdict_tags().items():
            group = "grade" if kind in store.GRADES else "level" if kind in store.LEVELS else "steer"
            out.append(TagInfo(name, group, "you", meanings[kind]))
        out.append(
            TagInfo(
                self.cfg.protect.must_read_stream,
                "stream",
                "you",
                "a smart stream in BazQux (a saved search); its unread members are never cut",
            )
        )
        return out

    # -- cut ------------------------------------------------------------------------------

    def prune_plan(
        self, keep: int | None = None, fraction: float | None = None, below: float | None = None
    ) -> PrunePlan:
        """What a prune would cut, without cutting. Refused while the safety limit is tripped."""
        with self.lock("prune"):
            _, sel = runmod.prune(self.ctx, keep=keep, fraction=fraction, below=below, dry_run=True)
        return prune_plan(self.cfg, self.conn, sel)

    def prune(
        self,
        keep: int | None = None,
        fraction: float | None = None,
        below: float | None = None,
        confirmed: set[str] | None = None,
    ) -> tuple[int, cutmod.Selection]:
        """The cut itself, bounded to `confirmed` (the ids shown and agreed to) when given."""
        with self.lock("prune"):
            return runmod.prune(self.ctx, keep=keep, fraction=fraction, below=below, confirmed=confirmed)

    def undo(self, run_id: int | None = None) -> tuple[int | None, int]:
        """Reverse the last cut, or the cut of one run: (run id, articles restored); (None, 0)
        when there is nothing to undo."""
        with self.lock("undo"):
            if run_id is None:
                return runmod.undo_last(self.ctx)
            row = self.conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise BodhError(f"no run {run_id}")
            n = cutmod.undo(self.ctx.client, self.conn, self.cfg, run_id)
            if n:
                undo_id = store.begin_run(self.conn, "undo")
                store.finish_run(self.conn, undo_id, True, summary=f"undo run {run_id}: {n} restored")
            return run_id, n

    def clear_old_count(self, days: int) -> int:
        candidates, _ = runmod.clear_old(self.ctx, days, dry_run=True)
        return candidates

    def clear_old(self, days: int) -> tuple[int, int]:
        with self.lock("clear-old"):
            return runmod.clear_old(self.ctx, days, dry_run=False)

    def next_run_id(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM runs").fetchone()
        return int(row[0])

    # -- teach ----------------------------------------------------------------------------

    def mark(self, ref: str, verdict: str) -> str:
        """Your verdict on an article (by id or URL), the same as its tag in BazQux."""
        if verdict not in MARKS:
            raise BodhError("the verdict must be one of " + ", ".join(MARKS))
        aid = self.resolve(ref)
        if aid is None:
            if "://" not in ref:
                try:
                    normalize_item_id(ref)
                except ValueError:
                    raise BodhError(
                        f"not an article id: {ref!r} (the long tag:google.com form, the signed number, or the URL)"
                    ) from None
            raise BodhError("unknown article; it must have been fetched by a run first")
        row = store.article(self.conn, aid)
        assert row is not None
        with self.lock("mark"):
            if verdict == "rescue":
                return feedback.rescue_one(self.ctx.client, self.conn, self.cfg, aid, row)
            if verdict in store.STEERS:
                store.set_verdict(self.conn, aid, verdict, store.STEERS)
                phrase = feedback.steer_phrase(self.conn, aid)
                profiler.steers(self.cfg, self.conn, [("want" if verdict == "more" else "meh", phrase)])
                return f"recorded {verdict} for: {row['title'][:70]} (steer: {phrase})"
            group = store.GRADES if verdict in store.GRADES else store.LEVELS
            store.set_verdict(self.conn, aid, verdict, group)
            return f"recorded {verdict} for: {row['title'][:70]}"

    def steer(self, direction: str, phrase: str) -> str:
        if direction not in ("more", "less"):
            raise BodhError("say more or less")
        with self.lock("steer"):  # learned.md is written: not under a run
            return profiler.steer(self.cfg, self.conn, "want" if direction == "more" else "meh", phrase)

    def profile(self) -> tuple[str, str]:
        """(about, learned): the student profile's two parts."""
        return self.cfg.read_about(), self.cfg.read_learned()

    def profile_revert(self) -> int | None:
        with self.lock("revert"):
            return profiler.revert(self.cfg, self.conn)

    def eval(self, **kwargs: Any) -> dict[str, Any]:
        from . import evals

        return evals.run_eval(self.ctx, **kwargs)

    # -- control --------------------------------------------------------------------------

    def stop(self, everything: bool = False) -> list[str]:
        from . import jobs
        from .locks import stop_process

        if everything:
            return jobs.stop_all(self.cfg)
        return [stop_process(self.cfg.root / self.cfg.data_dir)]

    def schedule(self, action: str, job: str = "all") -> str:
        from . import jobs

        return jobs.schedule(action, job, self.cfg.root / self.cfg.data_dir)

    def set_autocut(self, on: bool) -> str:
        from . import jobs

        ok, msg = jobs.set_autocut(self.cfg.root, on)
        if not ok:
            raise BodhError(msg)
        return msg

    # -- housekeeping ---------------------------------------------------------------------

    def backup(self, keep: int = recovery.BACKUPS_KEPT) -> Path:
        return recovery.backup(self.conn, self.cfg, keep=keep)

    def backups(self) -> list[Path]:
        return recovery.backups(self.cfg)

    def rebuild(self, dry_run: bool = False) -> dict[str, Any]:
        with self.lock("rebuild"):
            return recovery.rebuild(self.ctx, dry_run=dry_run)

    def weekly(self, dry_run: bool = False, say: Callable[[str], None] | None = None) -> dict[str, Any]:
        from . import weekly as weeklymod

        with self.lock("weekly"):
            return weeklymod.weekly(self.ctx, dry_run=dry_run, say=say)

    def report(self, days: int = 30) -> Path:
        return report.render(self.cfg, self.conn, days=days)

    def dashboard(self) -> Path:
        return dashboard.render(self.cfg, self.conn)


# -- the shapes ------------------------------------------------------------------------


def article_view(cfg: Config, row: sqlite3.Row, marks: list[tuple[str, str]]) -> ArticleView:
    judged = row["score"] is not None
    view = ArticleView(
        id=str(row["id"]),
        title=str(row["title"] or ""),
        url=str(row["url"] or ""),
        feed=str(row["feed_title"] or ""),
        folder=row["folder"],
        judged=judged,
        status=row["status"],
        cut_mode=store.cut_mode_name(row["cut_mode"]) if row["cut_mode"] else None,
        cut_at=row["cut_at"],
        cut_run_id=int(row["cut_run_id"]) if row["cut_run_id"] is not None else None,
        starred=bool(row["starred"]),
        protected=bool(row["protected"]),
        new_ground=bool(row["exploration"]),
        record=[{"kind": store.feedback_kind_name(kind), "at": at} for kind, at in marks],
    )
    if judged:
        try:
            flags = normalize_flags(json.loads(row["flags"] or "[]"))
        except (TypeError, ValueError):
            flags = [str(row["flags"])]
        view.score = float(row["score"])
        view.base_score = float(row["base_score"]) if row["base_score"] is not None else None
        view.review_score = float(row["second_look_score"]) if row["second_look_score"] is not None else None
        for name in ("quality", "learning", "fit", "breadth", "relevance"):
            setattr(view, name, int(row[name]) if row[name] is not None else None)
        view.confidence = row["confidence"]
        view.kind = row["kind"]
        view.flags = flags
        view.lesson = row["lesson"]
        view.concept = row["concept"]
        view.judged_by = row["model"]
        view.prompt_version = row["prompt_version"]
        view.band = cfg.tags.band_for(float(row["score"]))
    return view


def list_row(cfg: Config, r: sqlite3.Row | dict[str, Any]) -> ListRow:
    score = float(r["score"])
    return ListRow(
        id=str(r["id"]),
        title=str(r["title"] or ""),
        url=str(r["url"] or ""),
        feed=str(r["feed_title"] or ""),
        folder=r["folder"],
        score=score,
        band=cfg.tags.band_for(score),
        lesson=str(r["lesson"] or ""),
        concept=str(r["concept"] or ""),
        judged_at=r["scored_at"] if "scored_at" in r.keys() else None,  # noqa: SIM118  # a Row has no __contains__
    )


def prune_plan(cfg: Config, conn: sqlite3.Connection, sel: cutmod.Selection) -> PrunePlan:
    by_band: dict[str, int] = {}
    by_folder: dict[str, int] = {}
    for r in sel.cut:
        band = cfg.tags.band_for(float(r["score"]))
        by_band[band] = by_band.get(band, 0) + 1
        folder = str(r["folder"] or "(no folder)")
        by_folder[folder] = by_folder.get(folder, 0) + 1
    best = sorted(sel.cut, key=lambda r: -float(r["score"]))[:10]
    row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM runs").fetchone()
    return PrunePlan(
        cut=len(sel.cut),
        unread_scored=len(sel.cut) + len(sel.survivors),
        boundary=sel.boundary,
        by_band={b: by_band[b] for b in sorted(by_band, key=lambda b: -cfg_band_threshold(cfg, b))},
        by_folder=dict(sorted(by_folder.items(), key=lambda kv: (-kv[1], kv[0]))),
        best=[list_row(cfg, r) for r in best],
        cut_ids=sel.cut_ids,
        next_run_id=int(row[0]),
    )


def cfg_band_threshold(cfg: Config, band: str) -> int:
    for threshold, name in cfg.tags.bands.items():
        if name == band:
            return threshold
    return -1
