"""`bodh doctor`: configuration and connectivity checks, one line each, the problems collected.
`check` is what the command, the API and the installer call; `say` receives every line as it is
found (a terminal prints it at once), `problems` is what came back wrong."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import recovery, store
from . import run as runmod
from .config import Config, Secrets
from .providers.base import REQUIRED_SECRET, ModelRef, model_name

PROBE = {
    "id": "probe",
    "feed_title": "doctor",
    "folder": None,
    "title": "A short note on why liquid cooling retrofits cost more than greenfield builds",
    "url": "https://example.com/probe",
    "author": "doctor",
    "published": None,
    "text": (
        "A probe article. Retrofitting an air-cooled hall for direct-to-chip liquid cooling means new "
        "manifolds, CDUs and leak detection in a live facility; greenfield designs size the plant once."
    ),
}


@dataclass
class Report:
    """Every line in order, and the ones that are problems."""

    lines: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    live_skipped: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems


def check(
    cfg: Config,
    secrets: Secrets,
    conn: sqlite3.Connection,
    live: bool = False,
    values: bool = False,
    say: Callable[[str], None] | None = None,
) -> Report:
    """The checks in order: configuration, the profile, the prompts, the secrets, the database,
    and with ``live`` BazQux and each role's model (a few cents). Live checks are skipped while
    an earlier check failed."""
    report = Report()

    def out(text: str) -> None:
        report.lines.append(text)
        if say is not None:
            say(text)

    def problem(text: str) -> None:
        report.problems.append(text)
        out(text)

    _config(cfg, out)
    if values:
        for key, value, source in cfg.values_in_effect():
            out(f"   {key} = {value!r}  ({source})")
    _profile(cfg, out, problem)
    _prompts(cfg, out, problem)
    secrets_check(secrets, cfg, out, problem)
    _database(cfg, conn, out, problem)
    if live and not report.problems:
        _live(cfg, secrets, conn, out, problem)
    elif live:
        report.live_skipped = True
        out("live checks skipped until the problems above are fixed")
    return report


def _config(cfg: Config, out: Callable[[str], None]) -> None:
    out(f"root: {cfg.root}")
    out(f"timezone: {cfg.timezone} (from {cfg.timezone_source})")
    out(
        f"models: judge={cfg.models.judge} reviewer={cfg.models.reviewer} profiler={cfg.models.profiler}"
        f" quick judge={cfg.catch_up.model} budget=${cfg.models.daily_budget_usd}/day"
    )
    mode = (
        "batch (half price; judgments arrive on the next run)"
        if cfg.models.batch
        else f"synchronous ({cfg.limits.parallel_calls} calls at once)"
    )
    out(
        f"mode: {mode} · prompt {cfg.judge.prompt_version} · {cfg.judge.articles_per_call} articles per call"
        f" · articles older than {cfg.limits.max_age_days} days are never judged"
    )
    out(
        f"autocut: {'on' if cfg.autocut.enabled else 'off (scoring-only mode)'}, cut-off {cfg.autocut.cutoff}, "
        f"at most {cfg.autocut.max_per_run}/run · prune keeps {cfg.prune.keep} · folders {list(cfg.folders) or 'none'}"
    )
    out(
        f"home angle: {cfg.judge.home_angle!r} (+{cfg.judge.home_bonus})"
        if cfg.judge.home_angle
        else "home angle: none (judge.home_angle)"
    )
    if cfg.old_keys:
        out(f"config.yaml: {len(cfg.old_keys)} old key name(s), still read for one release; rename them:")
        for move in cfg.old_keys:
            out(f"   {move}")


def _profile(cfg: Config, out: Callable[[str], None], problem: Callable[[str], None]) -> None:
    if cfg.about_is_example():
        problem(
            "about: STILL THE EXAMPLE (profile/about.md describes an invented student): replace every paragraph "
            "with your own and delete the comment at the top"
        )
    elif cfg.read_about():
        old = (
            " (as profile/seed.md, the old name; install.sh renames it)"
            if cfg.about_path.name == "seed.md"
            else ""
        )
        out("about: found" + old)
    else:
        problem(
            "about: MISSING (profile/about.md): the judge cannot judge for a student it knows nothing about"
        )
    out(f"learned profile: {'found' if cfg.read_learned() else 'missing (will start empty)'}")


def _prompts(cfg: Config, out: Callable[[str], None], problem: Callable[[str], None]) -> None:
    from .judge import PROMPTS_DIR, load_prompt

    for name in (cfg.judge.prompt_version, "review.v1", "profiler.v1"):
        try:
            load_prompt(name, cfg.root)
            out(f"prompt {name}: ok")
        except Exception as exc:  # whatever stopped the read: the line names it and doctor exits 1
            problem(f"prompt {name}: MISSING ({exc}; looked in {PROMPTS_DIR})")


def roles(cfg: Config | None) -> list[tuple[str, str]]:
    """(role, model) for the four roles, as configured."""
    if cfg is None:
        return [("judge", "claude-sonnet-5-5")]
    return [
        ("judge", cfg.models.judge),
        ("reviewer", cfg.models.reviewer),
        ("profiler", cfg.models.profiler),
        ("quick judge", cfg.catch_up.model),
    ]


def secrets_check(
    secrets: Secrets,
    cfg: Config | None,
    out: Callable[[str], None],
    problem: Callable[[str], None],
) -> None:
    """The BazQux login, one line per provider the roles use (set, or the variable to set), the
    heartbeat, and the models without a price."""
    from .llm import secret_field

    if secrets.is_set("bazqux_email") and secrets.is_set("bazqux_password"):
        out("bazqux credentials: set")
    else:
        problem("bazqux credentials: MISSING (BAZQUX_EMAIL, BAZQUX_PASSWORD in .env)")
    needed: dict[str, list[str]] = {}
    for role, model in roles(cfg):
        needed.setdefault(ModelRef.parse(model).provider, []).append(role)
    for provider, used_by in needed.items():
        name = REQUIRED_SECRET[provider]
        if secrets.is_set(secret_field(provider)):
            out(f"{provider} key: set ({', '.join(used_by)})")
        else:
            problem(f"{provider} key: MISSING ({name} in .env; used by the {', '.join(used_by)})")
    hb = "set" if secrets.is_set("healthcheck_url") else "not set (no dead-man's switch)"
    out(f"heartbeat url: {hb}")
    unpriced = [m for _r, m in roles(cfg) if cfg is not None and not cfg.models.has_price(m)]
    if unpriced:
        out(
            "unpriced models (charged as the dearest known until models.prices_per_mtok names them): "
            f"{sorted(set(unpriced))}"
        )


def _database(
    cfg: Config, conn: sqlite3.Connection, out: Callable[[str], None], problem: Callable[[str], None]
) -> None:
    articles = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    out(f"database: {cfg.db_path} ({articles} articles, schema v{store.schema_version(conn)})")
    for pragma in ("quick_check", "integrity_check"):
        verdict = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
        if verdict != "ok":
            problem(f"database {pragma}: {verdict} (restore data/bodh.db from a backup)")
            return
    out("database integrity: ok")
    kept = recovery.backups(cfg)
    out(f"backups: {len(kept)}" + (f", newest {kept[-1].name}" if kept else " (the run makes one a day)"))


# -- live ---------------------------------------------------------------------------


def _live(
    cfg: Config,
    secrets: Secrets,
    conn: sqlite3.Connection,
    out: Callable[[str], None],
    problem: Callable[[str], None],
) -> None:
    """Log in to BazQux and call each role's model once; every failure is a line, never a crash."""
    ctx = runmod.Context(cfg, secrets, conn)
    try:
        live_bazqux(ctx, out, problem)
    except Exception as exc:  # doctor reports, never crashes: the line carries the cause
        problem(f"bazqux: FAILED ({exc})")
    try:
        live_models(ctx, out, problem)
    except Exception as exc:  # the same: a rejected key or model id is a line, and exit 1
        problem(f"models: FAILED ({exc})")


