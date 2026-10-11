"""The command line: argument parsing and printing only. Every command calls `bodh.api.App`,
which the console and the MCP server call too. The commands come in eight groups named by when
they are used (`bodh --help` lists them); each has examples at the end of its own help."""

from __future__ import annotations

import functools
import logging
import sys
import time
from pathlib import Path
from typing import Any

import click
import typer

from . import __version__, api, store
from .config import Config, load_config, system_timezone
from .errors import BodhError, ConfigError, LockedError
from .locks import AlreadyRunning

HELP = "bodh (बोध): keeps your BazQux unread list worth reading."

EPILOG = """\
\b
Set up        init, doctor, keys, models, version
Every run     run, catch-up, serve
Look          status, progress, why, find, list
Cut           prune, undo, clear-old
Teach         mark, steer, profile, tags, eval
Control       stop, schedule, autocut, safety, console
Housekeeping  backup, rebuild, weekly, report, dashboard, upgrade

\b
Every command takes -v for debug lines; --json prints one object (docs/json.md); --dry-run
shows and changes nothing; --yes skips the question. Exit codes: 1 an error, 2 configuration,
3 BazQux, 4 the model, 5 a run in progress, 130 stopped. Errors are one line on stderr.
Shell completion: bodh --install-completion.
"""

app = typer.Typer(
    name="bodh",
    help=HELP,
    epilog=EPILOG,
    no_args_is_help=True,
    add_completion=True,
    rich_markup_mode=None,
    pretty_exceptions_show_locals=False,
)
log = logging.getLogger(__name__)

EXIT_CONFIG = 2  # no usable config.yaml
VERBOSE = typer.Option(False, "-v", "--verbose", help="Debug-level log lines in the terminal.")
JSON = typer.Option(False, "--json", help="One JSON object on stdout, nothing else.")
_secret_values: list[str] = []  # what `_fail` must never print, whatever an error message carries


def _config() -> Config:
    """config.yaml, or a one-line refusal: every command but `version` and `init` needs it."""
    try:
        return load_config()
    except ConfigError as exc:
        _fail(str(exc), EXIT_CONFIG)
        raise  # unreachable; keeps the type checker honest
    except Exception as exc:  # a YAML or validation error the loader did not translate: still exit 2
        _fail(f"config.yaml does not load: {exc}", EXIT_CONFIG)
        raise


def _app(verbose: bool = False) -> api.App:
    """The project, opened: configuration, secrets (remembered for the scrubber), logging."""
    cfg = _config()
    found = api.App(cfg=cfg, verbose=verbose)
    _secret_values[:] = found.secrets.values()
    return found


def _fail(msg: str, code: int = 1) -> None:
    """One line on stderr and the exit code; a secret that found its way into the message
    (an API echoing a header, a URL with a token) is replaced, never printed."""
    for value in _secret_values:
        msg = msg.replace(value, "[secret]")
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(code)


def _handled(fn: Any) -> Any:
    """Every command: an error bodh raised on purpose is one plain line on stderr and its exit
    code (2 configuration, 3 BazQux, 4 the model, 5 a run in progress, 1 anything else)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (LockedError, AlreadyRunning) as exc:
            _fail(str(exc) or "another run is in progress; `bodh stop` it or wait", 5)
        except BodhError as exc:
            _fail(str(exc), exc.exit_code)

    return wrapper


def _emit(payload: dict[str, Any]) -> None:
    typer.echo(api.dumps(api.with_schema(payload)))


def _interactive() -> bool:
    """Someone is at the keyboard (so a question can be asked); a script or launchd is not."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _renamed(old: str, new: str) -> None:
    """The one line an old command name prints before running the new one (one release)."""
    typer.echo(
        f"note: `bodh {old}` is now `bodh {new}`; the old name goes away in the next release", err=True
    )


# == Set up ===========================================================================


@app.command()
@_handled
def version(
    check: bool = typer.Option(False, "--check", help="Also ask PyPI for the newest release."),
    as_json: bool = JSON,
    verbose: bool = VERBOSE,
) -> None:
    """The version of bodh, and with --check the newest on PyPI.

    \b
    Examples:
      bodh version
      bodh version --check
    """
    from . import upgrade

    newest = upgrade.newest_on_pypi() if check else None
    if as_json:
        _emit({"version": __version__, "newest": newest, "channel": upgrade.channel().name})
        return
    typer.echo(f"bodh {__version__}")
    if check:
        typer.echo(upgrade.check_text(newest))


