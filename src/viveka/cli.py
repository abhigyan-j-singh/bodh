"""viveka: keeps your BazQux unread list worth reading.

    viveka doctor [--live]      check config, secrets, prompts; --live also logs in and calls the models
    viveka run [--shadow]       the timer's job: feedback, ingest, score, tag, floor, dashboard, heartbeat
    viveka blast                one pass over everything pending, as fast as the API allows; cuts nothing
    viveka progress [--watch]   judged, in flight, pending, rate, ETA, by folder; batches; the log
    viveka prune                cut to the target (default --keep 40); or --fraction 0.3; or --below 65
    viveka undo                 reverse the last cut
    viveka bankruptcy           mark everything older than N days read, stars and protections exempt
    viveka status | why ID      the whole system at a glance; why one article got its score
    viveka panel                the control room in the browser: status, run, stop, prune, undo, log
    viveka stop | halt          stop what is running; halt stops everything incl. the schedule and the control room
    viveka schedule             show, pause, resume or kick the launchd jobs
    viveka enforce on|off       flip the automatic floor
    viveka want "x" | meh "x"   nudge the learner model now
    viveka weekly | report      Sunday's learner rewrite and note; the monthly feed report
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import typer

from . import __version__, dashboard, learner, pipeline, report, store
from .config import Config, ConfigError, load_config, load_secrets
from .ids import normalize_item_id
from .ops import AlreadyRunning, RunTimeout, graceful_sigterm, run_lock, setup_logging, time_limit

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
    except Exception as exc:  # noqa: BLE001 — a config that does not parse or validate
        _fail(f"config.yaml does not load: {exc}", EXIT_CONFIG)
        raise


def _ctx(verbose: bool = False) -> pipeline.Context:
    cfg = _config()
    secrets = load_secrets(cfg.root)
    setup_logging(cfg.root / cfg.data_dir, verbose, secrets=secrets.values())
    conn = store.connect(cfg.db_path)
    return pipeline.Context(cfg, secrets, conn)


def _fail(msg: str, code: int = 1) -> None:
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(code)


@app.command()
def version() -> None:
    typer.echo(f"viveka {__version__}")


@app.command()
def doctor(live: bool = typer.Option(False, "--live", help="Also log in to BazQux and call both models."),
           verbose: bool = VERBOSE) -> None:
    """Connectivity and configuration checks. Run after every update. Exits 1 when a check
    fails, so an installer stops on a broken install."""
    cfg = _config()
    secrets = load_secrets(cfg.root)
    problems: list[str] = []

    def problem(text: str) -> None:
        problems.append(text)
        typer.echo(text)

    typer.echo(f"root: {cfg.root}")
    typer.echo(f"models: teacher={cfg.models.teacher} second_look={cfg.models.second_look} learner={cfg.models.learner}"
               f" budget=${cfg.models.daily_budget_usd}/day")
    mode = ("batch (half price; judgments arrive on the next run)" if cfg.models.batch
            else f"synchronous ({cfg.scoring.workers} calls at once)")
    typer.echo(f"mode: {mode} · prompt {cfg.scoring.prompt_version} · {cfg.scoring.batch_size} articles per call"
               f" · articles older than {cfg.scoring.max_age_days} days are never scored")
    typer.echo(f"cut: {'enforcing' if cfg.cut.enforce else 'shadow mode'}, target {cfg.cut.target_unread}, "
               f"floor {cfg.cut.default_floor}, max {cfg.cut.max_cuts_per_run}/run, folders {list(cfg.cut.folders) or 'none'}")
    if cfg.read_seed():
        typer.echo("seed: found")
    else:
        problem("seed: MISSING (profile/seed.md): the teacher cannot judge for a student it knows nothing about")
    typer.echo(f"learner model: {'found' if cfg.read_learner() else 'missing (will start empty)'}")
    from .teacher import PROMPTS_DIR, load_prompt

    for name in (cfg.scoring.prompt_version, "second_look.v1", "learner.v1"):
        try:
            load_prompt(name, cfg.root)
            typer.echo(f"prompt {name}: ok")
        except Exception as exc:  # noqa: BLE001
            problem(f"prompt {name}: MISSING ({exc}; looked in {PROMPTS_DIR})")
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
    conn = store.connect(cfg.db_path)
    typer.echo(f"database: {cfg.db_path} ({conn.execute('SELECT COUNT(*) FROM articles').fetchone()[0]} articles, "
               f"schema v{store.schema_version(conn)})")
    if live and not problems:
        _doctor_live(cfg, secrets, conn, problem)
    elif live:
        typer.echo("live checks skipped until the problems above are fixed")
    if problems:
        typer.echo(f"\n{len(problems)} problem(s); see above", err=True)
        raise typer.Exit(1)
    typer.echo("ok")


def _doctor_live(cfg: Config, secrets, conn, problem) -> None:  # noqa: ANN001
    ctx = pipeline.Context(cfg, secrets, conn)
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
        want = f"user/-/label/{cfg.cut.must_read_stream}"
        if want in tags:
            typer.echo(f"must-read stream '{cfg.cut.must_read_stream}': present")
        else:
            problem(f"must-read stream '{cfg.cut.must_read_stream}': not found (create it in BazQux, or name yours in "
                    "cut.must_read_stream)")
        unknown = [f for f in cfg.cut.folders if f not in folders]
        if unknown:
            problem(f"config folders not in BazQux: {unknown}")
        if unread:
            items, _ = ctx.client.contents([r.id for r in unread[:3]])
            for it in items:
                typer.echo(f"   - {it.title[:70]} ({it.feed_title[:30]})")
    except Exception as exc:  # noqa: BLE001
        problem(f"bazqux: FAILED ({exc})")
    try:
        from .teacher import Teacher, concept_context

        teacher = Teacher(cfg, ctx.llm, conn, context=concept_context(conn, cfg))
        probe = {"id": "probe", "feed_title": "doctor", "folder": None, "title": "A short note on why liquid cooling retrofits cost more than greenfield builds",
                 "url": "https://example.com/probe", "author": "doctor", "published": None,
                 "text": "A probe article. Retrofitting an air-cooled hall for direct-to-chip liquid cooling means new manifolds, CDUs and leak detection in a live facility; greenfield designs size the plant once."}
        verdicts = teacher.judge_batch([probe], None)  # type: ignore[list-item]
        v = verdicts.get("probe")
        if v:
            typer.echo(f"teacher ({cfg.models.teacher}): ok, probe scored {v.quality}/{v.learning}/{v.fit}/{v.breadth}/{v.relevance}")
        else:
            problem(f"teacher ({cfg.models.teacher}): call ok, no verdict parsed")
        second = teacher.second_look(probe, None)  # type: ignore[arg-type]
        if second is not None:
            typer.echo(f"second look ({cfg.models.second_look}): ok")
        else:
            problem(f"second look ({cfg.models.second_look}): FAILED")
        typer.echo(f"spend today, this doctor run included: ${store.spent_since(conn, store.local_midnight_iso(cfg.timezone)):.4f}")
    except Exception as exc:  # noqa: BLE001
        problem(f"models: FAILED ({exc})")


@app.command()
def run(shadow: bool = typer.Option(False, "--shadow", help="Score and tag, cut nothing."),
        limit: int | None = typer.Option(None, "--limit", help="Score (or submit) at most this many articles this run."),
        sync: bool = typer.Option(False, "--sync", help="Wait for every model call instead of using the Batches API."),
        workers: int | None = typer.Option(None, "--workers", help="Model calls in flight at once (--sync); default from config."),
        verbose: bool = VERBOSE) -> None:
    """One full pass. This is what launchd runs every 30 minutes."""
    ctx = _ctx(verbose)
    try:
        with run_lock(ctx.cfg.root / ctx.cfg.data_dir, "run"), graceful_sigterm(), time_limit(pipeline.RUN_TIMEOUT_SECONDS):
            res = pipeline.run(ctx, score_limit=limit, enforce=(ctx.cfg.cut.enforce and not shadow),
                               workers=workers, sync=sync)
    except AlreadyRunning:
        typer.echo("another run is in progress; skipping")
        raise typer.Exit(0) from None
    except RunTimeout as exc:
        _fail(str(exc), 2)
        return
    except KeyboardInterrupt:
        _fail("stopped; the next run picks up where this one left off", 130)
        return
    except Exception as exc:  # noqa: BLE001
        _fail(f"run failed: {exc}", 1)
        return
    typer.echo(res.summary())


@app.command()
def score(limit: int | None = typer.Option(None, "--limit"), sync: bool = typer.Option(False, "--sync"),
          workers: int | None = typer.Option(None, "--workers"), verbose: bool = VERBOSE) -> None:
    """Score and tag without cutting (the same as `run --shadow`)."""
    run(shadow=True, limit=limit, sync=sync, workers=workers, verbose=verbose)


@app.command()
def progress(watch: bool = typer.Option(False, "--watch", help="Redraw every --every seconds until Ctrl-C."),
             every: int = typer.Option(5, "--every", help="Seconds between redraws with --watch."),
             verbose: bool = VERBOSE) -> None:
    """How far the teacher has got: judged, in flight, pending, by folder; rate and ETA while a
    run is going; batches; spend; the log."""
    import time

    from . import progress as progressmod
    from .ops import log_file

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


@app.command()
def blast(workers: int = typer.Option(8, "--workers", help="Model calls in flight at once."),
          chunk: int = typer.Option(1000, "--chunk", help="Articles per round."),
          all_ages: bool = typer.Option(False, "--all-ages", help="Also the unread articles older than scoring.max_age_days."),
          model: str | None = typer.Option(None, "--model", help="A stand-in for the teacher, e.g. claude-haiku-5-5 "
                                           "(fastest, 20x cheaper); the teacher re-judges what it rates 50+."),
          budget: float | None = typer.Option(None, "--budget", help="Raise today's budget to this many dollars for the blast."),
          max_seconds: float | None = typer.Option(None, "--max-seconds", help="Start no new call after this long; run again to continue."),
          yes: bool = typer.Option(False, "--yes", help="Skip the confirmation."),
          verbose: bool = VERBOSE) -> None:
    """One deliberate pass over everything pending, as fast as the API allows: synchronous,
    many calls at once, full price. Scores and tags; cuts nothing. Stop it with Ctrl-C and run
    it again to continue. Watch it on the dashboard or with `viveka progress --watch`."""
    import time

    from .teacher import Teacher

    ctx = _ctx(verbose)
    if not verbose:  # the progress line owns the terminal; the log file keeps the detail
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.WARNING)
    cfg = ctx.cfg
    if all_ages:
        cfg.scoring.max_age_days = None
    judge = model or cfg.models.teacher
    stand_in = judge != cfg.models.teacher
    rejudge = None if stand_in else Teacher.rejudge_rule(cfg)
    pending = store.unscored_unread_count(ctx.conn, cfg.scoring.prompt_version, cfg.scoring.max_age_days,
                                          cfg.cut.unscored_folders(), exclude_inflight=True, rejudge=rejudge)
    if pending == 0:
        typer.echo("nothing pending" + ("" if all_ages else " within the scoring window (--all-ages for the rest)"))
        return
    if not cfg.models.has_price(judge):
        _fail(f"no price for {judge} in models.prices_per_mtok; add it to config.yaml first")
    p_in, p_out = cfg.models.price(judge)
    est = pending * (450 * p_in + 65 * p_out) / 1_000_000 * 1.1   # synchronous, prefix cached
    left = cfg.models.daily_budget_usd - store.spent_since(ctx.conn, store.local_midnight_iso(cfg.timezone))
    need = max(cfg.models.daily_budget_usd, cfg.models.daily_budget_usd - left + est + 1)
    typer.echo(f"{pending:,} articles pending{' (any age)' if all_ages else ''}; about ${est:.2f} with {judge}, "
               f"{workers} calls at once." + (" The teacher re-judges what it rates 50+ on the next runs."
                                               if stand_in else ""))
    if est > left and budget is None:
        typer.echo(f"today's budget has ${left:.2f} left; pass --budget {need:.0f} to let the blast finish")
    if not yes and not typer.confirm("Start the blast?"):
        raise typer.Exit(0)
    started = time.monotonic()
    last_line = {"n": 0}

    def show(total: dict, stats: dict) -> None:
        done = total["scored"] + total["dupes"] + stats["scored"] + stats["dupes"]
        mins = max(1e-6, (time.monotonic() - started) / 60)
        rate = done / mins
        left_n = max(0, pending - done)
        eta = f"{left_n / rate:.0f} min" if rate > 0 else "–"
        line = (f"\r  judged {done:,} of {pending:,} · {rate:.0f}/min · ETA {eta} · "
                f"failed batches {total['failed'] + stats['failed_batches']} · round {total['rounds'] + 1}   ")
        typer.echo(line, nl=False)
        last_line["n"] = done

    try:
        with run_lock(cfg.root / cfg.data_dir, "blast"), graceful_sigterm():
            total = pipeline.blast(ctx, workers=workers, chunk=chunk, all_ages=all_ages, budget=budget,
                                   on_progress=show, model=model, max_seconds=max_seconds)
    except AlreadyRunning:
        _fail("another run is in progress; wait for it (or stop the launchd agent) and try again")
        return
    typer.echo("")
    typer.echo(f"judged {total['scored']:,} (+{total['dupes']:,} dupes) in {total['elapsed'] / 60:.1f} min over "
               f"{total['rounds']} rounds; {total['failed']} failed batches; pending {total['pending']:,}; "
               f"spend today ${total['spend']:.2f}")
    if total["interrupted"]:
        typer.echo("stopped; what was judged stays. Run `viveka blast` again to continue.")
    if total["timed_out"]:
        typer.echo("time limit reached; run again to continue")
    if total["budget_stop"]:
        typer.echo("stopped at the daily budget; pass --budget to raise it for today")
    if total["failed"] and total["pending"]:
        typer.echo("some calls were refused (rate limits); run again, or with fewer --workers")


@app.command()
def prune(keep: int | None = typer.Option(None, "--keep", help="Keep this many unread (default: the target)."),
          fraction: float | None = typer.Option(None, "--fraction", help="Cut this share of what is unread."),
          below: float | None = typer.Option(None, "--below", help="Cut everything scoring under this."),
          dry_run: bool = typer.Option(False, "--dry-run", help="Show the cut without making it."),
          yes: bool = typer.Option(False, "--yes", help="Skip the confirmation."),
          verbose: bool = VERBOSE) -> None:
    """Cut the worst of what is still unread. Default keeps the target (40). Run again to cut
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
            _, sel = pipeline.prune(ctx, keep=keep, fraction=fraction, below=below, dry_run=True)
        _show_prune(sel, dry_run=True)
        if dry_run or not sel.cut:
            return
        # the question is asked with the lock released: a run must not be skipped over a prompt
        if not yes and _interactive() and not typer.confirm(f"Cut these {len(sel.cut)} (undo reverses it)?"):
            raise typer.Exit(0)
        with run_lock(lock, "prune"), graceful_sigterm():
            n, sel = pipeline.prune(ctx, keep=keep, fraction=fraction, below=below, confirmed=set(sel.cut_ids))
    except AlreadyRunning:
        _fail("another run is in progress; `viveka stop` it or wait")
        return
    except KeyboardInterrupt:
        _fail("stopped; the next run finishes the tags and read marks, `viveka undo` reverses the prune", 130)
        return
    except pipeline.cutmod.BreakerTripped as exc:
        _fail(str(exc))
        return
    if n:
        typer.echo(f"cut {n}; `viveka undo` reverses it (run {store.last_runs(ctx.conn, 1)[0]['id']})")


