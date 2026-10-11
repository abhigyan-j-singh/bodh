"""bodh: keeps your BazQux unread list worth reading.

bodh doctor [--live]      check config, secrets, prompts; --live also logs in and calls the models
bodh run [--shadow]       the timer's job: feedback, new articles, judgments, tags, autocut, dashboard, heartbeat
bodh catch-up             one pass over everything pending with the quick judge; cuts nothing
bodh progress [--watch]   judged, with the model, pending, rate, ETA, by folder; batches; the log
bodh prune                cut to prune.keep (default 40); or --fraction 0.3; or --below 65
bodh undo                 reverse the last cut
bodh clear-old --days N   mark everything older than N days read, stars and protections exempt
bodh status | why ID      the whole system at a glance; why one article got its score
bodh console              the console in the browser: status, run, stop, prune, undo, log
bodh stop [--all]         stop what is running; --all stops everything incl. the schedule and the console
bodh schedule             show, pause, resume or kick the launchd jobs
bodh autocut on|off       the automatic cut below the cut-off, or scoring-only mode
bodh mark ID great|…      your verdict on an article; `rescue` brings a cut one back
bodh steer more|less "x"  more or less of a concept, from the next run
bodh weekly | report      Sunday's profile rewrite and note; the monthly feed report
"""

from __future__ import annotations

import functools
import json
import logging
import sys
from typing import Any

import typer

from . import __version__, dashboard, feedback, profiler, report, store
from . import cutting as cutmod
from . import run as runmod
from .config import Config, Secrets, load_config, load_secrets
from .errors import BodhError, ConfigError, LockedError
from .ids import normalize_item_id
from .locks import AlreadyRunning, graceful_sigterm, run_lock, time_limit
from .logs import setup_logging

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_show_locals=False)
log = logging.getLogger(__name__)

EXIT_CONFIG = 2  # no usable config.yaml
VERBOSE = typer.Option(False, "-v", "--verbose", help="Debug-level log lines in the terminal.")


def _config() -> Config:
    """config.yaml, or a one-line refusal: every command but `version` needs it."""
    try:
        return load_config()
    except ConfigError as exc:
        _fail(str(exc), EXIT_CONFIG)
        raise  # unreachable; keeps the type checker honest
    except Exception as exc:  # a YAML or validation error the loader did not translate: still exit 2
        _fail(f"config.yaml does not load: {exc}", EXIT_CONFIG)
        raise


def _ctx(verbose: bool = False) -> runmod.Context:
    cfg = _config()
    secrets = load_secrets(cfg.root)
    setup_logging(cfg.root / cfg.data_dir, verbose, secrets=secrets.values())
    conn = store.connect(cfg.db_path)
    return runmod.Context(cfg, secrets, conn)


def _fail(msg: str, code: int = 1) -> None:
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(code)