@app.command()
@_handled
def init(
    directory: Path | None = typer.Argument(None, help="Where the project goes (default ~/bodh)."),
    bazqux_email: str | None = typer.Option(None, "--bazqux-email", help="Your BazQux login."),
    bazqux_password: str | None = typer.Option(
        None, "--bazqux-password", help="Its password (or set BAZQUX_PASSWORD in the environment)."
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="anthropic, openai, gemini, openrouter or openai-compatible."
    ),
    key: str | None = typer.Option(None, "--key", help="The provider's API key (or the service's base URL)."),
    judge: str | None = typer.Option(None, "--judge", help="The judge, as provider/model-id."),
    who: str | None = typer.Option(None, "--who", help="Profile: who you are."),
    knows: str | None = typer.Option(None, "--knows", help="Profile: what you already know well."),
    goal: str | None = typer.Option(None, "--goal", help="Profile: what reading is for this year."),
    decisions: str | None = typer.Option(None, "--decisions", help="Profile: the decisions in front of you."),
    never: str | None = typer.Option(None, "--never", help="Profile: what is never worth attention."),
    timezone: str | None = typer.Option(None, "--timezone", help="IANA name; default the machine's."),
    live: bool = typer.Option(
        True, "--live/--no-live", help="Check the login and the key with one call each."
    ),
    install_schedule: bool | None = typer.Option(
        None, "--install-schedule/--no-install-schedule", help="Schedule the jobs when doctor passes."
    ),
    yes: bool = typer.Option(False, "--yes", help="Take the defaults for anything not given."),
    verbose: bool = VERBOSE,
) -> None:
    """A new project in one sitting: the directory, your BazQux login, a provider and its key
    (each checked live), five questions that become your profile, the timezone; then doctor
    runs and the schedule is offered. Every answer has a flag, for scripts.

    \b
    Examples:
      bodh init                               the questions, one by one
      bodh init ~/reading --provider openai --judge openai/gpt-5-mini
      bodh init --yes --bazqux-email EMAIL --key KEY --who "..." --knows "..." \\
                --goal "..." --decisions "..." --never "..." --no-live
    """
    import os

    from . import init as initmod

    def ask(text: str, given: str | None, default: str = "", hide: bool = False) -> str:
        if given is not None:
            return given
        if yes or not _interactive():
            return default
        return str(typer.prompt(text, default=default, hide_input=hide, show_default=bool(default)))

    answers = initmod.Answers(
        directory=Path(
            ask(
                "Where does the project go?",
                str(directory) if directory else None,
                str(initmod.default_directory()),
            )
        )
    )
    answers.bazqux_email = ask("BazQux e-mail", bazqux_email)
    answers.bazqux_password = ask(
        "BazQux password", bazqux_password or os.environ.get("BAZQUX_PASSWORD"), hide=True
    )
    answers.provider = ask(
        "Model provider (anthropic, openai, gemini, openrouter, openai-compatible)", provider, "anthropic"
    )
    key_prompt = (
        "The service's base URL (e.g. http://localhost:11434/v1)"
        if answers.provider == "openai-compatible"
        else f"{answers.provider} API key"
    )
    answers.key = ask(key_prompt, key, hide=answers.provider != "openai-compatible")
    answers.judge = ask("The judge (provider/model-id)", judge, initmod.default_judge(answers.provider))
    given = {"who": who, "knows": knows, "goal": goal, "decisions": decisions, "never": never}
    for name, question in initmod.QUESTIONS:
        answers.about[name] = ask(question, given[name])
    answers.timezone = ask("Timezone (IANA name)", timezone, system_timezone())
    answers.live = live
    if install_schedule is None:
        install_schedule = (
            False
            if (yes or not _interactive())
            else typer.confirm("Schedule the jobs on this machine?", default=True)
        )
    answers.install_schedule = install_schedule
    _secret_values[:] = [v for v in (answers.bazqux_password, answers.key, answers.bazqux_email) if v]
    outcome = initmod.init(answers, say=typer.echo)
    if not outcome.doctor_ok:
        _fail("not ready yet: fix what doctor lists above, then `bodh doctor` again", 1)
    typer.echo(
        f"ready: {outcome.root}. Next: `cd {outcome.root}` (or export BODH_HOME={outcome.root}), "
        "`bodh run --shadow` for a first scoring-only pass"
        + ("" if answers.install_schedule else ", `bodh schedule install` to schedule the jobs")
    )


@app.command()
@_handled
def doctor(
    live: bool = typer.Option(False, "--live", help="Also log in to BazQux and call each role's model once."),
    values: bool = typer.Option(False, "--values", help="Print every setting in effect with its source."),
    verbose: bool = VERBOSE,
) -> None:
    """Configuration and connectivity checks, one line each. Exits 1 when a check fails, so an
    installer stops on a broken install. Run after every update.

    \b
    Examples:
      bodh doctor               config, profile, prompts, keys, database
      bodh doctor --live        also BazQux and each role's model (a few cents)
      bodh doctor --values      every setting with where it came from
    """
    found = _app(verbose)
    report = found.doctor(live=live, values=values, say=typer.echo)
    if report.problems:
        typer.echo(f"\n{len(report.problems)} problem(s); see above", err=True)
        raise typer.Exit(1)
    typer.echo("ok")


@app.command()
@_handled
def keys(
    action: str = typer.Argument("list", help="list, or set <provider>"),
    provider: str | None = typer.Argument(
        None, help="anthropic, openai, gemini, openrouter or openai-compatible"
    ),
    keychain: bool = typer.Option(
        False, "--keychain", help="Store in the system keychain instead of .env (pip install bodh[keychain])."
    ),
    as_json: bool = JSON,
    verbose: bool = VERBOSE,
) -> None:
    """Which provider keys are set (never a value, never a prefix), or set one with hidden
    input: .env with owner-only permissions, or the keychain. Lookup order: .env, the
    environment, the keychain.

    \b
    Examples:
      bodh keys                     one line per provider: set or not set
      bodh keys set openai          asks for the key, writes .env
      bodh keys set gemini --keychain
    """
    found = _app(verbose)
    if action == "list":
        rows = found.keys()
        if as_json:
            _emit({"keys": rows})
            return
        for row in rows:
            extra = " (the base URL; OPENAI_COMPATIBLE_API_KEY is optional)" if row["optional"] else ""
            typer.echo(
                f"{row['provider']:<18} {row['variable']:<28} {'set' if row['set'] else 'not set'}{extra}"
            )
        return
    if action != "set" or not provider:
        _fail("say `bodh keys list` or `bodh keys set <provider>`")
        return
    from .providers.base import PROVIDERS

    if provider not in PROVIDERS:
        _fail(f"unknown provider {provider!r}; one of {', '.join(PROVIDERS)}")
        return
    value = typer.prompt(PROVIDERS[provider], hide_input=True, default="", show_default=False).strip()
    if not value:
        _fail("nothing entered; the key is unchanged")
        return
    typer.echo(found.set_key(provider, value, keychain=keychain))
    if provider == "openai-compatible":
        url = typer.prompt(
            "OPENAI_COMPATIBLE_BASE_URL (where the service is, e.g. http://localhost:11434/v1)", default=""
        ).strip()
        if url:
            path = found.set_env_value("OPENAI_COMPATIBLE_BASE_URL", url)
            typer.echo(f"OPENAI_COMPATIBLE_BASE_URL: written to {path}")