def _interactive() -> bool:
    """Someone is at the keyboard (so a question can be asked); a script or launchd is not."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _show_prune(sel: Any, dry_run: bool) -> None:
    unread_scored = len(sel.cut) + len(sel.survivors)
    verb = "would cut" if dry_run else "cut"
    typer.echo(f"{verb} {len(sel.cut)} of {unread_scored} scored unread articles; boundary "
               f"{sel.boundary:.0f}" if sel.boundary is not None else f"{verb} {len(sel.cut)} articles")
    if sel.cut:
        best = sorted(sel.cut, key=lambda r: -float(r["score"]))[:10]
        typer.echo("the best of what goes (this is the boundary you are choosing):")
        for r in best:
            typer.echo(f"  {r['score']:5.1f}  {(r['title'] or '')[:70]}  [{(r['feed_title'] or '')[:25]}]")


@app.command()
def undo(verbose: bool = VERBOSE) -> None:
    """Reverse the last cut: mark its articles unread again and remove the cut tag."""
    ctx = _ctx(verbose)
    try:
        with run_lock(ctx.cfg.root / ctx.cfg.data_dir, "undo"), graceful_sigterm():
            run_id, n = pipeline.undo_last(ctx)
    except AlreadyRunning:
        _fail("another run is in progress; `viveka stop` it or wait")
        return
    if run_id is None:
        typer.echo("nothing to undo")
    else:
        typer.echo(f"restored {n} articles from run {run_id}")


@app.command()
def bankruptcy(older_than: int = typer.Option(14, "--older-than", help="Days."),
               yes: bool = typer.Option(False, "--yes"), dry_run: bool = typer.Option(False, "--dry-run"),
               verbose: bool = VERBOSE) -> None:
    """Clear the debt: every unread article older than the cutoff is marked read and tagged `cut`.
    Stars and protected articles are exempt. Reversible with `viveka undo`."""
    ctx = _ctx(verbose)
    candidates, _ = pipeline.bankruptcy(ctx, older_than, dry_run=True)
    typer.echo(f"{candidates} unread articles older than {older_than} days (stars and protections exempt)")
    if dry_run or candidates == 0:
        return
    if not yes and not typer.confirm("Mark them read?"):
        raise typer.Exit(0)
    try:
        with run_lock(ctx.cfg.root / ctx.cfg.data_dir, "bankruptcy"), graceful_sigterm():
            _, done = pipeline.bankruptcy(ctx, older_than, dry_run=False)
    except AlreadyRunning:
        _fail("another run is in progress; `viveka stop` it or wait")
        return
    typer.echo(f"marked {done} read; `viveka undo` reverses it")


@app.command()
def status(verbose: bool = VERBOSE) -> None:
    """The whole system at a glance: what is running, the schedule, the pile, spend, the last runs."""
    from . import control

    ctx = _ctx(verbose)
    typer.echo(control.render_status_text(control.status(ctx.cfg, ctx.conn)))
    typer.echo("\n(`viveka progress` for the pile by folder, `viveka panel` for the control room)")


@app.command()
def stop(verbose: bool = VERBOSE) -> None:
    """Stop the run, prune or blast in progress (the same as Ctrl-C in its terminal)."""
    from .ops import stop_process

    cfg = _config()
    typer.echo(stop_process(cfg.root / cfg.data_dir))


@app.command()
def schedule(action: str = typer.Argument("status", help="status, pause, resume or kick (a run now, via launchd)"),
             job: str = typer.Option("all", "--job", help="run, weekly, report, panel or all"),
             verbose: bool = VERBOSE) -> None:
    """The launchd jobs: show them, pause them all (nothing fires until resume), resume, or kick a run."""
    from . import control

    if action == "status":
        for name, label in control.JOBS.items():
            j = control.job_state(label)
            extra = f" · last exit {j['last_exit']}" if j["last_exit"] not in (None, 0) else ""
            typer.echo(f"{name:<8}{j['state']}{extra}  {j['plist'] or label}")
        return
    if action not in ("pause", "resume", "kick"):
        _fail("action must be status, pause, resume or kick")
    if job != "all" and job not in control.JOBS:
        _fail("--job must be run, weekly, report, panel or all")
    cfg = _config()
    typer.echo(control.schedule(action, job, cfg.root / cfg.data_dir))


@app.command()
def halt(verbose: bool = VERBOSE) -> None:
    """Stop everything: pause every launchd job, interrupt the run in progress, end any other
    viveka process including the control room. `viveka schedule resume` brings the jobs back."""
    from . import control

    cfg = _config()
    for line in control.halt(cfg):
        typer.echo(line)


@app.command()
def safety(action: str = typer.Argument("status", help="status, or reset"), verbose: bool = VERBOSE) -> None:
    """The breaker: a floor that would cut nearly everything newly judged trips it, and the
    floor then cuts nothing until `viveka safety reset` says someone has looked."""
    from . import cut as cutmod

    if action not in ("status", "reset"):
        _fail("say status or reset")
    ctx = _ctx(verbose)
    tripped = cutmod.breaker_tripped(ctx.conn)
    given_up = store.given_up(ctx.conn)
    if action == "reset":
        if cutmod.reset_breaker(ctx.conn):
            typer.echo(f"breaker reset (it tripped in run {tripped}); the floor cuts again from the next run")
        else:
            typer.echo("the breaker is not tripped")
        n = store.reset_judge_failures(ctx.conn)
        typer.echo(f"{given_up} articles the teacher had given up on are pending again" if given_up
                   else f"no articles were given up on ({n} failure counts cleared)")
        return
    if tripped:
        typer.echo(f"breaker TRIPPED in run {tripped}: the floor cuts nothing. Check the prompt and the model "
                   "(`viveka why ID` on a few newly judged articles), then `viveka safety reset`")
    else:
        typer.echo("breaker clear: the floor cuts as configured")
    if given_up:
        typer.echo(f"{given_up} unread articles given up on after {store.MAX_JUDGE_FAILURES} answers with no judgment; "
                   "they stay unread and unjudged until `viveka safety reset`")
    else:
        typer.echo("no article has been given up on")


@app.command()
def enforce(state: str = typer.Argument(..., help="on or off"), verbose: bool = VERBOSE) -> None:
    """Flip the automatic floor: on = the floor cuts from the next run; off = shadow mode."""
    from . import control

    if state not in ("on", "off"):
        _fail("say on or off")
    cfg = _config()
    ok, msg = control.set_enforce(cfg.root, state == "on")
    if not ok:
        _fail(msg)
    typer.echo(msg)


@app.command()
def panel(port: int = typer.Option(8766, "--port"), host: str = typer.Option("127.0.0.1", "--host",
          help="127.0.0.1 keeps it local; publish it on your tailnet with `tailscale serve`."),
          verbose: bool = VERBOSE) -> None:
    """The control room: status, run, stop, schedule, prune, undo, blast, log, dashboard, in one page."""
    import traceback

    from .panel import Panel, panel_log

    ctx = _ctx(verbose)
    typer.echo(f"control room on http://{host}:{port}  (Ctrl-C stops it; launchd keeps it alive when installed)")
    try:
        Panel(ctx.cfg, ctx.conn, host=host, port=port).serve()
    except KeyboardInterrupt:
        raise typer.Exit(0) from None
    except Exception as exc:  # noqa: BLE001 — leave the reason where `viveka status` can show it
        panel_log(ctx.cfg.root / ctx.cfg.data_dir, f"failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        _fail(f"control room failed: {exc}")


@app.command()
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
        typer.echo(f"score {row['score']:.1f} (base {row['base_score']:.1f}"
                   + (f", second look {row['second_look_score']:.1f}" if row["second_look_score"] is not None else "") + ")")
        from .teacher import normalize_flags

        try:
            flags = normalize_flags(json.loads(row["flags"] or "[]"))
        except (TypeError, ValueError):
            flags = [str(row["flags"])]
        typer.echo(f"quality {row['quality']} · learning {row['learning']} · fit {row['fit']} · breadth {row['breadth']}"
                   f" · relevance {row['relevance']} · confidence {row['confidence']} · kind {row['kind']} · flags "
                   f"{', '.join(flags) or 'none'} · judged by {row['model']} ({row['prompt_version']})")
        typer.echo(f"concept: {row['concept']}\nlesson: {row['lesson']}")
    typer.echo(f"state: {row['status']}" + (f" ({row['cut_mode']} at {row['cut_at']})" if row["cut_mode"] else "")
               + (" · starred" if row["starred"] else "") + (" · protected" if row["protected"] else "")
               + (" · exploration" if row["exploration"] else ""))
    marks = store.feedback_for(ctx.conn, aid)
    if marks:
        typer.echo("your record: " + ", ".join(f"{kind} ({at[:10]})" for kind, at in marks))


@app.command()
def label(article_id: str, judgment: str = typer.Argument(..., help="star or junk"), verbose: bool = VERBOSE) -> None:
    """From the terminal, the same as tagging it great (star) or bad (junk) in BazQux."""
    if judgment not in ("star", "junk"):
        _fail("judgment must be star or junk")
    ctx = _ctx(verbose)
    try:
        aid = normalize_item_id(article_id)
    except ValueError:
        _fail(f"not an article id: {article_id!r} (the long tag:google.com form or the signed number)")
        return
    row = store.article(ctx.conn, aid)
    if row is None:
        _fail("unknown article; it must have been ingested first")
        return
    store.set_verdict(ctx.conn, aid, "great" if judgment == "star" else "bad", store.GRADES)
    golden = ctx.cfg.root / ctx.cfg.data_dir / "golden.jsonl"
    with golden.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": store.utcnow_iso(), "article_id": aid, "title": row["title"], "judgment": judgment}) + "\n")
    typer.echo(f"recorded {judgment} for: {row['title'][:70]}")


@app.command()
def want(phrase: str, verbose: bool = VERBOSE) -> None:
    """Tell the teacher now that you want more on a topic."""
    _nudge("want", phrase, verbose)


@app.command()
def meh(phrase: str, verbose: bool = VERBOSE) -> None:
    """Tell the teacher now that you want less of a topic."""
    _nudge("meh", phrase, verbose)


def _nudge(kind: str, phrase: str, verbose: bool) -> None:
    ctx = _ctx(verbose)
    try:
        with run_lock(ctx.cfg.root / ctx.cfg.data_dir, "nudge"), graceful_sigterm():   # learner.md is written: not under a run
            typer.echo(learner.nudge(ctx.cfg, ctx.conn, kind, phrase))
    except AlreadyRunning:
        _fail("a run is in progress; try again in a minute (or tag the article `more`/`less` in BazQux)", 5)


@app.command()
def profile(action: str = typer.Argument("show", help="show or revert"), verbose: bool = VERBOSE) -> None:
    """Show the learner model, or go back one version."""
    ctx = _ctx(verbose)
    if action == "revert":
        try:
            with run_lock(ctx.cfg.root / ctx.cfg.data_dir, "revert"), graceful_sigterm():
                v = learner.revert(ctx.cfg, ctx.conn)
        except AlreadyRunning:
            _fail("a run is in progress; try again in a minute", 5)
            return
        typer.echo("nothing to revert to" if v is None else f"reverted; now version {v}")
        return
    typer.echo(ctx.cfg.read_seed())
    typer.echo("\n---\n")
    typer.echo(ctx.cfg.read_learner())


@app.command()
def weekly(dry_run: bool = typer.Option(False, "--dry-run"), verbose: bool = VERBOSE) -> None:
    """Sunday's job: rewrite the learner model (gated), write the weekly note, run the drift check."""
    ctx = _ctx(verbose)
    try:
        with run_lock(ctx.cfg.root / ctx.cfg.data_dir, "weekly"), graceful_sigterm():
            _weekly(ctx, dry_run)
    except AlreadyRunning:
        _fail("a run is in progress; the weekly job will be run again by launchd next Sunday, or run it by hand later", 5)