def live_bazqux(ctx: runmod.Context, out: Callable[[str], None], problem: Callable[[str], None]) -> None:
    cfg = ctx.cfg
    ctx.client.user_info()
    out("bazqux login: ok")
    unread = ctx.client.unread_ids()
    out(f"bazqux unread: {len(unread)}")
    subs = ctx.client.subscriptions()
    folders = sorted({f for s in subs for f in s.folders})
    out(f"bazqux feeds: {len(subs)} in folders {folders}")
    tags = ctx.client.tags()
    out(f"bazqux labels (folders, tags, smart streams): {len(tags)}")
    if f"user/-/label/{cfg.protect.must_read_stream}" in tags:
        out(f"must-read stream '{cfg.protect.must_read_stream}': present")
    else:
        problem(
            f"must-read stream '{cfg.protect.must_read_stream}': not found (create it in BazQux, or name yours in "
            "protect.must_read_stream)"
        )
    unknown = [f for f in cfg.folders if f not in folders]
    if unknown:
        problem(f"config folders not in BazQux: {unknown}")
    if unread:
        items, _ = ctx.client.contents([r.id for r in unread[:3]])
        for it in items:
            out(f"   - {it.title[:70]} ({it.feed_title[:30]})")


def live_models(ctx: runmod.Context, out: Callable[[str], None], problem: Callable[[str], None]) -> None:
    """The judge and the reviewer on the probe article, then the profiler and the quick judge
    when they are models of their own; the spend of today at the end."""
    from .judge import Judge, concept_context

    cfg, conn = ctx.cfg, ctx.conn
    judge = Judge(cfg, ctx.llm, conn, context=concept_context(conn, cfg))
    verdicts = judge.judge_batch([PROBE], None)  # type: ignore[list-item]
    v = verdicts.get("probe")
    if v:
        out(
            f"judge ({cfg.models.judge}): ok, probe scored {v.quality}/{v.learning}/{v.fit}/{v.breadth}/{v.relevance}"
        )
    else:
        problem(f"judge ({cfg.models.judge}): call ok, no judgment parsed")
    second = judge.review(PROBE, None)  # type: ignore[arg-type]
    if second is not None:
        out(f"review ({cfg.models.reviewer}): ok")
    else:
        problem(f"review ({cfg.models.reviewer}): FAILED")
    _live_other_roles(ctx, judge, out, problem)
    spent = store.spent_since(conn, store.local_midnight_iso(cfg.timezone))
    out(f"spend today, this doctor run included: ${spent:.4f}")