def _handled(fn: Any) -> Any:
    """Every command: an error bodh raised on purpose is one plain line on stderr and its exit
    code (2 configuration, 3 BazQux, 4 the model, 5 a run in progress, 1 anything else)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except LockedError as exc:
            _fail(str(exc) or "another run is in progress; `bodh stop` it or wait", exc.exit_code)
        except BodhError as exc:
            _fail(str(exc), exc.exit_code)

    return wrapper


@app.command()
@_handled
def version() -> None:
    typer.echo(f"bodh {__version__}")


@app.command()
@_handled
def doctor(
    live: bool = typer.Option(False, "--live", help="Also log in to BazQux and call both models."),
    values: bool = typer.Option(False, "--values", help="Print every setting in effect with its source."),
    verbose: bool = VERBOSE,
) -> None:
    """Connectivity and configuration checks. Run after every update. Exits 1 when a check
    fails, so an installer stops on a broken install."""
    cfg = _config()
    secrets = load_secrets(cfg.root)
    problems: list[str] = []

    def problem(text: str) -> None:
        problems.append(text)
        typer.echo(text)

    _doctor_config(cfg)
    if values:
        for key, value, source in cfg.values_in_effect():
            typer.echo(f"   {key} = {value!r}  ({source})")
    _doctor_profile(cfg, problem)
    _doctor_prompts(cfg, problem)
    _doctor_secrets(secrets, problem)
    conn = store.connect(cfg.db_path)
    _doctor_database(cfg, conn, problem)
    if live and not problems:
        _doctor_live(cfg, secrets, conn, problem)
    elif live:
        typer.echo("live checks skipped until the problems above are fixed")
    if problems:
        typer.echo(f"\n{len(problems)} problem(s); see above", err=True)
        raise typer.Exit(1)
    typer.echo("ok")


def _doctor_config(cfg: Config) -> None:
    typer.echo(f"root: {cfg.root}")
    typer.echo(f"timezone: {cfg.timezone} (from {cfg.timezone_source})")
    typer.echo(
        f"models: judge={cfg.models.judge} reviewer={cfg.models.reviewer} profiler={cfg.models.profiler}"
        f" quick judge={cfg.catch_up.model} budget=${cfg.models.daily_budget_usd}/day"
    )
    mode = (
        "batch (half price; judgments arrive on the next run)"
        if cfg.models.batch
        else f"synchronous ({cfg.limits.parallel_calls} calls at once)"
    )
    typer.echo(
        f"mode: {mode} · prompt {cfg.judge.prompt_version} · {cfg.judge.articles_per_call} articles per call"
        f" · articles older than {cfg.limits.max_age_days} days are never judged"
    )
    typer.echo(
        f"autocut: {'on' if cfg.autocut.enabled else 'off (scoring-only mode)'}, cut-off {cfg.autocut.cutoff}, "
        f"at most {cfg.autocut.max_per_run}/run · prune keeps {cfg.prune.keep} · folders {list(cfg.folders) or 'none'}"
    )
    typer.echo(
        f"home angle: {cfg.judge.home_angle!r} (+{cfg.judge.home_bonus})"
        if cfg.judge.home_angle
        else "home angle: none (judge.home_angle)"
    )
    if cfg.old_keys:
        typer.echo(
            f"config.yaml: {len(cfg.old_keys)} old key name(s), still read for one release; rename them:"
        )
        for move in cfg.old_keys:
            typer.echo(f"   {move}")


def _doctor_profile(cfg: Config, problem: Any) -> None:
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
        typer.echo("about: found" + old)
    else:
        problem(
            "about: MISSING (profile/about.md): the judge cannot judge for a student it knows nothing about"
        )
    typer.echo(f"learned profile: {'found' if cfg.read_learned() else 'missing (will start empty)'}")


def _doctor_prompts(cfg: Config, problem: Any) -> None:
    from .judge import PROMPTS_DIR, load_prompt

    for name in (cfg.judge.prompt_version, "review.v1", "profiler.v1"):
        try:
            load_prompt(name, cfg.root)
            typer.echo(f"prompt {name}: ok")
        except Exception as exc:  # whatever stopped the read: the line names it and doctor exits 1
            problem(f"prompt {name}: MISSING ({exc}; looked in {PROMPTS_DIR})")


def _doctor_secrets(secrets: Secrets, problem: Any) -> None:
    if secrets.is_set("bazqux_email") and secrets.is_set("bazqux_password"):
        typer.echo("bazqux credentials: set")
    else:
        problem("bazqux credentials: MISSING (BAZQUX_EMAIL, BAZQUX_PASSWORD in .env)")
    if secrets.is_set("anthropic_api_key"):
        typer.echo("anthropic key: set")
    else:
        problem("anthropic key: MISSING (ANTHROPIC_API_KEY in .env)")
    hb = "set" if secrets.is_set("healthcheck_url") else "not set (no dead-man's switch)"
    typer.echo(f"heartbeat url: {hb}")


def _doctor_database(cfg: Config, conn: Any, problem: Any) -> None:
    articles = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    typer.echo(f"database: {cfg.db_path} ({articles} articles, schema v{store.schema_version(conn)})")
    for pragma in ("quick_check", "integrity_check"):
        verdict = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
        if verdict != "ok":
            problem(f"database {pragma}: {verdict} (restore data/bodh.db from a backup)")
            return
    typer.echo("database integrity: ok")


def _doctor_live(cfg: Config, secrets: Secrets, conn: Any, problem: Any) -> None:
    ctx = runmod.Context(cfg, secrets, conn)
    try:
        ctx.client.user_info()
        typer.echo("bazqux login: ok")
        unread = ctx.client.unread_ids()
        typer.echo(f"bazqux unread: {len(unread)}")
        subs = ctx.client.subscriptions()
        folders = sorted({f for s in subs for f in s.folders})
        typer.echo(f"bazqux feeds: {len(subs)} in folders {folders}")
        tags = ctx.client.tags()
        typer.echo(f"bazqux labels (folders, tags, smart streams): {len(tags)}")
        want = f"user/-/label/{cfg.protect.must_read_stream}"
        if want in tags:
            typer.echo(f"must-read stream '{cfg.protect.must_read_stream}': present")
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
                typer.echo(f"   - {it.title[:70]} ({it.feed_title[:30]})")
    except Exception as exc:  # doctor reports, never crashes: the line carries the cause
        problem(f"bazqux: FAILED ({exc})")
    try:
        from .judge import Judge, concept_context

        judge = Judge(cfg, ctx.llm, conn, context=concept_context(conn, cfg))
        probe = {
            "id": "probe",
            "feed_title": "doctor",
            "folder": None,
            "title": "A short note on why liquid cooling retrofits cost more than greenfield builds",
            "url": "https://example.com/probe",
            "author": "doctor",
            "published": None,
            "text": "A probe article. Retrofitting an air-cooled hall for direct-to-chip liquid cooling means new manifolds, CDUs and leak detection in a live facility; greenfield designs size the plant once.",
        }
        verdicts = judge.judge_batch([probe], None)  # type: ignore[list-item]
        v = verdicts.get("probe")
        if v:
            typer.echo(
                f"judge ({cfg.models.judge}): ok, probe scored {v.quality}/{v.learning}/{v.fit}/{v.breadth}/{v.relevance}"
            )
        else:
            problem(f"judge ({cfg.models.judge}): call ok, no judgment parsed")
        second = judge.review(probe, None)  # type: ignore[arg-type]
        if second is not None:
            typer.echo(f"review ({cfg.models.reviewer}): ok")
        else:
            problem(f"review ({cfg.models.reviewer}): FAILED")
        typer.echo(
            f"spend today, this doctor run included: ${store.spent_since(conn, store.local_midnight_iso(cfg.timezone)):.4f}"
        )
    except Exception as exc:  # the same: a rejected key or model id is a line, and exit 1
        problem(f"models: FAILED ({exc})")


@app.command()
@_handled
def run(
    shadow: bool = typer.Option(
        False, "--shadow", help="Judge and tag, cut nothing: scoring-only mode for this run."
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Score (or submit) at most this many articles this run."
    ),
    sync: bool = typer.Option(
        False, "--sync", help="Wait for every model call instead of using the Batches API."
    ),
    workers: int | None = typer.Option(
        None, "--workers", help="Model calls running at once (--sync); default from config."
    ),
    verbose: bool = VERBOSE,
) -> None:
    """One full pass. This is what launchd runs every 30 minutes."""
    ctx = _ctx(verbose)
    try:
        with (
            run_lock(ctx.cfg.root / ctx.cfg.data_dir, "run"),
            graceful_sigterm(),
            time_limit(runmod.RUN_TIMEOUT_SECONDS),
        ):
            res = runmod.run(
                ctx,
                score_limit=limit,
                enforce=(ctx.cfg.autocut.enabled and not shadow),
                workers=workers,
                sync=sync,
            )
    except AlreadyRunning:
        typer.echo("another run is in progress; skipping")
        raise typer.Exit(0) from None
    except KeyboardInterrupt:
        _fail("stopped; the next run picks up where this one left off", 130)
        return
    except BodhError:
        raise
    except Exception as exc:  # a bug, not a refusal: one line here, the traceback is in the log
        _fail(f"run failed: {exc}", 1)
        return
    typer.echo(res.summary())


def _renamed(old: str, new: str) -> None:
    """The one line an old command name prints before running the new one (one release)."""
    typer.echo(
        f"note: `bodh {old}` is now `bodh {new}`; the old name goes away in the next release", err=True
    )


@app.command(hidden=True)
@_handled
def score(
    limit: int | None = typer.Option(None, "--limit"),
    sync: bool = typer.Option(False, "--sync"),
    workers: int | None = typer.Option(None, "--workers"),
    verbose: bool = VERBOSE,
) -> None:
    """The name before October 2026 of `run --shadow`."""
    _renamed("score", "run --shadow")
    run(shadow=True, limit=limit, sync=sync, workers=workers, verbose=verbose)


@app.command()
@_handled
def progress(
    watch: bool = typer.Option(False, "--watch", help="Redraw every --every seconds until Ctrl-C."),
    every: int = typer.Option(5, "--every", help="Seconds between redraws with --watch."),
    verbose: bool = VERBOSE,
) -> None:
    """How far the judge has got: judged, with the model, pending, by folder; rate and ETA while a
    run is going; batches; spend; the log."""
    import time

    from . import progress as progressmod
    from .logs import log_file

    cfg = _config()
    conn = store.connect(cfg.db_path)
    while True:
        snap = progressmod.snapshot(conn, cfg)
        snap["log"] = progressmod.log_tail(log_file(cfg.root / cfg.data_dir), 10)
        text = progressmod.render_text(snap)
        if watch:
            typer.echo("\033[2J\033[H" + text + f"\n\n(redraws every {every}s; Ctrl-C to stop)")
            try:
                time.sleep(max(2, every))
            except KeyboardInterrupt:
                raise typer.Exit(0) from None
        else:
            typer.echo(text)
            return


@app.command("catch-up")
@_handled
def catch_up(
    workers: int = typer.Option(8, "--workers", help="Model calls running at once."),
    chunk: int = typer.Option(1000, "--chunk", help="Articles per round."),
    all_ages: bool = typer.Option(
        False, "--all-ages", help="Also the unread articles older than limits.max_age_days."
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="The model to use; default catch_up.model, the quick "
        "judge. Name the judge itself for a full-price pass.",
    ),
    budget: float | None = typer.Option(
        None, "--budget", help="Raise today's budget to this many dollars for the catch-up."
    ),
    max_seconds: float | None = typer.Option(
        None, "--max-seconds", help="Start no new call after this long; run again to continue."
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation."),
    verbose: bool = VERBOSE,
) -> None:
    """One deliberate pass over everything pending, as fast as the API allows: synchronous, many
    calls at once, with the quick judge unless --model says otherwise. Judges and tags; cuts
    nothing; the judge reads again what the quick judge rates highly (catch_up.rejudge_at). Stop
    it with Ctrl-C and run it again to continue. Watch it with `bodh progress --watch`."""
    import time

    ctx = _ctx(verbose)
    if not verbose:  # the progress line owns the terminal; the log file keeps the detail
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.WARNING)
    cfg = ctx.cfg
    cfg.check_about()
    if all_ages:
        cfg.limits.max_age_days = None
    judge = model or cfg.catch_up.model
    pending = _catch_up_preview(ctx, judge, workers, all_ages, budget)
    if pending == 0:
        return
    if not yes and not typer.confirm("Start the catch-up?"):
        raise typer.Exit(0)
    started = time.monotonic()

    def show(total: dict[str, Any], stats: dict[str, Any]) -> None:
        done = total["scored"] + total["dupes"] + stats["scored"] + stats["dupes"]
        mins = max(1e-6, (time.monotonic() - started) / 60)
        rate = done / mins
        left_n = max(0, pending - done)
        eta = f"{left_n / rate:.0f} min" if rate > 0 else "–"
        typer.echo(
            f"\r  judged {done:,} of {pending:,} · {rate:.0f}/min · ETA {eta} · "
            f"failed batches {total['failed'] + stats['failed_batches']} · round {total['rounds'] + 1}   ",
            nl=False,
        )

    try:
        with run_lock(cfg.root / cfg.data_dir, "catch-up"), graceful_sigterm():
            total = runmod.catch_up(
                ctx,
                workers=workers,
                chunk=chunk,
                all_ages=all_ages,
                budget=budget,
                on_progress=show,
                model=judge,
                max_seconds=max_seconds,
            )
    except AlreadyRunning:
        _fail("another run is in progress; wait for it (or `bodh schedule pause`) and try again", 5)
        return
    typer.echo("")
    _catch_up_report(total)


def _catch_up_preview(
    ctx: runmod.Context, judge: str, workers: int, all_ages: bool, budget: float | None
) -> int:
    """How many articles are pending and what the pass will cost; 0 when there is nothing to do."""
    from .judge import Judge

    cfg = ctx.cfg
    quick = judge != cfg.models.judge
    rejudge = None if quick else Judge.rejudge_rule(cfg)
    pending = store.unscored_unread_count(
        ctx.conn,
        cfg.judge.prompt_version,
        cfg.limits.max_age_days,
        cfg.unjudged_folders(),
        exclude_inflight=True,
        rejudge=rejudge,
    )
    if pending == 0:
        typer.echo(
            "nothing pending" + ("" if all_ages else " within the age window (--all-ages for the rest)")
        )
        return 0
    if not cfg.models.has_price(judge):
        _fail(f"no price for {judge} in models.prices_per_mtok; add it to config.yaml first")
    p_in, p_out = cfg.models.price(judge)
    est = pending * (450 * p_in + 65 * p_out) / 1_000_000 * 1.1  # synchronous, prefix cached
    left = cfg.models.daily_budget_usd - store.spent_since(ctx.conn, store.local_midnight_iso(cfg.timezone))
    need = max(cfg.models.daily_budget_usd, cfg.models.daily_budget_usd - left + est + 1)
    again = (
        f" The judge reads again what it rates {cfg.catch_up.rejudge_at:.0f} or more, on the next runs."
        if quick and cfg.catch_up.rejudge_at is not None
        else ""
    )
    typer.echo(
        f"{pending:,} articles pending{' (any age)' if all_ages else ''}; about ${est:.2f} with {judge}, "
        f"{workers} calls at once.{again}"
    )
    if est > left and budget is None:
        typer.echo(
            f"today's budget has ${left:.2f} left; pass --budget {need:.0f} to let the catch-up finish"
        )
    return pending


def _catch_up_report(total: dict[str, Any]) -> None:
    typer.echo(
        f"judged {total['scored']:,} (+{total['dupes']:,} dupes) in {total['elapsed'] / 60:.1f} min over "
        f"{total['rounds']} rounds; {total['failed']} failed batches; pending {total['pending']:,}; "
        f"spend today ${total['spend']:.2f}"
    )
    if total["interrupted"]:
        typer.echo("stopped; what was judged stays. Run `bodh catch-up` again to continue.")
    if total["timed_out"]:
        typer.echo("time limit reached; run again to continue")
    if total["budget_stop"]:
        typer.echo("stopped at the daily budget; pass --budget to raise it for today")
    if total["failed"] and total["pending"]:
        typer.echo("some calls were refused (rate limits); run again, or with fewer --workers")


@app.command(hidden=True)
@_handled
def blast(
    workers: int = typer.Option(8, "--workers"),
    chunk: int = typer.Option(1000, "--chunk"),
    all_ages: bool = typer.Option(False, "--all-ages"),
    model: str | None = typer.Option(None, "--model"),
    budget: float | None = typer.Option(None, "--budget"),
    max_seconds: float | None = typer.Option(None, "--max-seconds"),
    yes: bool = typer.Option(False, "--yes"),
    verbose: bool = VERBOSE,
) -> None:
    """The name before October 2026 of `catch-up`."""
    _renamed("blast", "catch-up")
    catch_up(
        workers=workers,
        chunk=chunk,
        all_ages=all_ages,
        model=model,
        budget=budget,
        max_seconds=max_seconds,
        yes=yes,
        verbose=verbose,
    )


@app.command()
@_handled
def prune(
    keep: int | None = typer.Option(None, "--keep", help="Keep this many unread (default: prune.keep)."),
    fraction: float | None = typer.Option(None, "--fraction", help="Cut this share of what is unread."),
    below: float | None = typer.Option(None, "--below", help="Cut everything scoring under this."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the cut without making it."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation."),
    verbose: bool = VERBOSE,
) -> None:
    """Cut the worst of what is still unread. Default keeps prune.keep (40). Run again to cut
    deeper. Shows what would go and asks first, unless --yes or not in a terminal."""
    if sum(x is not None for x in (keep, fraction, below)) > 1:
        _fail("choose one of --keep, --fraction, --below")
    if keep is not None and keep < 0:
        _fail("--keep must be 0 or more")
    if fraction is not None and not 0 <= fraction <= 1:
        _fail("--fraction is a share between 0 and 1")
    ctx = _ctx(verbose)
    lock = ctx.cfg.root / ctx.cfg.data_dir
    try:
        with run_lock(lock, "prune"):
            _, sel = runmod.prune(ctx, keep=keep, fraction=fraction, below=below, dry_run=True)
        _show_prune(sel, dry_run=True)
        if dry_run or not sel.cut:
            return
        # the question is asked with the lock released: a run must not be skipped over a prompt
        if not yes and _interactive() and not typer.confirm(f"Cut these {len(sel.cut)} (undo reverses it)?"):
            raise typer.Exit(0)
        with run_lock(lock, "prune"), graceful_sigterm():
            n, sel = runmod.prune(ctx, keep=keep, fraction=fraction, below=below, confirmed=set(sel.cut_ids))
    except KeyboardInterrupt:
        _fail("stopped; the next run finishes the tags and read marks, `bodh undo` reverses the prune", 130)
        return
    if n:
        typer.echo(f"cut {n}; `bodh undo` reverses it (run {store.last_runs(ctx.conn, 1)[0]['id']})")


def _interactive() -> bool:
    """Someone is at the keyboard (so a question can be asked); a script or launchd is not."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _show_prune(sel: Any, dry_run: bool) -> None:
    unread_scored = len(sel.cut) + len(sel.survivors)
    verb = "would cut" if dry_run else "cut"
    typer.echo(
        f"{verb} {len(sel.cut)} of {unread_scored} scored unread articles; boundary {sel.boundary:.0f}"
        if sel.boundary is not None
        else f"{verb} {len(sel.cut)} articles"
    )
    if sel.cut:
        best = sorted(sel.cut, key=lambda r: -float(r["score"]))[:10]
        typer.echo("the best of what goes (this is the boundary you are choosing):")
        for r in best:
            typer.echo(f"  {r['score']:5.1f}  {(r['title'] or '')[:70]}  [{(r['feed_title'] or '')[:25]}]")