@app.command()
@_handled
def models(
    action: str = typer.Argument("list", help="list, or compare"),
    with_model: str | None = typer.Option(
        None, "--with", help="compare: the second model, as provider/model-id."
    ),
    sample: int = typer.Option(
        50, "--sample", min=1, max=500, help="compare: how many judged unread articles."
    ),
    yes: bool = typer.Option(False, "--yes", help="compare: skip the confirmation."),
    verbose: bool = VERBOSE,
) -> None:
    """The roles and their models, the providers with keys, the prices and the capabilities;
    or compare the judge with a second model on a sample of already-judged unread articles
    (band agreement, the largest disagreements, cost per 1,000, seconds per article).
    Nothing stored changes.

    \b
    Examples:
      bodh models
      bodh models compare --with openai/gpt-5-mini
      bodh models compare --with gemini/gemini-2.5-flash --sample 20 --yes
    """
    from . import comparing

    found = _app(verbose)
    if action == "list":
        typer.echo(found.models_text())
        return
    if action != "compare":
        _fail("say `bodh models list` or `bodh models compare --with provider/model`")
        return
    if not with_model:
        _fail("compare needs --with provider/model-id")
        return
    plan = found.compare_plan(with_model, sample)
    typer.echo(plan["preview"])
    if plan["n"] == 0:
        return
    if not yes and not typer.confirm("Judge them with both models?"):
        raise typer.Exit(0)
    result = found.compare(with_model, sample, on_progress=lambda d, t: typer.echo(f"  {d}/{t}", err=True))
    typer.echo(comparing.render_comparison(result))


# == Every run ========================================================================


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
    rejudge: bool = typer.Option(
        False, "--rejudge", help="Have the judge read again every unread article another model judged."
    ),
    as_json: bool = JSON,
    verbose: bool = VERBOSE,
) -> None:
    """One full pass: feedback, new articles, judgments, tags, autocut, the dashboard, the
    heartbeat. This is what the schedule runs every 30 minutes. Answers 0 when another run
    holds the lock.

    \b
    Examples:
      bodh run                      the scheduled pass
      bodh run --shadow             the same, cutting nothing
      bodh run --sync --limit 20    twenty judgments now, no batch
      bodh run -v                   the phases as they happen
    """
    found = _app(verbose)
    try:
        res = found.run(shadow=shadow, limit=limit, sync=sync, workers=workers, rejudge=rejudge)
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
    if as_json:
        _emit({"run": _run_dict(res)})
    else:
        typer.echo(res.summary())