def _live_other_roles(
    ctx: runmod.Context, judge: Any, out: Callable[[str], None], problem: Callable[[str], None]
) -> None:
    """A few tokens each, so a wrong id or a missing key shows here and not on Sunday or in a backlog."""
    cfg = ctx.cfg
    probed = {model_name(cfg.models.judge), model_name(cfg.models.reviewer)}
    if model_name(cfg.models.profiler) not in probed:
        text = ctx.llm.complete(
            cfg.models.profiler, "Answer with one word.", "Say ok.", None, "doctor", max_tokens=5
        )
        if text.strip():
            out(f"profiler ({cfg.models.profiler}): ok")
        else:
            problem(f"profiler ({cfg.models.profiler}): call ok, empty answer")
        probed.add(model_name(cfg.models.profiler))
    if model_name(cfg.catch_up.model) not in probed:
        quick_cfg = cfg.model_copy(deep=True)
        quick_cfg.models.judge = cfg.catch_up.model
        quick = type(judge)(quick_cfg, ctx.llm, ctx.conn, context=judge.context, purpose="doctor")
        if quick.judge_batch([PROBE], None).get("probe"):
            out(f"quick judge ({cfg.catch_up.model}): ok")
        else:
            problem(f"quick judge ({cfg.catch_up.model}): call ok, no judgment parsed")