def _weekly(ctx: pipeline.Context, dry_run: bool) -> None:
    run_id = store.begin_run(ctx.conn, "weekly")
    try:
        result = learner.weekly_rewrite(ctx.cfg, ctx.llm, ctx.conn, run_id, dry_run=dry_run)
        typer.echo(result["reason"])
        if result.get("diff"):
            typer.echo(result["diff"])
        drift = _drift_check(ctx, run_id)
        if drift:
            typer.echo(drift)
        note = dashboard.weekly_note(ctx.cfg, ctx.conn, result)
        typer.echo(f"weekly note: {note}")
        dashboard.render(ctx.cfg, ctx.conn)
        store.finish_run(ctx.conn, run_id, True, summary=f"weekly: {result['reason']}" + (f"; {drift}" if drift else ""))
        if not result["applied"] and result.get("gate") and not result["gate"]["passed"]:
            ctx.heartbeat.fail(f"weekly learner rewrite rejected: {result['reason']}")
    except Exception as exc:  # noqa: BLE001
        store.finish_run(ctx.conn, run_id, False, error=f"{type(exc).__name__}: {exc}")
        ctx.heartbeat.fail(f"weekly failed: {exc}")
        _fail(f"weekly failed: {exc}")


def _drift_check(ctx: pipeline.Context, run_id: int) -> str:
    """Re-score 2% of the week's articles; a large shift means the model behind the API changed."""
    conn, cfg = ctx.conn, ctx.cfg
    rows = conn.execute(
        """SELECT a.*, s.base_score FROM scores s JOIN articles a ON a.id = s.article_id
           WHERE s.scored_at >= ? AND s.model = ? AND a.text != '' ORDER BY RANDOM() LIMIT ?""",
        (store.days_ago_iso(7), cfg.models.teacher, 30),
    ).fetchall()
    if len(rows) < 10:
        return ""
    teacher = ctx.teacher()
    from .teacher import compose

    diffs = []
    for i in range(0, len(rows), cfg.scoring.batch_size):
        batch = rows[i: i + cfg.scoring.batch_size]
        verdicts = teacher.judge_batch(batch, run_id)
        for r in batch:
            v = verdicts.get(r["id"])
            if v is not None:
                diffs.append(compose(v, cfg, teacher.expansion_topics) - float(r["base_score"]))
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
def report_cmd(days: int = typer.Option(30, "--days"), verbose: bool = VERBOSE) -> None:
    """The monthly feed report with proposals; written to the dashboard folder."""
    ctx = _ctx(verbose)
    path = report.render(ctx.cfg, ctx.conn, days=days)
    typer.echo(path.read_text(encoding="utf-8"))


@app.command("dashboard")
def dashboard_cmd(verbose: bool = VERBOSE) -> None:
    """Regenerate the dashboard now."""
    ctx = _ctx(verbose)
    typer.echo(str(dashboard.render(ctx.cfg, ctx.conn)))


def triage_alias() -> None:
    """The old command name, kept for one release: a note on stderr, then the same program."""
    typer.echo("note: `triage` is now `viveka`; the old name goes away in the next release", err=True)
    app()


if __name__ == "__main__":
    app()