def _run_dict(res: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return {**asdict(res), "summary": res.summary()}


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


@app.command("catch-up")
@_handled
def catch_up(
    workers: int = typer.Option(8, "--workers", help="Model calls running at once."),
    chunk: int = typer.Option(1000, "--chunk", help="Articles per round."),
    all_ages: bool = typer.Option(False, "--all-ages", help="Also articles older than limits.max_age_days."),
    model: str | None = typer.Option(
        None, "--model", help="The model to judge with; default catch_up.model."
    ),
    budget: float | None = typer.Option(None, "--budget", help="Raise today's budget to this many dollars."),
    max_seconds: float | None = typer.Option(
        None, "--max-seconds", help="Start no new call after this long; run again to continue."
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation."),
    rejudge: bool = typer.Option(
        False,
        "--rejudge",
        help="The judge (not the quick judge) also reads what other models judged, whatever they rated.",
    ),
    verbose: bool = VERBOSE,
) -> None:
    """One deliberate pass over everything pending, as fast as the API allows: synchronous, many
    calls at once, with the quick judge unless --model says otherwise. Judges and tags; cuts
    nothing; the judge reads again what the quick judge rates highly (catch_up.rejudge_at).
    Ctrl-C and run it again to continue. Watch it with `bodh progress --watch`.

    \b
    Examples:
      bodh catch-up                           the quick judge over the pending pile
      bodh catch-up --all-ages --budget 40    the whole backlog, today's cap raised
      bodh catch-up --model claude-sonnet-5-5 --max-seconds 600
      bodh catch-up --rejudge                 the judge re-reads what other models judged
    """
    from . import logs

    found = _app(verbose)
    if not verbose:  # the progress line owns the terminal; the log file keeps the detail
        logs.quiet_terminal()
    plan = found.catch_up_plan(model=model, all_ages=all_ages, budget=budget, rejudge=rejudge)
    if plan.pending == 0:
        typer.echo(
            "nothing pending" + ("" if all_ages else " within the age window (--all-ages for the rest)")
        )
        return
    again = (
        f" The judge reads again what it rates {plan.rejudge_at:.0f} or more, on the next runs."
        if plan.quick and plan.rejudge_at is not None
        else ""
    )
    typer.echo(
        f"{plan.pending:,} articles pending{' (any age)' if all_ages else ''}; about ${plan.estimate_usd:.2f} "
        f"with {plan.judge}, {workers} calls at once.{again}"
    )
    if plan.over_budget and budget is None:
        typer.echo(
            f"today's budget has ${plan.budget_left:.2f} left; pass --budget {plan.budget_needed:.0f} "
            "to let the catch-up finish"
        )
    if not yes and not typer.confirm("Start the catch-up?"):
        raise typer.Exit(0)
    show = _catch_up_progress_line(plan.pending, time.monotonic())
    try:
        total = found.catch_up(
            workers=workers,
            chunk=chunk,
            all_ages=all_ages,
            budget=budget,
            model=model,
            max_seconds=max_seconds,
            rejudge=rejudge,
            on_progress=show,
        )
    except AlreadyRunning:
        _fail("another run is in progress; wait for it (or `bodh schedule pause`) and try again", 5)
        return
    typer.echo("")
    _catch_up_report(total)


def _catch_up_progress_line(pending: int, started: float) -> Any:
    """The one line that redraws in the terminal while a catch-up runs."""

    def show(total: dict[str, Any], stats: dict[str, Any]) -> None:
        done = total["scored"] + total["dupes"] + stats["scored"] + stats["dupes"]
        mins = max(1e-6, (time.monotonic() - started) / 60)
        rate = done / mins
        left_n = max(0, pending - done)
        eta = f"{left_n / rate:.0f} min" if rate > 0 else "-"
        typer.echo(
            f"\r  judged {done:,} of {pending:,} · {rate:.0f}/min · ETA {eta} · "
            f"failed batches {total['failed'] + stats['failed_batches']} · round {total['rounds'] + 1}   ",
            nl=False,
        )

    return show


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
    if total.get("halted"):
        typer.echo(f"{total['halted']}; check the provider's status page and run again later")
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
        rejudge=False,
        verbose=verbose,
    )


@app.command()
@_handled
def serve(
    every: int = typer.Option(30, "--every", min=1, help="Minutes between runs."),
    port: int = typer.Option(8766, "--port", help="The console's port."),
    host: str = typer.Option("127.0.0.1", "--host", help="127.0.0.1 keeps the console local."),
    no_console: bool = typer.Option(False, "--no-console", help="The schedule only."),
    verbose: bool = VERBOSE,
) -> None:
    """The schedule and the console in one process: a run every --every minutes, the weekly
    rewrite on Sunday mornings, the report on the first of the month, the console on its port.
    For Docker, Windows, and any machine without launchd or systemd. Ctrl-C stops it.

    \b
    Examples:
      bodh serve                     every 30 minutes, console on http://127.0.0.1:8766
      bodh serve --every 15 --no-console
    """
    from . import serve as servemod

    found = _app(verbose)
    typer.echo(
        f"serving: a run every {every} min"
        + ("" if no_console else f", console on http://{host}:{port}")
        + " (Ctrl-C stops it)"
    )
    servemod.serve(found.cfg, found.conn, every_minutes=every, host=host, port=port, console=not no_console)


# == Look =============================================================================


@app.command()
@_handled
def status(as_json: bool = JSON, verbose: bool = VERBOSE) -> None:
    """The whole system at a glance: what is running, the schedule, the unread, spend, the last runs.

    \b
    Examples:
      bodh status
      bodh status --json
    """
    from . import jobs

    found = _app(verbose)
    st = found.status()
    if as_json:
        _emit({"status": st})
        return
    typer.echo(jobs.render_status_text(st))
    typer.echo("\n(`bodh progress` for the unread by folder, `bodh console` for the console)")


@app.command()
@_handled
def progress(
    watch: bool = typer.Option(False, "--watch", help="Redraw every --every seconds until Ctrl-C."),
    every: int = typer.Option(5, "--every", help="Seconds between redraws with --watch."),
    as_json: bool = JSON,
    verbose: bool = VERBOSE,
) -> None:
    """How far the judge has got: judged, with the model, pending, by folder; rate and ETA while
    a run is going; batches; spend; the log.

    \b
    Examples:
      bodh progress
      bodh progress --watch          redraws every 5 s
      bodh progress --json
    """
    from . import progress as progressmod

    found = _app(verbose)
    while True:
        snap = found.progress()
        if as_json:
            _emit({"progress": snap})
            return
        text = progressmod.render_text(snap)
        if watch:
            click.clear()
            typer.echo(text + f"\n\n(redraws every {every}s; Ctrl-C to stop)")
            try:
                time.sleep(max(2, every))
            except KeyboardInterrupt:
                raise typer.Exit(0) from None
        else:
            typer.echo(text)
            return


@app.command()
@_handled
def why(
    article: str = typer.Argument(..., help="The article's id (either form) or its URL."),
    as_json: bool = JSON,
    verbose: bool = VERBOSE,
) -> None:
    """One article: its score and sub-scores, the lesson, the concept, its state, your record.
    This is the explanation of the system, one article at a time.

    \b
    Examples:
      bodh why tag:google.com,2005:reader/item/00000000a1b2c3d4
      bodh why https://example.org/post
      bodh why 1234 --json
    """
    found = _app(verbose)
    view = found.why(article)
    if view is None:
        _fail("not found (an id of either form, or the URL of an article a run has fetched)")
        return
    if as_json:
        _emit({"article": view.to_dict()})
        return
    typer.echo(f"{view.title}\n{view.url}\nfeed: {view.feed} · folder: {view.folder}")
    if not view.judged:
        typer.echo("not judged yet")
    else:
        typer.echo(
            f"score {view.score:.1f} (base {view.base_score:.1f}"
            + (f", review {view.review_score:.1f}" if view.review_score is not None else "")
            + f") · band {view.band}"
        )
        typer.echo(
            f"quality {view.quality} · learning {view.learning} · fit {view.fit} · breadth {view.breadth}"
            f" · relevance {view.relevance} · confidence {view.confidence} · kind {view.kind} · flags "
            f"{', '.join(view.flags) or 'none'} · judged by {view.judged_by} ({view.prompt_version})"
        )
        typer.echo(f"concept: {view.concept}\nlesson: {view.lesson}")
    typer.echo(
        f"state: {view.status}"
        + (f" ({view.cut_mode} at {view.cut_at}, run {view.cut_run_id})" if view.cut_mode else "")
        + (" · starred" if view.starred else "")
        + (" · protected" if view.protected else "")
        + (" · new ground" if view.new_ground else "")
    )
    if view.record:
        typer.echo("your record: " + ", ".join(f"{m['kind']} ({m['at'][:10]})" for m in view.record))


@app.command()
@_handled
def find(
    words: str = typer.Argument(..., help="Words to look for in titles and lessons (all must match)."),
    unread: bool = typer.Option(False, "--unread", help="Only what is still unread."),
    limit: int = typer.Option(api.FIND_LIMIT, "--limit", min=1, help="At most this many."),
    as_json: bool = JSON,
    verbose: bool = VERBOSE,
) -> None:
    """Find articles by words in their title or lesson: id, score, band, state, feed, so nobody
    types a forty-character id.

    \b
    Examples:
      bodh find "liquid cooling"
      bodh find seagrass --unread
      bodh find "grant" --json
    """
    found = _app(verbose)
    hits = found.find(words, unread=unread, limit=limit)
    if as_json:
        _emit({"query": words, "unread": unread, "hits": [h.to_dict() for h in hits]})
        return
    if not hits:
        typer.echo("nothing found")
        return
    for h in hits:
        score = f"{h.score:5.1f}" if h.score is not None else "   - "
        typer.echo(
            f"{score}  {h.band or '-':<9} {h.status or '-':<7} {h.title[:60]:<60}  [{h.feed[:24]}]  {h.id}"
        )


@app.command("list")
@_handled
def list_command(
    band: float = typer.Option(api.LIST_BAND, "--band", min=0, max=100, help="This score and above."),
    folder: str | None = typer.Option(None, "--folder", help="One folder only."),
    limit: int = typer.Option(api.LIST_LIMIT, "--limit", min=1, help="At most this many."),
    as_json: bool = JSON,
    verbose: bool = VERBOSE,
) -> None:
    """Today's reading list: the unread articles at --band and above, best first, each with its
    lesson, concept and feed (the console's list, in text).

    \b
    Examples:
      bodh list
      bodh list --band 50 --folder Essays
      bodh list --json
    """
    found = _app(verbose)
    rows = found.reading_list(band=band, folder=folder, limit=limit)
    if as_json:
        _emit({"band": band, "folder": folder, "articles": [r.to_dict() for r in rows]})
        return
    if not rows:
        typer.echo(f"nothing unread at {band:.0f} or above" + (f" in {folder}" if folder else ""))
        return
    for r in rows:
        typer.echo(f"{r.score:5.1f}  {r.title[:70]}  [{r.feed[:24]}]")
        typer.echo(f"       {r.lesson}" + (f"  ({r.concept})" if r.concept else "") + f"\n       {r.url}")


# == Cut ==============================================================================


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
    """Cut the worst of what is still unread. Default keeps prune.keep (40), spread over
    concepts. Shows the boundary, the counts by band and folder, the ten best that would go, and
    asks first. Run again to cut deeper. `bodh undo` reverses it.

    \b
    Examples:
      bodh prune --dry-run           what would go, and nothing goes
      bodh prune                     keep the best 40
      bodh prune --below 65 --yes    everything under 65, no question
    """
    if sum(x is not None for x in (keep, fraction, below)) > 1:
        _fail("choose one of --keep, --fraction, --below")
    if keep is not None and keep < 0:
        _fail("--keep must be 0 or more")
    if fraction is not None and not 0 <= fraction <= 1:
        _fail("--fraction is a share between 0 and 1")
    found = _app(verbose)
    mode = (
        f"--below {below:g}"
        if below is not None
        else f"--fraction {fraction:g}"
        if fraction is not None
        else f"keep {keep if keep is not None else found.cfg.prune.keep}"
    )
    try:
        plan = found.prune_plan(keep=keep, fraction=fraction, below=below)
        _show_prune(plan, dry_run=dry_run)
        if dry_run or not plan.cut:
            return
        # the question is asked with the lock released: a run must not be skipped over a prompt
        question = (
            f"Cut these {plan.cut} ({mode})? It marks them read in BazQux; `bodh undo` reverses it; "
            f"this will be run {plan.next_run_id}"
        )
        if not yes and _interactive() and not typer.confirm(question):
            raise typer.Exit(0)
        n, _sel = found.prune(keep=keep, fraction=fraction, below=below, confirmed=set(plan.cut_ids))
    except KeyboardInterrupt:
        _fail("stopped; the next run finishes the tags and read marks, `bodh undo` reverses the prune", 130)
        return
    if n:
        typer.echo(f"cut {n}; `bodh undo` reverses it (run {store.last_runs(found.conn, 1)[0]['id']})")


def _show_prune(plan: api.PrunePlan, dry_run: bool) -> None:
    verb = "would cut" if dry_run else "cut"
    typer.echo(
        f"{verb} {plan.cut} of {plan.unread_scored} scored unread articles; boundary {plan.boundary:.0f} "
        "(the lowest score that stays)"
        if plan.boundary is not None
        else f"{verb} {plan.cut} articles"
    )
    if not plan.cut:
        return
    typer.echo("by band: " + ", ".join(f"{b} {n}" for b, n in plan.by_band.items()))
    typer.echo("by folder: " + ", ".join(f"{f} {n}" for f, n in list(plan.by_folder.items())[:12]))
    typer.echo("the best of what goes (this is the boundary you are choosing):")
    for r in plan.best:
        typer.echo(f"  {r.score:5.1f}  {r.title[:66]}  [{r.feed[:22]}]")
        if r.lesson:
            typer.echo(f"         {r.lesson[:100]}")


@app.command()
@_handled
def undo(
    run_id: int | None = typer.Option(
        None, "--run", help="The run whose cut to reverse (default: the last cut)."
    ),
    verbose: bool = VERBOSE,
) -> None:
    """Reverse the last cut, or one run's: its articles are unread again, the cut tag removed,
    and autocut leaves them alone from then on.

    \b
    Examples:
      bodh undo
      bodh undo --run 212
    """
    found = _app(verbose)
    which, n = found.undo(run_id)
    if which is None:
        typer.echo("nothing to undo")
    elif n == 0:
        typer.echo(f"run {which} cut nothing (or its cut was undone already)")
    else:
        typer.echo(f"restored {n} articles from run {which}")


@app.command("clear-old")
@_handled
def clear_old(
    days: int = typer.Option(14, "--days", help="Everything unread older than this many days goes."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count, change nothing."),
    verbose: bool = VERBOSE,
) -> None:
    """Mark every unread article older than N days read and tag it `cut`. Stars, the must-read
    stream, protected folders and rescued articles are exempt. Reversible with `bodh undo`.

    \b
    Examples:
      bodh clear-old --days 14 --dry-run
      bodh clear-old --days 30 --yes
    """
    found = _app(verbose)
    candidates = found.clear_old_count(days)
    typer.echo(f"{candidates} unread articles older than {days} days (stars and protections exempt)")
    if dry_run or candidates == 0:
        return
    if not yes and not typer.confirm(
        f"Mark them read? It is reversible with `bodh undo`; this will be run {found.next_run_id()}"
    ):
        raise typer.Exit(0)
    _, done = found.clear_old(days)
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


# == Teach ============================================================================


@app.command()
@_handled
def mark(
    article: str = typer.Argument(..., help="The article's id (either form) or its URL."),
    verdict: str = typer.Argument(..., help="great, good, meh, bad, basic, heavy, more, less or rescue"),
    verbose: bool = VERBOSE,
) -> None:
    """Your verdict on an article from the terminal, the same as its tag in BazQux: a grade
    (great, good, meh, bad), the level missed (basic, heavy), a steer by its concept (more, less),
    or `rescue`, which brings a cut article back unread and keeps it from every cut.

    \b
    Examples:
      bodh mark https://example.org/post great
      bodh mark 1234 rescue
    """
    found = _app(verbose)
    try:
        typer.echo(found.mark(article, verdict))
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
    mark(article=article_id, verdict="great" if judgment == "star" else "bad", verbose=verbose)


@app.command()
@_handled
def steer(
    direction: str = typer.Argument(..., help="more or less"),
    phrase: str = typer.Argument(..., help="the concept"),
    verbose: bool = VERBOSE,
) -> None:
    """Tell the judge now that you want more, or less, of a concept: a dated line in the learned
    profile that it reads from the next run (the `more` and `less` tags do the same by article).

    \b
    Examples:
      bodh steer more "physical oceanography"
      bodh steer less "conference announcements"
    """
    found = _app(verbose)
    try:
        typer.echo(found.steer(direction, phrase))
    except AlreadyRunning:
        _fail("a run is in progress; try again in a minute (or tag the article `more`/`less` in BazQux)", 5)


@app.command(hidden=True)
@_handled
def want(phrase: str, verbose: bool = VERBOSE) -> None:
    """The name before October 2026 of `steer more`."""
    _renamed('want "x"', 'steer more "x"')
    steer(direction="more", phrase=phrase, verbose=verbose)


@app.command(hidden=True)
@_handled
def meh(phrase: str, verbose: bool = VERBOSE) -> None:
    """The name before October 2026 of `steer less`."""
    _renamed('meh "x"', 'steer less "x"')
    steer(direction="less", phrase=phrase, verbose=verbose)


@app.command()
@_handled
def profile(
    action: str = typer.Argument("show", help="show, revert or edit"), verbose: bool = VERBOSE
) -> None:
    """The student profile: show it (about, then learned), go back one version of the learned
    part, or open about.md in $EDITOR.

    \b
    Examples:
      bodh profile
      bodh profile edit
      bodh profile revert
    """
    if action not in ("show", "revert", "edit"):
        _fail(f"unknown action {action!r}: say show, revert or edit")
    found = _app(verbose)
    if action == "revert":
        v = found.profile_revert()
        typer.echo("nothing to revert to" if v is None else f"reverted; now version {v}")
        return
    if action == "edit":
        path = found.cfg.about_path
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        edited = click.edit(before, extension=".md")
        if edited is None or edited == before:
            typer.echo("unchanged")
            return
        path.write_text(edited, encoding="utf-8")
        typer.echo(f"wrote {path}; the judge reads it from the next run")
        return
    about, learned = found.profile()
    typer.echo(about)
    typer.echo("\n---\n")
    typer.echo(learned)


@app.command()
@_handled
def tags(as_json: bool = JSON, verbose: bool = VERBOSE) -> None:
    """The vocabulary: every tag bodh writes in BazQux and every tag you can add, and what each
    does. With `bodh why`, this is the explanation of the system.

    \b
    Examples:
      bodh tags
      bodh tags --json
    """
    found = _app(verbose)
    rows = found.tags()
    if as_json:
        _emit({"tags": [t.to_dict() for t in rows]})
        return
    typer.echo("bodh writes:")
    for t in rows:
        if t.writer == "bodh":
            typer.echo(f"  {t.tag:<12} {t.meaning}")
    typer.echo("you add (the next run reads them; a verdict stays on the article as your record):")
    for t in rows:
        if t.writer == "you":
            typer.echo(f"  {t.tag:<12} {t.group:<8} {t.meaning}")


@app.command("eval")
@_handled
def evals_command(
    set_path: Path | None = typer.Option(
        None, "--set", help="The set (default: evals/set.yaml in the checkout)."
    ),
    model: str | None = typer.Option(None, "--model", help="The judge to use; default models.judge."),
    cap: float = typer.Option(0.5, "--cap", min=0.0, help="Stop at this many dollars."),
    write: Path | None = typer.Option(None, "--write", help="Also write the results as JSON to this file."),
    profile_dir: Path | None = typer.Option(
        None, "--profile", help="The example student (default: profile.example/)."
    ),
    verbose: bool = VERBOSE,
) -> None:
    """Judge the evaluation set for the example student and print the agreement by band and
    the largest disagreements. A few cents.

    \b
    Examples:
      bodh eval
      bodh eval --model openai/gpt-5-mini --cap 0.20 --write evals/results.json
    """
    from . import evals

    found = _app(verbose)
    items = evals.load_set(set_path or evals.SET_PATH, found.cfg.tags.bands)
    judge_model = model or found.cfg.models.judge
    typer.echo(
        f"{len(items)} articles; about ${evals.estimate_usd(found.cfg, len(items), judge_model):.2f} with {judge_model}"
    )
    summary = evals.run_eval(
        found.ctx,
        items,
        model=model,
        cap_usd=cap,
        profile_dir=profile_dir or evals.EXAMPLE_PROFILE,
        on_progress=lambda done, total: typer.echo(f"  {done}/{total}", err=True),
    )
    typer.echo(evals.render_text(summary))
    if write is not None:
        typer.echo(f"written: {evals.write_results(summary, write)}")


# == Control ==========================================================================


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
    --all, stop everything: the run, the schedule and the console. Fail open: what was judged
    stays; a cut half-written to BazQux is finished by the next run.

    \b
    Examples:
      bodh stop
      bodh stop --all
    """
    found = _app(verbose)
    for line in found.stop(everything):
        typer.echo(line)


@app.command(hidden=True)
@_handled
def halt(verbose: bool = VERBOSE) -> None:
    """The name before October 2026 of `stop --all`."""
    _renamed("halt", "stop --all")
    stop(everything=True, verbose=verbose)


@app.command()
@_handled
def schedule(
    action: str = typer.Argument("status", help="status, pause, resume, kick, install or uninstall"),
    job: str = typer.Option("all", "--job", help="run, weekly, report, console or all"),
    kind: str | None = typer.Option(
        None, "--kind", help="install: launchd, systemd or taskschd (default: this machine's)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="install, uninstall: show what would be written and run."
    ),
    verbose: bool = VERBOSE,
) -> None:
    """The scheduled jobs: show them, pause them all (nothing fires until resume), resume, kick
    a run now, or install and uninstall them: launchd agents on a Mac, systemd user units on
    Linux, one Task Scheduler entry running `bodh serve` on Windows.

    \b
    Examples:
      bodh schedule                  the four jobs and their state
      bodh schedule pause            nothing fires until `bodh schedule resume`
      bodh schedule kick             a run now
      bodh schedule install          this machine's scheduler, this interpreter
      bodh schedule install --dry-run
    """
    from . import installers, jobs

    if job == "panel":  # the console's name before October 2026
        job = "console"
    found = _app(verbose)
    if action == "status":
        for name, label in jobs.JOBS.items():
            j = jobs.job_state(label)
            extra = f" · last exit {j['last_exit']}" if j["last_exit"] not in (None, 0) else ""
            typer.echo(f"{name:<8}{j['state']}{extra}  {j['plist'] or label}")
        return
    if action in ("install", "uninstall"):
        do = installers.install if action == "install" else installers.uninstall
        for line in do(found.cfg.root, kind=kind, dry_run=dry_run):
            typer.echo(line)
        return
    if action not in ("pause", "resume", "kick"):
        _fail("action must be status, pause, resume, kick, install or uninstall")
    if job != "all" and job not in jobs.JOBS:
        _fail("--job must be run, weekly, report, console or all")
    typer.echo(found.schedule(action, job))


@app.command()
@_handled
def autocut(state: str = typer.Argument(..., help="on or off"), verbose: bool = VERBOSE) -> None:
    """The automatic cut: on = autocut cuts below the cut-off from the next run; off = scoring-only
    mode, everything is judged and tagged, nothing is cut. Edits config.yaml in place.

    \b
    Examples:
      bodh autocut on
      bodh autocut off
    """
    if state not in ("on", "off"):
        _fail("say on or off")
    found = _app(verbose)
    typer.echo(found.set_autocut(state == "on"))


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
    """The console: status, run, stop, schedule, prune, undo, catch-up, log, dashboard, in one page.

    \b
    Examples:
      bodh console
      bodh console --port 8800
    """
    import traceback

    from .console import Console, console_log

    found = _app(verbose)
    typer.echo(
        f"console on http://{host}:{port}  (Ctrl-C stops it; the schedule keeps it alive when installed)"
    )
    try:
        Console(found.cfg, found.conn, host=host, port=port).serve()
    except KeyboardInterrupt:
        raise typer.Exit(0) from None
    except (
        Exception
    ) as exc:  # the console could not start (a port in use, say): the reason goes to console.log
        console_log(
            found.cfg.root / found.cfg.data_dir,
            f"failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}",
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
def safety(action: str = typer.Argument("status", help="status, or reset"), verbose: bool = VERBOSE) -> None:
    """The safety limit: an autocut that would cut nearly everything newly judged trips it, and
    autocut then cuts nothing until `bodh safety reset` says someone has looked.

    \b
    Examples:
      bodh safety
      bodh safety reset
    """
    from . import cutting as cutmod

    if action not in ("status", "reset"):
        _fail("say status or reset")
    found = _app(verbose)
    conn = found.conn
    tripped = cutmod.safety_tripped(conn)
    given_up = store.given_up(conn)
    if action == "reset":
        if cutmod.reset_safety(conn):
            typer.echo(
                f"safety limit reset (it tripped in run {tripped}); autocut cuts again from the next run"
            )
        else:
            typer.echo("the safety limit is not tripped")
        n = store.reset_judge_failures(conn)
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


# == Housekeeping =====================================================================


@app.command()
@_handled
def backup(
    keep: int = typer.Option(7, "--keep", min=1, help="Backups to keep, newest first."),
    verbose: bool = VERBOSE,
) -> None:
    """Copy the database to data/backups/bodh-<date>.db (the run does this once a day too).

    \b
    Examples:
      bodh backup
      bodh backup --keep 30
    """
    found = _app(verbose)
    path = found.backup(keep=keep)
    typer.echo(f"backup written: {path} ({len(found.backups())} kept)")


@app.command()
@_handled
def rebuild(
    dry_run: bool = typer.Option(False, "--dry-run", help="Count what would be written; write nothing."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation."),
    verbose: bool = VERBOSE,
) -> None:
    """After a lost database: read the band tags, the cut tag and the stars back from BazQux,
    so nothing already judged is judged again and nothing already cut is cut again. Rebuilt
    scores carry the band's threshold and a low confidence: autocut leaves them alone.

    \b
    Examples:
      bodh rebuild --dry-run
      bodh rebuild --yes
    """
    from . import recovery

    found = _app(verbose)
    if not dry_run and not yes and not typer.confirm("Rebuild scores and cuts from the tags in BazQux?"):
        raise typer.Exit(0)
    typer.echo(recovery.summary(found.rebuild(dry_run=dry_run)))


@app.command()
@_handled
def weekly(
    dry_run: bool = typer.Option(False, "--dry-run", help="Propose the rewrite, write nothing."),
    verbose: bool = VERBOSE,
) -> None:
    """Sunday's job: rewrite the learned profile (kept only if the eval gate passes), write the
    weekly note, run the drift check.

    \b
    Examples:
      bodh weekly
      bodh weekly --dry-run
    """
    found = _app(verbose)
    try:
        found.weekly(dry_run=dry_run, say=typer.echo)
    except AlreadyRunning:
        _fail("a run is in progress; the weekly job runs again next Sunday, or run it by hand later", 5)
    except BodhError as exc:
        _fail(f"weekly failed: {exc}", exc.exit_code)
    except Exception as exc:  # recorded on the run row and the heartbeat already; one line here
        _fail(f"weekly failed: {exc}", 1)


@app.command("report")
@_handled
def report_command(
    days: int = typer.Option(30, "--days", min=1, help="The window."), verbose: bool = VERBOSE
) -> None:
    """The feed report with proposals (unsubscribe, move to the strictest folder, protect),
    written to the dashboard folder and printed.

    \b
    Examples:
      bodh report
      bodh report --days 90
    """
    found = _app(verbose)
    typer.echo(found.report(days=days).read_text(encoding="utf-8"))


@app.command("dashboard")
@_handled
def dashboard_command(verbose: bool = VERBOSE) -> None:
    """Regenerate the dashboard now; prints where it was written.

    \b
    Examples:
      bodh dashboard
    """
    found = _app(verbose)
    typer.echo(str(found.dashboard()))


@app.command()
@_handled
def upgrade(
    run_it: bool = typer.Option(False, "--run", help="Run the upgrade command instead of printing it."),
    verbose: bool = VERBOSE,
) -> None:
    """The upgrade for the way this bodh was installed (pipx, uv, Homebrew, a one-file
    executable, a checkout, pip): printed, or run with --run. Run `bodh doctor` after.

    \b
    Examples:
      bodh upgrade            prints the command
      bodh upgrade --run
    """
    from . import upgrade as upgrademod

    ch = upgrademod.channel()
    newest = upgrademod.newest_on_pypi()
    typer.echo(f"bodh {__version__}, {ch.note}; {upgrademod.check_text(newest)}")
    if ch.command is None:
        return
    if not run_it:
        typer.echo("to upgrade: " + " ".join(ch.command))
        return
    code = upgrademod.run(ch)
    if code != 0:
        _fail(f"the upgrade command exited {code}", code)
    typer.echo("upgraded; run `bodh doctor` next")


def triage_alias() -> None:
    """The old command name, kept for one release: a note on stderr, then the same program."""
    typer.echo("note: `triage` is now `bodh`; the old name goes away in the next release", err=True)
    app()


if __name__ == "__main__":
    app()