@app.command()
@_handled
def undo(verbose: bool = VERBOSE) -> None:
    """Reverse the last cut: mark its articles unread again and remove the cut tag."""
    ctx = _ctx(verbose)
    with run_lock(ctx.cfg.root / ctx.cfg.data_dir, "undo"), graceful_sigterm():
        run_id, n = runmod.undo_last(ctx)
    if run_id is None:
        typer.echo("nothing to undo")
    else:
        typer.echo(f"restored {n} articles from run {run_id}")


@app.command("clear-old")
@_handled
def clear_old(
    days: int = typer.Option(14, "--days", help="Everything unread older than this many days goes."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count, change nothing."),
    verbose: bool = VERBOSE,
) -> None:
    """Mark every unread article older than N days read and tag it `cut`. Stars, the must-read
    stream, protected folders and rescued articles are exempt. Reversible with `bodh undo`."""
    ctx = _ctx(verbose)
    candidates, _ = runmod.clear_old(ctx, days, dry_run=True)
    typer.echo(f"{candidates} unread articles older than {days} days (stars and protections exempt)")
    if dry_run or candidates == 0:
        return
    if not yes and not typer.confirm("Mark them read?"):
        raise typer.Exit(0)
    with run_lock(ctx.cfg.root / ctx.cfg.data_dir, "clear-old"), graceful_sigterm():
        _, done = runmod.clear_old(ctx, days, dry_run=False)
    typer.echo(f"marked {done} read; `bodh undo` reverses it")


@app.command(hidden=True)
@_handled
def bankruptcy(
    older_than: int = typer.Option(14, "--older-than"),
    yes: bool = typer.Option(False, "--yes"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = VERBOSE,
) -> None:
    """The name before October 2026 of `clear-old --days N`."""
    _renamed("bankruptcy --older-than N", "clear-old --days N")
    clear_old(days=older_than, yes=yes, dry_run=dry_run, verbose=verbose)


@app.command()
@_handled
def status(verbose: bool = VERBOSE) -> None:
    """The whole system at a glance: what is running, the schedule, the unread, spend, the last runs."""
    from . import jobs

    ctx = _ctx(verbose)
    typer.echo(jobs.render_status_text(jobs.status(ctx.cfg, ctx.conn)))
    typer.echo("\n(`bodh progress` for the unread by folder, `bodh console` for the console)")


@app.command()
@_handled
def stop(
    everything: bool = typer.Option(
        False,
        "--all",
        help="Also pause every scheduled job and end the console; `bodh schedule resume` brings them back.",
    ),
    verbose: bool = VERBOSE,
) -> None:
    """Stop the run, prune or catch-up in progress (the same as Ctrl-C in its terminal). With
    --all, stop everything: the run, the schedule and the console."""
    from . import jobs
    from .locks import stop_process

    cfg = _config()
    if everything:
        for line in jobs.stop_all(cfg):
            typer.echo(line)
        return
    typer.echo(stop_process(cfg.root / cfg.data_dir))


@app.command()
@_handled
def schedule(
    action: str = typer.Argument("status", help="status, pause, resume or kick (a run now, via launchd)"),
    job: str = typer.Option("all", "--job", help="run, weekly, report, console or all"),
    verbose: bool = VERBOSE,
) -> None:
    """The launchd jobs: show them, pause them all (nothing fires until resume), resume, or kick a run."""
    from . import jobs

    if job == "panel":  # the console's name before October 2026
        job = "console"
    if action == "status":
        for name, label in jobs.JOBS.items():
            j = jobs.job_state(label)
            extra = f" · last exit {j['last_exit']}" if j["last_exit"] not in (None, 0) else ""
            typer.echo(f"{name:<8}{j['state']}{extra}  {j['plist'] or label}")
        return
    if action not in ("pause", "resume", "kick"):
        _fail("action must be status, pause, resume or kick")
    if job != "all" and job not in jobs.JOBS:
        _fail("--job must be run, weekly, report, console or all")
    cfg = _config()
    typer.echo(jobs.schedule(action, job, cfg.root / cfg.data_dir))


@app.command(hidden=True)
@_handled
def halt(verbose: bool = VERBOSE) -> None:
    """The name before October 2026 of `stop --all`."""
    _renamed("halt", "stop --all")
    stop(everything=True, verbose=verbose)


@app.command()
@_handled
def safety(action: str = typer.Argument("status", help="status, or reset"), verbose: bool = VERBOSE) -> None:
    """The safety limit: an autocut that would cut nearly everything newly judged trips it, and
    autocut then cuts nothing until `bodh safety reset` says someone has looked."""

    if action not in ("status", "reset"):
        _fail("say status or reset")
    ctx = _ctx(verbose)
    tripped = cutmod.safety_tripped(ctx.conn)
    given_up = store.given_up(ctx.conn)
    if action == "reset":
        if cutmod.reset_safety(ctx.conn):
            typer.echo(
                f"safety limit reset (it tripped in run {tripped}); autocut cuts again from the next run"
            )
        else:
            typer.echo("the safety limit is not tripped")
        n = store.reset_judge_failures(ctx.conn)
        typer.echo(
            f"{given_up} articles the judge had given up on are pending again"
            if given_up
            else f"no articles were given up on ({n} failure counts cleared)"
        )
        return
    if tripped:
        typer.echo(
            f"safety limit TRIPPED in run {tripped}: autocut cuts nothing. Check the prompt and the model "
            "(`bodh why ID` on a few newly judged articles), then `bodh safety reset`"
        )
    else:
        typer.echo("safety limit clear: autocut cuts as configured")
    if given_up:
        typer.echo(
            f"{given_up} unread articles given up on after {store.MAX_JUDGE_FAILURES} answers with no judgment; "
            "they stay unread and unjudged until `bodh safety reset`"
        )
    else:
        typer.echo("no article has been given up on")


@app.command()
@_handled
def autocut(state: str = typer.Argument(..., help="on or off"), verbose: bool = VERBOSE) -> None:
    """The automatic cut: on = autocut cuts below the cut-off from the next run; off = scoring-only
    mode, everything is judged and tagged, nothing is cut. Edits config.yaml in place."""
    from . import jobs

    if state not in ("on", "off"):
        _fail("say on or off")
    cfg = _config()
    ok, msg = jobs.set_autocut(cfg.root, state == "on")
    if not ok:
        _fail(msg)
    typer.echo(msg)


@app.command(hidden=True)
@_handled
def enforce(state: str = typer.Argument(..., help="on or off"), verbose: bool = VERBOSE) -> None:
    """The name before October 2026 of `autocut on|off`."""
    _renamed("enforce", "autocut")
    autocut(state=state, verbose=verbose)


@app.command()
@_handled
def console(
    port: int = typer.Option(8766, "--port"),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="127.0.0.1 keeps it local; publish it on your tailnet with `tailscale serve`.",
    ),
    verbose: bool = VERBOSE,
) -> None:
    """The console: status, run, stop, schedule, prune, undo, catch-up, log, dashboard, in one page."""
    import traceback

    from .console import Console, console_log

    ctx = _ctx(verbose)
    typer.echo(f"console on http://{host}:{port}  (Ctrl-C stops it; launchd keeps it alive when installed)")
    try:
        Console(ctx.cfg, ctx.conn, host=host, port=port).serve()
    except KeyboardInterrupt:
        raise typer.Exit(0) from None
    except (
        Exception
    ) as exc:  # the console could not start (a port in use, say): the reason goes to console.log
        console_log(
            ctx.cfg.root / ctx.cfg.data_dir, f"failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        )
        _fail(f"console failed: {exc}")


@app.command(hidden=True)
@_handled
def panel(
    port: int = typer.Option(8766, "--port"),
    host: str = typer.Option("127.0.0.1", "--host"),
    verbose: bool = VERBOSE,
) -> None:
    """The name before October 2026 of `console`."""
    _renamed("panel", "console")
    console(port=port, host=host, verbose=verbose)


@app.command()
@_handled
def why(article_id: str, verbose: bool = VERBOSE) -> None:
    """One article's score, sub-scores, lesson and state."""
    ctx = _ctx(verbose)
    try:
        aid = normalize_item_id(article_id)
    except ValueError:
        aid = article_id
    row = store.article(ctx.conn, aid)
    if row is None:
        _fail("not found")
        return
    typer.echo(f"{row['title']}\n{row['url']}\nfeed: {row['feed_title']} · folder: {row['folder']}")
    if row["score"] is None:
        typer.echo("not scored yet")
    else:
        typer.echo(
            f"score {row['score']:.1f} (base {row['base_score']:.1f}"
            + (f", review {row['second_look_score']:.1f}" if row["second_look_score"] is not None else "")
            + ")"
        )
        from .judgment import normalize_flags

        try:
            flags = normalize_flags(json.loads(row["flags"] or "[]"))
        except (TypeError, ValueError):
            flags = [str(row["flags"])]
        typer.echo(
            f"quality {row['quality']} · learning {row['learning']} · fit {row['fit']} · breadth {row['breadth']}"
            f" · relevance {row['relevance']} · confidence {row['confidence']} · kind {row['kind']} · flags "
            f"{', '.join(flags) or 'none'} · judged by {row['model']} ({row['prompt_version']})"
        )
        typer.echo(f"concept: {row['concept']}\nlesson: {row['lesson']}")
    typer.echo(
        f"state: {row['status']}"
        + (f" ({store.cut_mode_name(row['cut_mode'])} at {row['cut_at']})" if row["cut_mode"] else "")
        + (" · starred" if row["starred"] else "")
        + (" · protected" if row["protected"] else "")
        + (" · new ground" if row["exploration"] else "")
    )
    marks = store.feedback_for(ctx.conn, aid)
    if marks:
        typer.echo(
            "your record: " + ", ".join(f"{store.feedback_kind_name(kind)} ({at[:10]})" for kind, at in marks)
        )


MARKS = ("great", "good", "meh", "bad", "basic", "heavy", "more", "less", "rescue")


@app.command()
@_handled
def mark(
    article_id: str,
    verdict: str = typer.Argument(..., help="great, good, meh, bad, basic, heavy, more, less or rescue"),
    verbose: bool = VERBOSE,
) -> None:
    """Your verdict on an article from the terminal, the same as its tag in BazQux: a grade
    (great, good, meh, bad), the level missed (basic, heavy), a steer by its concept (more, less),
    or `rescue`, which brings a cut article back unread and keeps it from every cut."""
    if verdict not in MARKS:
        _fail("the verdict must be one of " + ", ".join(MARKS))
    ctx = _ctx(verbose)
    try:
        aid = normalize_item_id(article_id)
    except ValueError:
        _fail(f"not an article id: {article_id!r} (the long tag:google.com form or the signed number)")
        return
    row = store.article(ctx.conn, aid)
    if row is None:
        _fail("unknown article; it must have been fetched by a run first")
        return
    try:
        with run_lock(ctx.cfg.root / ctx.cfg.data_dir, "mark"), graceful_sigterm():
            if verdict == "rescue":
                typer.echo(feedback.rescue_one(ctx.client, ctx.conn, ctx.cfg, aid, row))
            elif verdict in store.STEERS:
                store.set_verdict(ctx.conn, aid, verdict, store.STEERS)
                phrase = feedback.steer_phrase(ctx.conn, aid)
                profiler.steers(ctx.cfg, ctx.conn, [("want" if verdict == "more" else "meh", phrase)])
                typer.echo(f"recorded {verdict} for: {row['title'][:70]} (steer: {phrase})")
            else:
                group = store.GRADES if verdict in store.GRADES else store.LEVELS
                store.set_verdict(ctx.conn, aid, verdict, group)
                typer.echo(f"recorded {verdict} for: {row['title'][:70]}")
    except AlreadyRunning:
        _fail("a run is in progress; try again in a minute (or tag the article in BazQux)", 5)


@app.command(hidden=True)
@_handled
def label(
    article_id: str, judgment: str = typer.Argument(..., help="star or junk"), verbose: bool = VERBOSE
) -> None:
    """The name before October 2026 of `mark ID great|bad`."""
    if judgment not in ("star", "junk"):
        _fail("judgment must be star or junk")
    _renamed(f"label ID {judgment}", "mark ID " + ("great" if judgment == "star" else "bad"))
    mark(article_id=article_id, verdict="great" if judgment == "star" else "bad", verbose=verbose)


@app.command()
@_handled
def steer(
    direction: str = typer.Argument(..., help="more or less"),
    phrase: str = typer.Argument(..., help="the concept"),
    verbose: bool = VERBOSE,
) -> None:
    """Tell the judge now that you want more, or less, of a concept: a dated line in the learned
    profile that it reads from the next run (the `more` and `less` tags do the same by article)."""
    if direction not in ("more", "less"):
        _fail("say more or less")
    _steer("want" if direction == "more" else "meh", phrase, verbose)


@app.command(hidden=True)
@_handled
def want(phrase: str, verbose: bool = VERBOSE) -> None:
    """The name before October 2026 of `steer more`."""
    _renamed('want "x"', 'steer more "x"')
    _steer("want", phrase, verbose)


@app.command(hidden=True)
@_handled
def meh(phrase: str, verbose: bool = VERBOSE) -> None:
    """The name before October 2026 of `steer less`."""
    _renamed('meh "x"', 'steer less "x"')
    _steer("meh", phrase, verbose)


def _steer(kind: str, phrase: str, verbose: bool) -> None:
    ctx = _ctx(verbose)
    try:
        with (
            run_lock(ctx.cfg.root / ctx.cfg.data_dir, "steer"),
            graceful_sigterm(),
        ):  # learned.md is written: not under a run
            typer.echo(profiler.steer(ctx.cfg, ctx.conn, kind, phrase))
    except AlreadyRunning:
        _fail("a run is in progress; try again in a minute (or tag the article `more`/`less` in BazQux)", 5)


@app.command()
@_handled
def profile(action: str = typer.Argument("show", help="show or revert"), verbose: bool = VERBOSE) -> None:
    """Show the student profile (about, then learned), or go back one version of the learned part."""
    ctx = _ctx(verbose)
    if action == "revert":
        with run_lock(ctx.cfg.root / ctx.cfg.data_dir, "revert"), graceful_sigterm():
            v = profiler.revert(ctx.cfg, ctx.conn)
        typer.echo("nothing to revert to" if v is None else f"reverted; now version {v}")
        return
    typer.echo(ctx.cfg.read_about())
    typer.echo("\n---\n")
    typer.echo(ctx.cfg.read_learned())


@app.command()
@_handled
def weekly(dry_run: bool = typer.Option(False, "--dry-run"), verbose: bool = VERBOSE) -> None:
    """Sunday's job: rewrite the learned profile (gated), write the weekly note, run the drift check."""
    ctx = _ctx(verbose)
    try:
        with run_lock(ctx.cfg.root / ctx.cfg.data_dir, "weekly"), graceful_sigterm():
            _weekly(ctx, dry_run)
    except AlreadyRunning:
        _fail(
            "a run is in progress; the weekly job will be run again by launchd next Sunday, or run it by hand later",
            5,
        )


def _weekly(ctx: runmod.Context, dry_run: bool) -> None:
    run_id = store.begin_run(ctx.conn, "weekly")
    try:
        result = profiler.weekly_rewrite(ctx.cfg, ctx.llm, ctx.conn, run_id, dry_run=dry_run)
        typer.echo(result["reason"])
        if result.get("diff"):
            typer.echo(result["diff"])
        drift = _drift_check(ctx, run_id)
        if drift:
            typer.echo(drift)
        note = dashboard.weekly_note(ctx.cfg, ctx.conn, result)
        typer.echo(f"weekly note: {note}")
        dashboard.render(ctx.cfg, ctx.conn)
        store.finish_run(
            ctx.conn, run_id, True, summary=f"weekly: {result['reason']}" + (f"; {drift}" if drift else "")
        )
        if not result["applied"] and result.get("gate") and not result["gate"]["passed"]:
            ctx.heartbeat.fail(f"weekly profile rewrite rejected: {result['reason']}")
    except Exception as exc:  # the run row and the heartbeat record the failure before the line and exit 1
        store.finish_run(ctx.conn, run_id, False, error=f"{type(exc).__name__}: {exc}")
        ctx.heartbeat.fail(f"weekly failed: {exc}")
        _fail(f"weekly failed: {exc}")


def _drift_check(ctx: runmod.Context, run_id: int) -> str:
    """Re-score 2% of the week's articles; a large shift means the model behind the API changed."""
    conn, cfg = ctx.conn, ctx.cfg
    rows = conn.execute(
        """SELECT a.*, s.base_score FROM scores s JOIN articles a ON a.id = s.article_id
           WHERE s.scored_at >= ? AND s.model = ? AND a.text != '' ORDER BY RANDOM() LIMIT ?""",
        (store.days_ago_iso(7), cfg.models.judge, 30),
    ).fetchall()
    if len(rows) < 10:
        return ""
    judge = ctx.judge()
    from .judgment import compose

    diffs = []
    for i in range(0, len(rows), cfg.judge.articles_per_call):
        batch = rows[i : i + cfg.judge.articles_per_call]
        verdicts = judge.judge_batch(batch, run_id)
        for r in batch:
            v = verdicts.get(r["id"])
            if v is not None:
                diffs.append(compose(v, cfg, judge.expansion_topics) - float(r["base_score"]))
    if not diffs:
        return ""
    mean_shift = sum(diffs) / len(diffs)
    mean_abs = sum(abs(d) for d in diffs) / len(diffs)
    msg = f"drift check: {len(diffs)} re-scored, mean shift {mean_shift:+.1f}, mean absolute change {mean_abs:.1f}"
    if abs(mean_shift) > 8 or mean_abs > 15:
        ctx.heartbeat.fail(msg + " (above threshold)")
        return msg + " ABOVE THRESHOLD"
    return msg


@app.command("report")
@_handled
def report_cmd(days: int = typer.Option(30, "--days"), verbose: bool = VERBOSE) -> None:
    """The monthly feed report with proposals; written to the dashboard folder."""
    ctx = _ctx(verbose)
    path = report.render(ctx.cfg, ctx.conn, days=days)
    typer.echo(path.read_text(encoding="utf-8"))


@app.command("dashboard")
@_handled
def dashboard_cmd(verbose: bool = VERBOSE) -> None:
    """Regenerate the dashboard now."""
    ctx = _ctx(verbose)
    typer.echo(str(dashboard.render(ctx.cfg, ctx.conn)))


def triage_alias() -> None:
    """The old command name, kept for one release: a note on stderr, then the same program."""
    typer.echo("note: `triage` is now `bodh`; the old name goes away in the next release", err=True)
    app()


if __name__ == "__main__":
    app()
