"""The command line, driven through typer's runner against a temporary project: every
command's happy path and its refusals, their exit codes, and the one rule that holds for all
of them, that nothing a command prints looks like a secret."""

from __future__ import annotations

import os
import re
import sqlite3
import time
import types
from pathlib import Path

import anthropic
import pytest
import yaml
from conftest import seed_scored
from fakes import FakeLLM, FakeReader, api_error, make_item
from typer.testing import CliRunner

from bodh import __version__, jobs, locks, recovery, run, store
from bodh.cli import app
from bodh.config import load_config
from bodh.readers.bazqux import ItemRef

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]

# The values the tests put in .env and in the fakes: a command may print none of them.
TEST_SECRETS = ["hunter2", "sk-ant-test-key-0123456789", "reader@example.com", "sk-ant-test-key"]
# Nor anything shaped like one: an API key, an e-mail address, BazQux's auth header or login form.
SECRET_SHAPES = [
    re.compile(p)
    for p in (r"sk-ant-[A-Za-z0-9_-]{8,}", r"[\w.+-]+@[\w-]+\.[\w.-]+", r"GoogleLogin auth=", r"Passwd=")
]
SECRET_ENV = ("BAZQUX_EMAIL", "BAZQUX_PASSWORD", "ANTHROPIC_API_KEY", "HEALTHCHECK_URL")
ENV_FILE = (
    "BAZQUX_EMAIL=reader@example.com\nBAZQUX_PASSWORD=hunter2\nANTHROPIC_API_KEY=sk-ant-test-key-0123456789\n"
)
ABOUT = "# About the student\nStudent: test.\n"
LEARNED = "## What the student has been learning\nx\n"


def invoke(args, input=None, env=None):
    """`runner.invoke`, then the check every command must pass: nothing it printed (stdout and
    stderr, mixed in `output`) looks like a secret."""
    result = runner.invoke(app, args, input=input, env=env)
    leaked = [p.pattern for p in SECRET_SHAPES if p.search(result.output)]
    leaked += [s for s in TEST_SECRETS if s in result.output]
    assert not leaked, (args, leaked, result.output)
    return result


@pytest.fixture(autouse=True)
def _secrets_out_of_the_environment():
    """`load_secrets` writes .env into os.environ, which outlives the command: every test here
    starts with no secret in the environment and leaves none behind for the next."""
    saved = {name: os.environ.pop(name, None) for name in SECRET_ENV}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _project(tmp_path, monkeypatch, config="timezone: UTC\n", about=True):
    """A temporary project the commands find through BODH_HOME, with the slow and external
    things stubbed: git in the profile folder, and the second that `stop --all` waits for stray
    processes to end."""
    (tmp_path / "config.yaml").write_text(config, encoding="utf-8")
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "learned.md").write_text(LEARNED, encoding="utf-8")
    if about:
        (tmp_path / "profile" / "about.md").write_text(ABOUT, encoding="utf-8")
    monkeypatch.setenv("BODH_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("bodh.profiler.git_commit", lambda *a, **k: True)
    monkeypatch.setattr(jobs, "time", types.SimpleNamespace(time=time.time, sleep=lambda _s: None))
    return tmp_path


def _env(root):
    """The three secrets, as install.sh leaves them in .env."""
    (root / ".env").write_text(ENV_FILE, encoding="utf-8")


def _fakes(root, monkeypatch, client=None):
    """Every command run after this talks to `client` (an empty FakeReader unless given) and to
    a FakeLLM instead of BazQux and the API. Returns (cfg, client, llm)."""
    cfg = load_config(root)
    conn = store.connect(cfg.db_path)  # the file exists from here on: the model opens its own connection
    llm = FakeLLM(cfg, conn)
    conn.close()
    client = FakeReader() if client is None else client
    monkeypatch.setattr(run.Context, "client", property(lambda self: client))
    monkeypatch.setattr(run.Context, "llm", property(lambda self: llm))
    return cfg, client, llm


class DatedReader(FakeReader):
    """A reader whose unread list carries BazQux's timestamps, which clear-old reads."""

    def __init__(self, items=(), timestamps=None, **kwargs):
        super().__init__(items, **kwargs)
        self.timestamps = dict(timestamps or {})

    def unread_ids(self):
        return [ItemRef(id=i, timestamp_usec=self.timestamps.get(i)) for i in self.unread]


# -- the project itself ---------------------------------------------------------


NEEDS_CONFIG = (
    ["doctor"],
    ["run", "--shadow"],
    ["score"],
    ["progress"],
    ["catch-up"],
    ["blast"],
    ["prune", "--dry-run"],
    ["undo"],
    ["backup"],
    ["rebuild", "--dry-run"],
    ["clear-old", "--dry-run"],
    ["bankruptcy", "--dry-run"],
    ["status"],
    ["stop"],
    ["halt"],
    ["schedule", "pause"],
    ["safety"],
    ["autocut", "on"],
    ["enforce", "off"],
    ["console"],
    ["panel"],
    ["why", "1"],
    ["mark", "1", "great"],
    ["label", "1", "star"],
    ["steer", "more", "x"],
    ["want", "x"],
    ["meh", "x"],
    ["profile"],
    ["weekly"],
    ["report"],
    ["dashboard"],
)


def test_refuses_without_config(tmp_path, monkeypatch):
    """A folder with no config.yaml gets a one-line refusal from every command but `version`,
    and nothing is created in it."""
    monkeypatch.delenv("BODH_HOME", raising=False)
    monkeypatch.delenv("TRIAGE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # a real ~/bodh on this machine must not answer
    monkeypatch.chdir(tmp_path)
    for args in NEEDS_CONFIG:
        result = invoke(args)
        assert result.exit_code == 2, (args, result.output)
        assert "no config.yaml" in result.output
    assert list(tmp_path.iterdir()) == []
    assert invoke(["version"]).exit_code == 0


def test_version_and_console_help_need_no_project(tmp_path, monkeypatch):
    """`version` prints the version and `console --help` the options; neither reads a config
    or starts anything."""
    monkeypatch.delenv("BODH_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    result = invoke(["version"])
    assert result.exit_code == 0 and result.output.strip() == f"bodh {__version__}"
    result = invoke(["console", "--help"])
    assert result.exit_code == 0, result.output
    for option in ("--port", "--host", "--verbose"):
        assert option in result.output, (option, result.output)
    assert list(tmp_path.iterdir()) == []


# -- the run lock -----------------------------------------------------------------


def test_commands_that_write_refuse_while_a_run_holds_the_lock(tmp_path, monkeypatch):
    """Whatever writes learned.md, the database or BazQux refuses with exit 5 while a run is
    in progress; `run` itself skips quietly (launchd fires it regardless) and `schedule kick`
    says why it did not kick."""
    root = _project(tmp_path, monkeypatch)
    item = make_item(1)
    old = int((time.time() - 30 * 86400) * 1_000_000)
    cfg, client, _ = _fakes(root, monkeypatch, DatedReader([item], timestamps={item.id: old}))
    conn = store.connect(cfg.db_path)
    store.upsert_article(conn, item, None)
    conn.close()
    with locks.run_lock(root / "data", "run"):
        for args in (
            ["weekly"],
            ["want", "reefs"],
            ["meh", "crypto"],
            ["profile", "revert"],
            ["steer", "less", "crypto"],
            ["prune", "--dry-run"],
            ["undo"],
            ["rebuild", "--yes"],
            ["clear-old", "--yes"],
            ["mark", "1", "great"],
            ["catch-up", "--yes"],
        ):
            result = invoke(args)
            assert result.exit_code == 5 and "run is in progress" in result.output, (args, result.output)
        result = invoke(["run", "--shadow"])
        assert result.exit_code == 0 and "another run is in progress; skipping" in result.output
        result = invoke(["schedule", "kick"])
        assert result.exit_code == 0 and "not kicked: run (pid" in result.output, result.output
    assert client.read == [] and client.tagged == []
    result = invoke(["want", "reefs"])
    assert result.exit_code == 0 and "wants more on reefs" in result.output
    assert "wants more on reefs" in (root / "profile" / "learned.md").read_text(encoding="utf-8")


# -- run ---------------------------------------------------------------------------


def test_run_exit_codes(tmp_path, monkeypatch):
    """ReaderError 3, ModelError 4, ConfigError 2 and LockedError 5 are one line each; a bug is
    exit 1 with "run failed"; Ctrl-C is 130; a lock already held is exit 0, because launchd
    fires the run every half hour whatever the last one is doing."""
    from bodh import run as runmod
    from bodh.errors import ConfigError, LockedError, ModelError, ReaderError

    root = _project(tmp_path, monkeypatch)
    for exc, code in (
        (ReaderError("BazQux is down"), 3),
        (ModelError("key rejected"), 4),
        (ConfigError("no about"), 2),
    ):

        def boom(*a, _exc=exc, **k):
            raise _exc

        monkeypatch.setattr(runmod, "run", boom)
        result = invoke(["run", "--shadow"])
        assert result.exit_code == code and f"error: {exc}" in result.output, (exc, result.output)

    def bug(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(runmod, "run", bug)
    result = invoke(["run", "--shadow"])
    assert result.exit_code == 1 and "error: run failed: boom" in result.output, result.output

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(runmod, "run", interrupted)
    result = invoke(["run", "--shadow"])
    assert result.exit_code == 130 and "stopped; the next run picks up" in result.output, result.output

    def locked(*a, **k):
        raise LockedError

    monkeypatch.setattr(runmod, "prune", locked)
    result = invoke(["prune", "--dry-run"])
    assert result.exit_code == 5 and "another run is in progress" in result.output, result.output
    with locks.run_lock(root / "data", "run"):
        result = invoke(["run", "--shadow"])
    assert result.exit_code == 0 and "another run is in progress; skipping" in result.output, result.output


def test_run_shadow_sync_judges_and_tags_and_cuts_nothing(tmp_path, monkeypatch):
    """With BazQux and the model faked, one synchronous shadow run fetches, judges and tags
    every unread article, cuts nothing, and ends with the summary line."""
    root = _project(tmp_path, monkeypatch)
    items = [make_item(1, title="gold essay"), make_item(2, title="junk listicle")]
    cfg, client, llm = _fakes(root, monkeypatch, FakeReader(items, labels={"Must read": []}))
    result = invoke(["run", "--shadow", "--sync"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert (
        "unread 2 · new 2 · judged 2" in result.output and "scoring-only mode: nothing cut" in result.output
    )
    assert client.read == [] and {name for name, _ in client.tagged} == {"score-90", "score-0"}
    assert llm.calls == [("claude-sonnet-5-5", "teacher")]
    conn = store.connect(cfg.db_path)
    last = store.last_runs(conn, 1)[0]
    assert last["kind"] == "run" and last["ok"] and last["scored"] == 2
    assert (
        store.article(conn, items[0].id)["score"] == 90.0
        and store.article(conn, items[1].id)["score"] == 10.0
    )
    conn.close()
    assert (root / "dashboard" / "index.html").exists() and not (root / "data" / "bodh.pid").exists()


# -- doctor --------------------------------------------------------------------------


def test_doctor_exit_code_on_missing_secrets(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch, about=False)
    result = invoke(["doctor"])
    assert result.exit_code == 1 and "MISSING" in result.output and "problem(s)" in result.output
    (root / ".env").write_text(
        "BAZQUX_EMAIL=a@b.c\nBAZQUX_PASSWORD=pw\nANTHROPIC_API_KEY=sk-ant-test\n", encoding="utf-8"
    )
    example = (ROOT / "profile.example" / "about.md").read_text(encoding="utf-8")
    (root / "profile" / "about.md").write_text(example, encoding="utf-8")  # what ./install.sh leaves behind
    result = invoke(["doctor"])
    assert result.exit_code == 1 and "STILL THE EXAMPLE" in result.output, result.output
    for args in (["run", "--shadow"], ["blast", "--yes"]):  # refused before any network, exit code 2
        result = invoke(args)
        assert result.exit_code == 2 and "still the worked example" in result.output, (args, result.output)
    (root / "profile" / "about.md").write_text(ABOUT, encoding="utf-8")
    result = invoke(["doctor"])
    assert result.exit_code == 0 and result.output.rstrip().endswith("ok"), result.output
    assert "sk-ant-test" not in result.output and "pw" not in result.output.split("credentials")[1][:20]


def test_doctor_values_prints_every_setting_with_its_source(tmp_path, monkeypatch):
    """`--values` lists every setting in effect, each marked config.yaml or default."""
    root = _project(tmp_path, monkeypatch, config="timezone: UTC\nprune:\n  keep: 12\n")
    _env(root)
    result = invoke(["doctor", "--values"])
    assert result.exit_code == 0, result.output
    assert "   prune.keep = 12  (config.yaml)" in result.output
    assert "   timezone = 'UTC'  (config.yaml)" in result.output
    assert "   autocut.cutoff = 35  (default)" in result.output
    values = load_config(root).values_in_effect()
    assert {source for _, _, source in values} == {"config.yaml", "default"}
    for key, value, source in values:
        assert f"   {key} = {value!r}  ({source})" in result.output, key


def test_doctor_live_talks_to_bazqux_and_the_models_and_prints_no_secret(tmp_path, monkeypatch):
    """`--live` logs in, counts the unread, checks the folders and the must-read stream, and
    calls both models once; the spend line counts today only. The e-mail BazQux answers with
    is never printed. A folder in config.yaml that BazQux does not have is a problem, exit 1;
    with any problem before the live part, the live checks are skipped."""
    root = _project(tmp_path, monkeypatch, config="timezone: UTC\nfolders:\n  Essays: {}\n")
    items = [make_item(1, feed="Feed A"), make_item(2, feed="Feed B")]
    reader = FakeReader(
        items, folders={"Feed A": "Essays"}, labels={"Must read": []}, email="reader@example.com"
    )
    cfg, client, llm = _fakes(root, monkeypatch, reader)
    result = invoke(["doctor", "--live"])
    assert result.exit_code == 1 and "live checks skipped until the problems above are fixed" in result.output
    assert llm.calls == []
    _env(root)
    conn = store.connect(cfg.db_path)
    store.record_cost(conn, None, "claude-sonnet-5-5", "teacher", {}, 1.0)
    store.record_cost(conn, None, "claude-sonnet-5-5", "teacher", {}, 100.0)
    conn.execute("UPDATE costs SET created_at = ? WHERE usd = 100.0", (store.days_ago_iso(1),))
    conn.close()
    result = invoke(["doctor", "--live"])
    assert result.exit_code == 0 and result.output.rstrip().endswith("ok"), (result.output, result.exception)
    for line in (
        "bazqux credentials: set",
        "bazqux login: ok",
        "bazqux unread: 2",
        "bazqux feeds: 2 in folders ['Essays']",
        "must-read stream 'Must read': present",
        "   - Article 1 (Feed A)",
        "judge (claude-sonnet-5-5): ok, probe scored 55/55/55/55/55",
        "review (claude-opus-5-5): ok",
    ):
        assert line in result.output, (line, result.output)
    # the dollar from today and the probe's cents, not yesterday's hundred
    assert "spend today, this doctor run included: $1.0" in result.output and "$101" not in result.output
    assert llm.calls == [("claude-sonnet-5-5", "teacher"), ("claude-opus-5-5", "second_look")]
    (root / "config.yaml").write_text("timezone: UTC\nfolders:\n  Essays: {}\n  Nope: {}\n", encoding="utf-8")
    result = invoke(["doctor", "--live"])
    assert result.exit_code == 1 and "config folders not in BazQux: ['Nope']" in result.output
    assert "1 problem(s)" in result.output


# -- prune and undo ------------------------------------------------------------------


def test_prune_confirms_and_rejects_negative_keep(tmp_path, monkeypatch):
    from bodh.config import Config

    root = _project(tmp_path, monkeypatch)
    cfg = Config()
    cfg.root = root
    conn = store.connect(cfg.db_path)
    seed_scored(conn, cfg, [dict(n=1, score=90), dict(n=2, score=20), dict(n=3, score=30)])
    conn.close()
    assert invoke(["prune", "--keep", "-1"]).exit_code == 1
    assert invoke(["prune", "--fraction", "2"]).exit_code == 1
    assert invoke(["prune", "--keep", "1", "--below", "5"]).exit_code == 1
    client = FakeReader(unread=[])
    monkeypatch.setattr(run.Context, "client", property(lambda self: client))
    # a dry run shows and cuts nothing
    result = invoke(["prune", "--keep", "1", "--dry-run"])
    assert result.exit_code == 0 and "would cut 2 of 3" in result.output and client.read == []
    # in a terminal, a declined confirmation cuts nothing; `n` is the answer
    monkeypatch.setattr("bodh.cli._interactive", lambda: True)
    result = invoke(["prune", "--keep", "1"], input="n\n")
    assert result.exit_code == 0 and client.read == [] and "Cut these 2" in result.output
    # --yes cuts without asking
    result = invoke(["prune", "--keep", "1", "--yes"])
    assert result.exit_code == 0 and len(client.read[0]) == 2 and "undo" in result.output


def test_prune_dry_run_shows_the_boundary_and_the_ten_best_of_what_goes(tmp_path, monkeypatch):
    """The dry run names the count and the boundary, lists the ten best articles that would go
    (the boundary being chosen), writes nothing, and is refused while the safety limit stands."""
    root = _project(tmp_path, monkeypatch)
    cfg, client, _ = _fakes(root, monkeypatch)
    conn = store.connect(cfg.db_path)
    seed_scored(conn, cfg, [dict(n=n, score=10 + 5 * n) for n in range(1, 16)])  # scores 15 to 85
    conn.close()
    result = invoke(["prune", "--keep", "2", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would cut 13 of 15 scored unread articles; boundary 80" in result.output
    assert "the best of what goes (this is the boundary you are choosing):" in result.output
    for n in range(13, 3, -1):  # 75 down to 30: the ten best of the thirteen
        assert f"  {10 + 5 * n:5.1f}  Article {n}  [Feed A]" in result.output, result.output
    for n in (3, 2, 1):
        assert f"  {10 + 5 * n:5.1f}  Article {n}  [Feed A]" not in result.output
    assert client.read == [] and client.tagged == []
    conn = store.connect(cfg.db_path)
    assert store.last_runs(conn) == [] and len(store.status_ids(conn, "unread")) == 15
    store.kv_set(conn, store.SAFETY_KEY, "7")
    conn.close()
    result = invoke(["prune", "--keep", "2", "--dry-run"])
    assert result.exit_code == 1 and "the safety limit tripped in run 7" in result.output, result.output


def test_undo_restores_the_last_cut_and_says_when_there_is_none(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    cfg, client, _ = _fakes(root, monkeypatch)
    result = invoke(["undo"])
    assert result.exit_code == 0 and result.output.strip() == "nothing to undo"
    conn = store.connect(cfg.db_path)
    ids = seed_scored(conn, cfg, [dict(n=1, score=10), dict(n=2, score=20), dict(n=3, score=90)])
    run_id = store.begin_run(conn, "prune")
    store.mark_cut(conn, ids[:2], run_id, "prune")
    store.set_flags(conn, ids[:2], cut_tagged=1, read_marked=1)
    store.finish_run(conn, run_id, True, cut_prune=2)
    conn.close()
    result = invoke(["undo"])
    assert result.exit_code == 0 and f"restored 2 articles from run {run_id}" in result.output, result.output
    assert [sorted(ids_) for ids_ in client.unread_marked] == [sorted(ids[:2])]
    assert [(name, sorted(ids_)) for name, ids_ in client.untagged] == [("cut", sorted(ids[:2]))]
    conn = store.connect(cfg.db_path)
    assert store.status_ids(conn, "unread") == set(ids)
    exempt = {r[0] for r in conn.execute("SELECT article_id FROM state WHERE exempt = 1")}
    assert exempt == set(ids[:2])  # autocut leaves what an undo restored alone
    assert store.last_runs(conn, 1)[0]["kind"] == "undo"
    conn.close()
    result = invoke(["undo"])
    assert result.exit_code == 0 and "nothing to undo" in result.output


# -- clear-old -----------------------------------------------------------------------


def test_clear_old_counts_on_a_dry_run_and_cuts_only_with_yes(tmp_path, monkeypatch):
    """The dry run counts and records no run; a declined confirmation cuts nothing; --yes
    marks the old unread read, tag first, as a run of its own that `undo` reverses. A star
    is exempt."""
    items = [make_item(n) for n in (1, 2, 3, 4)]
    ids = [i.id for i in items]
    old = int((time.time() - 30 * 86400) * 1_000_000)
    fresh = int((time.time() - 86400) * 1_000_000)
    root = _project(tmp_path, monkeypatch)
    reader = DatedReader(
        items,
        timestamps={ids[0]: old, ids[1]: old, ids[2]: fresh, ids[3]: old},
        starred=[ids[3]],
        labels={"Must read": []},
    )
    cfg, client, _ = _fakes(root, monkeypatch, reader)
    result = invoke(["clear-old", "--days", "14", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "2 unread articles older than 14 days (stars and protections exempt)" in result.output
    result = invoke(["clear-old"], input="n\n")
    assert result.exit_code == 0 and "marked" not in result.output
    assert client.read == [] and client.tagged == []
    conn = store.connect(cfg.db_path)
    assert store.last_runs(conn) == []
    conn.close()
    result = invoke(["clear-old", "--yes"])
    assert result.exit_code == 0 and "marked 2 read; `bodh undo` reverses it" in result.output, result.output
    assert [sorted(x) for x in client.read] == [sorted(ids[:2])]
    assert [(name, sorted(x)) for name, x in client.tagged] == [("cut", sorted(ids[:2]))]
    conn = store.connect(cfg.db_path)
    last = store.last_runs(conn, 1)[0]
    assert last["kind"] == "clear-old" and last["ok"] and last["cut_prune"] == 2
    assert store.status_ids(conn, "cut") == set(ids[:2])
    assert store.article(conn, ids[0])["title"] == "(clear-old: not fetched)"
    conn.close()
    result = invoke(["undo"])
    assert result.exit_code == 0 and f"restored 2 articles from run {last['id']}" in result.output


# -- catch-up ------------------------------------------------------------------------


def test_catch_up_refuses_without_a_price_and_judges_everything_pending_with_yes(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    cfg, client, llm = _fakes(root, monkeypatch)
    result = invoke(["catch-up"])
    assert result.exit_code == 0 and "nothing pending within the age window" in result.output
    conn = store.connect(cfg.db_path)
    for n in (1, 2, 3):
        store.upsert_article(conn, make_item(n), None)
    conn.close()
    result = invoke(["catch-up", "--model", "made-up-model", "--yes"])
    assert result.exit_code == 1 and "no price for made-up-model in models.prices_per_mtok" in result.output
    result = invoke(["catch-up"], input="n\n")
    assert result.exit_code == 0 and "3 articles pending" in result.output, result.output
    assert llm.calls == [] and "judged" not in result.output
    result = invoke(["catch-up", "--yes"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert "about $0.00 with claude-haiku-5-5, 8 calls at once" in result.output
    assert re.search(
        r"judged 3 \(\+0 dupes\) in \d\.\d min over 1 rounds; 0 failed batches; pending 0; spend today \$0\.00",
        result.output,
    ), result.output
    assert llm.calls == [("claude-haiku-5-5", "teacher")]
    assert [name for name, _ in client.tagged] == ["score-50"] and client.read == []
    conn = store.connect(cfg.db_path)
    assert len(store.scored_ids(conn)) == 3 and store.last_runs(conn, 1)[0]["kind"] == "catch-up"
    conn.close()
    assert "nothing pending" in invoke(["catch-up"]).output


# -- status, safety, why -------------------------------------------------------------


def test_status_shows_the_schedule_the_safety_limit_and_the_last_run(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    cfg = load_config(root)
    result = invoke(["status"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert "○ idle · no scheduled run pending" in result.output
    assert "mode: scoring-only (nothing is cut)" in result.output
    schedule = (
        "schedule: run not installed · weekly not installed · report not installed · console not installed"
    )
    assert schedule in result.output, result.output
    assert "last runs:" in result.output and "SAFETY LIMIT" not in result.output
    conn = store.connect(cfg.db_path)
    run_id = store.begin_run(conn, "run")
    store.finish_run(conn, run_id, True, summary="unread 3 · new 1 · judged 1")
    store.kv_set(conn, store.SAFETY_KEY, str(run_id))
    conn.close()
    result = invoke(["status"])
    assert result.exit_code == 0
    assert (
        f"SAFETY LIMIT TRIPPED in run {run_id}: autocut cuts nothing until `bodh safety reset`"
        in result.output
    )
    assert re.search(r"ok  run +unread 3 · new 1 · judged 1", result.output), result.output


def test_safety_reports_and_resets_the_tripped_limit(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    cfg = load_config(root)
    result = invoke(["safety", "maybe"])
    assert result.exit_code == 1 and "say status or reset" in result.output
    result = invoke(["safety"])
    assert result.exit_code == 0 and "safety limit clear: autocut cuts as configured" in result.output
    assert "no article has been given up on" in result.output
    result = invoke(["safety", "reset"])
    assert result.exit_code == 0 and "the safety limit is not tripped" in result.output
    conn = store.connect(cfg.db_path)
    store.kv_set(conn, store.SAFETY_KEY, "7")
    store.kv_set(conn, store.LEGACY_SAFETY_KEY, "7")
    store.upsert_article(conn, make_item(1), None)
    conn.execute("UPDATE state SET judge_failures = ?", (store.MAX_JUDGE_FAILURES,))
    conn.close()
    result = invoke(["safety"])
    assert result.exit_code == 0 and "safety limit TRIPPED in run 7: autocut cuts nothing" in result.output
    assert "1 unread articles given up on" in result.output
    result = invoke(["safety", "reset"])
    assert result.exit_code == 0, result.output
    assert "safety limit reset (it tripped in run 7); autocut cuts again from the next run" in result.output
    assert "1 articles the judge had given up on are pending again" in result.output
    conn = store.connect(cfg.db_path)
    assert (
        store.kv_get(conn, store.SAFETY_KEY) is None and store.kv_get(conn, store.LEGACY_SAFETY_KEY) is None
    )
    assert store.given_up(conn) == 0
    conn.close()
    assert "safety limit clear" in invoke(["safety"]).output


def test_why_explains_a_judged_article_and_refuses_an_unknown_one(tmp_path, monkeypatch):
    """`why` takes the signed number or the long id, never a URL; an id it does not know is
    one line and exit 1."""
    root = _project(tmp_path, monkeypatch)
    cfg = load_config(root)
    conn = store.connect(cfg.db_path)
    ids = seed_scored(conn, cfg, [dict(n=1, score=80, concept="reefs", title="Reef recovery")])
    store.upsert_article(conn, make_item(2, title="Not judged yet"), None)
    store.set_verdict(conn, ids[0], "great", store.GRADES)
    conn.close()
    for args in (["why", "99"], ["why", "https://example.com/FeedA/1"]):
        result = invoke(args)
        assert result.exit_code == 1 and "error: not found" in result.output, (args, result.output)
    for ref in ("1", ids[0]):
        result = invoke(["why", ref])
        assert result.exit_code == 0, result.output
        for line in (
            "Reef recovery\nhttps://example.com/FeedA/1\nfeed: Feed A · folder: None",
            "score 80.0 (base 80.0)",
            "quality 80 · learning 80 · fit 80 · breadth 80 · relevance 80 · confidence high · kind reporting"
            " · flags none · judged by test (teacher.v2)",
            "concept: reefs\nlesson: l",
            "state: unread",
            "your record: great (",
        ):
            assert line in result.output, (line, result.output)
    result = invoke(["why", "2"])
    assert result.exit_code == 0 and "not scored yet" in result.output and "your record" not in result.output


# -- mark and steer ------------------------------------------------------------------


def test_mark_records_verdicts_steers_and_rescues(tmp_path, monkeypatch):
    """`bodh mark ID great|…|rescue` is the tag from the terminal: grades and levels are recorded
    in their exclusive groups, a steer writes the dated line, and a rescue brings a cut article
    back unread in BazQux and here and keeps it from every cut."""
    root = _project(tmp_path, monkeypatch)
    cfg = load_config(root)
    conn = store.connect(cfg.db_path)
    ids = seed_scored(conn, cfg, [dict(n=1, score=80, concept="reefs"), dict(n=2, score=20)])
    store.mark_cut(conn, ids[1:], 7, "autocut")
    store.set_flags(conn, ids[1:], cut_tagged=1, read_marked=1)
    conn.close()
    client = FakeReader(unread=ids[:1])
    monkeypatch.setattr(run.Context, "client", property(lambda self: client))
    assert invoke(["mark", "1", "great"]).exit_code == 0
    assert invoke(["mark", "1", "heavy"]).exit_code == 0
    result = invoke(["mark", "1", "more"])
    assert result.exit_code == 0 and "steer: reefs" in result.output
    result = invoke(["mark", "2", "rescue"])
    assert result.exit_code == 0 and "rescued" in result.output
    assert invoke(["mark", "1", "maybe"]).exit_code == 1
    assert "not an article id" in invoke(["mark", "not-an-id", "great"]).output
    conn = store.connect(cfg.db_path)
    assert store.verdicts(conn, store.GRADES) == {ids[0]: "great"} and store.verdicts(conn, store.LEVELS) == {
        ids[0]: "heavy"
    }
    assert store.verdicts(conn, store.STEERS) == {ids[0]: "more"}
    assert "wants more on reefs" in (root / "profile" / "learned.md").read_text(encoding="utf-8")
    row = store.article(conn, ids[1])
    assert row["status"] == "unread" and row["rescued_at"] and client.unread_marked == [[ids[1]]]
    assert ("cut", [ids[1]]) in client.untagged
    assert {k for k, _ in store.feedback_for(conn, ids[1])} == {"rescue", "wrong"}
    conn.close()


def test_mark_takes_every_kind_and_refuses_an_unknown_article(tmp_path, monkeypatch):
    """good, meh and bad replace one another as the grade; basic and heavy as the level; less
    steers by the article's concept; a rescue of an article that was never cut is the positive
    alone. An id no run has fetched is refused."""
    root = _project(tmp_path, monkeypatch)
    cfg, client, _ = _fakes(root, monkeypatch)
    conn = store.connect(cfg.db_path)
    ids = seed_scored(conn, cfg, [dict(n=1, score=50, concept="crypto", title="Coin news")])
    conn.close()
    result = invoke(["mark", "99", "great"])
    assert (
        result.exit_code == 1 and "unknown article; it must have been fetched by a run first" in result.output
    )
    for kind in ("bad", "good", "meh", "heavy", "basic"):
        result = invoke(["mark", "1", kind])
        assert result.exit_code == 0 and f"recorded {kind} for: Coin news" in result.output, result.output
    result = invoke(["mark", "1", "less"])
    assert (
        result.exit_code == 0
        and 'recorded less for: Coin news (steer: crypto (e.g. "Coin news"))' in result.output
    )
    result = invoke(["mark", "1", "rescue"])
    assert (
        result.exit_code == 0 and "recorded as a strong positive (it was not cut): Coin news" in result.output
    )
    assert client.unread_marked == [] and client.untagged == []
    conn = store.connect(cfg.db_path)
    assert store.verdicts(conn, store.GRADES) == {ids[0]: "meh"}
    assert store.verdicts(conn, store.LEVELS) == {ids[0]: "basic"}
    assert store.verdicts(conn, store.STEERS) == {ids[0]: "less"}
    assert {k for k, _ in store.feedback_for(conn, ids[0])} == {"meh", "basic", "less", "wrong"}
    assert store.article(conn, ids[0])["rescued_at"] is None
    conn.close()
    assert "wants less of crypto" in (root / "profile" / "learned.md").read_text(encoding="utf-8")


def test_steer_writes_a_dated_line_and_refuses_another_word(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    result = invoke(["steer", "more", "coral reefs"])
    assert result.exit_code == 0, result.output
    assert re.search(r"^- \d{4}-\d{2}-\d{2}: the student wants more on coral reefs$", result.output, re.M)
    result = invoke(["steer", "less", "crypto"])
    assert result.exit_code == 0 and "the student wants less of crypto" in result.output
    learned = (root / "profile" / "learned.md").read_text(encoding="utf-8")
    assert learned.startswith(LEARNED.rstrip()) and "\n## Steers\n" in learned
    assert learned.index("wants more on coral reefs") < learned.index("wants less of crypto")
    result = invoke(["steer", "sideways", "x"])
    assert result.exit_code == 1 and "say more or less" in result.output
    assert (root / "profile" / "learned.md").read_text(encoding="utf-8") == learned
    conn = store.connect(load_config(root).db_path)
    versions = store.learned_versions(conn, 5)
    assert [v["reason"] for v in versions] == ["steer: meh crypto", "steer: want coral reefs", "initial"]
    conn.close()


# -- profile ----------------------------------------------------------------------------


def test_profile_shows_the_student_and_reverts_one_version(tmp_path, monkeypatch):
    """`profile` prints about.md, a rule, then learned.md. `revert` with fewer than two versions
    says so and changes nothing; with two it writes the previous one back as a new version."""
    root = _project(tmp_path, monkeypatch)
    shown = f"{ABOUT.strip()}\n\n---\n\n{LEARNED.strip()}\n"
    for args in (["profile"], ["profile", "show"]):
        result = invoke(args)
        assert result.exit_code == 0 and result.output == shown, (args, result.output)
    result = invoke(["profile", "revert"])
    assert result.exit_code == 0 and result.output.strip() == "nothing to revert to"
    result = invoke(["profile", "delete"])
    assert result.exit_code == 1 and "say show or revert" in result.output
    assert (root / "profile" / "learned.md").read_text(encoding="utf-8") == LEARNED
    assert invoke(["steer", "more", "reefs"]).exit_code == 0  # version 1 is the initial text, 2 the steer
    assert "wants more on reefs" in invoke(["profile"]).output
    result = invoke(["profile", "revert"])
    assert result.exit_code == 0 and "reverted; now version 3" in result.output, result.output
    assert (root / "profile" / "learned.md").read_text(encoding="utf-8") == LEARNED
    assert "wants more on reefs" not in invoke(["profile", "show"]).output


# -- schedule, autocut, stop ------------------------------------------------------------


def test_schedule_and_autocut_refuse_a_wrong_word_and_edit_the_right_thing(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    for args in (["schedule"], ["schedule", "status"]):
        result = invoke(args)
        assert result.exit_code == 0, (args, result.output)
        assert "run     not installed  bodh.run" in result.output, result.output
        assert "console not installed  bodh.console" in result.output
    for action in ("pause", "resume", "kick"):
        result = invoke(["schedule", action])
        assert result.exit_code == 0, (action, result.output)
        assert "bodh.run is not installed; run ./install-launchd.sh first" in result.output, result.output
    result = invoke(["schedule", "pause"])
    assert "the console is paused too; `bodh schedule resume` brings everything back" in result.output
    result = invoke(["schedule", "resume", "--job", "weekly"])
    assert (
        result.exit_code == 0
        and "bodh.weekly is not installed" in result.output
        and "bodh.run" not in result.output
    )
    result = invoke(["schedule", "dance"])
    assert result.exit_code == 1 and "action must be status, pause, resume or kick" in result.output
    result = invoke(["schedule", "pause", "--job", "nope"])
    assert result.exit_code == 1 and "--job must be run, weekly, report, console or all" in result.output
    with locks.run_lock(root / "data", "run"):
        result = invoke(["schedule", "kick"])
        assert (
            result.exit_code == 0
            and "not kicked: run (pid" in result.output
            and "wait for it" in result.output
        )
    result = invoke(["autocut", "maybe"])
    assert result.exit_code == 1 and "say on or off" in result.output
    result = invoke(["autocut", "on"])
    assert (
        result.exit_code == 0
        and "autocut.enabled is now true: autocut cuts below the cut-off" in result.output
    )
    assert yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) == {
        "timezone": "UTC",
        "autocut": {"enabled": True},
    }
    assert (
        "autocut: on, cut-off 35" in invoke(["doctor"]).output
        and "mode: autocut" in invoke(["status"]).output
    )
    result = invoke(["autocut", "off"])
    assert (
        result.exit_code == 0
        and "autocut.enabled is now false: scoring-only mode, nothing is cut" in result.output
    )
    assert yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) == {
        "timezone": "UTC",
        "autocut": {"enabled": False},
    }
    assert "autocut: off (scoring-only mode)" in invoke(["doctor"]).output


def test_stop_with_nothing_running_and_stop_all(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    result = invoke(["stop"])
    assert result.exit_code == 0 and result.output.strip() == "nothing is running"
    result = invoke(["stop", "--all"])
    assert result.exit_code == 0, result.output
    for line in (
        "nothing is running",
        "bodh.run is not installed; run ./install-launchd.sh first",
        "everything is stopped; `bodh schedule resume` starts the jobs again",
    ):
        assert line in result.output, (line, result.output)
    assert not (root / "data" / "console.json").exists()


# -- backup and rebuild ------------------------------------------------------------------


def test_backup_writes_a_dated_copy_and_keeps_the_newest(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    result = invoke(["backup", "--keep", "0"])
    assert result.exit_code == 2 and "--keep" in result.output, result.output
    result = invoke(["backup", "--keep", "2"])
    assert result.exit_code == 0 and "backup written: " in result.output and "(1 kept)" in result.output
    folder = root / "data" / "backups"
    kept = recovery.backups(load_config(root))
    assert [p.parent for p in kept] == [folder] and re.fullmatch(r"bodh-\d{4}-\d{2}-\d{2}\.db", kept[0].name)
    today = kept[0].name
    copy = sqlite3.connect(folder / today)
    assert store.kv_get(copy, store.SCHEMA_VERSION_KEY) == str(store.SCHEMA_VERSION)  # a real database
    copy.close()
    for n in (1, 2, 3):  # older backups, by the date in the name
        (folder / f"bodh-2000-01-0{n}.db").write_bytes(b"old")
    result = invoke(["backup", "--keep", "2"])
    assert result.exit_code == 0 and "(2 kept)" in result.output, result.output
    assert {p.name for p in folder.glob("bodh-*.db")} == {today, "bodh-2000-01-03.db"}
    assert f"backups: 2, newest {today}" in invoke(["doctor"]).output


def test_rebuild_dry_run_counts_and_writes_nothing(tmp_path, monkeypatch):
    """The dry run reads the tags back from BazQux and only counts; a declined confirmation
    writes nothing either; --yes writes the scores and the cuts as a run of its own."""
    root = _project(tmp_path, monkeypatch)
    items = [make_item(1), make_item(2)]
    reader = FakeReader(
        items,
        unread=[items[0].id],
        labels={"score-90": [items[0].id], "cut": [items[1].id], "Must read": []},
    )
    cfg, client, _ = _fakes(root, monkeypatch, reader)
    result = invoke(["rebuild", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert (
        "1 band-tagged, 1 cut-tagged and 0 starred in BazQux; 2 unknown here (0 fetched, 0 vanished); "
        "would write 1 scores, 0 cuts, 0 stars; the judge reads 1 unread of them again on the next runs"
        in result.output
    )
    result = invoke(["rebuild"], input="n\n")
    assert result.exit_code == 0 and "wrote" not in result.output and "would write" not in result.output
    conn = store.connect(cfg.db_path)
    assert store.known_ids(conn) == set() and store.last_runs(conn) == []
    conn.close()
    result = invoke(["rebuild", "--yes"])
    assert result.exit_code == 0, result.output
    assert "2 unknown here (2 fetched, 0 vanished); wrote 1 scores, 1 cuts, 0 stars" in result.output
    conn = store.connect(cfg.db_path)
    assert store.last_runs(conn, 1)[0]["kind"] == "rebuild"
    first, second = store.article(conn, items[0].id), store.article(conn, items[1].id)
    assert (
        first["score"] == 90.0 and first["model"] == recovery.REBUILT_MODEL and first["confidence"] == "low"
    )
    assert second["status"] == "cut" and second["cut_mode"] == "rebuild"
    conn.close()
    assert client.read == [] and client.tagged == []  # nothing BazQux shows is changed


# -- weekly, report, dashboard, progress ---------------------------------------------------


def test_weekly_keeps_the_profile_on_an_empty_week_and_when_the_gate_rejects(tmp_path, monkeypatch):
    """With fewer than five positives nothing is rewritten and no model is called; with six
    starred articles the judge rates as junk the proposal is rejected by the eval gate, the
    reason is printed, the note and the run row are written, and the exit code is 0."""
    root = _project(tmp_path, monkeypatch)
    cfg, client, llm = _fakes(root, monkeypatch)
    result = invoke(["weekly"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert "only 0 positives this week; learned profile kept as is" in result.output
    assert "weekly note: " in result.output and (root / "dashboard" / "weekly.md").exists()
    assert llm.calls == []
    conn = store.connect(cfg.db_path)
    ids = seed_scored(conn, cfg, [dict(n=n, score=90, title=f"junk {n}") for n in range(1, 7)])
    for aid in ids:
        store.add_feedback(conn, aid, "star")
    conn.close()
    result = invoke(["weekly"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert "rewrite rejected by the eval gate: 6 starred below the cut-off: junk 6; junk 5" in result.output
    assert llm.calls == [("claude-opus-5-5", "learner_rewrite"), ("claude-sonnet-5-5", "teacher")]
    assert (root / "profile" / "learned.md").read_text(encoding="utf-8") == LEARNED
    conn = store.connect(cfg.db_path)
    last = store.last_runs(conn, 1)[0]
    assert last["kind"] == "weekly" and last["ok"] and last["summary"].startswith("weekly: rewrite rejected")
    conn.close()


def test_weekly_reports_a_model_failure_and_records_it(tmp_path, monkeypatch):
    """A rejected key is one line and the model's exit code, 4; the run row carries the error
    and the profile is untouched."""
    root = _project(tmp_path, monkeypatch)
    cfg, client, llm = _fakes(root, monkeypatch)
    conn = store.connect(cfg.db_path)
    ids = seed_scored(conn, cfg, [dict(n=n, score=90) for n in range(1, 7)])
    for aid in ids:
        store.add_feedback(conn, aid, "star")
    conn.close()
    llm.api.fail_with = api_error(anthropic.AuthenticationError, "bad key", 401)
    result = invoke(["weekly"])
    assert result.exit_code == 4, result.output
    assert "error: weekly failed: model call rejected" in result.output, result.output
    assert (root / "profile" / "learned.md").read_text(encoding="utf-8") == LEARNED
    conn = store.connect(cfg.db_path)
    last = store.last_runs(conn, 1)[0]
    assert last["kind"] == "weekly" and not last["ok"] and "ModelConfigError" in last["error"]
    conn.close()


def test_report_and_dashboard_write_their_files_on_an_empty_database(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    result = invoke(["report", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("# Feed report · ") and "· last 7 days" in result.output
    assert "0 proposals. Act on them in BazQux" in result.output
    assert (root / "dashboard" / "feed-report.md").read_text(encoding="utf-8") + "\n" == result.output
    result = invoke(["dashboard"])
    assert result.exit_code == 0, result.output
    folder = root / "dashboard"
    assert result.output.strip() == str((folder / "index.html").resolve())
    for name in ("index.html", "data.json", "progress.json"):
        assert (folder / name).is_file(), name
    assert "<html" in (folder / "index.html").read_text(encoding="utf-8")


def test_progress_prints_the_pile_once(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    conn = store.connect(load_config(root).db_path)
    store.upsert_article(conn, make_item(1), None)
    conn.close()
    result = invoke(["progress"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("bodh progress · ") and "○ idle" in result.output
    assert re.search(r"Unread known to bodh +1\n", result.output) and "pending (≤14 days)" in result.output


# -- every command: -v, the old names ------------------------------------------------------


def test_label_bad_id_and_verbose_everywhere(tmp_path, monkeypatch):
    """Every command takes -v and runs through with it, the hidden old names
    included, with the secrets in .env and the debug log on the terminal (so `invoke` checks
    every line of it for a secret); `console` is checked through its help, since it would start
    the server."""
    root = _project(tmp_path, monkeypatch)
    _env(root)
    cfg, client, llm = _fakes(root, monkeypatch)
    conn = store.connect(cfg.db_path)
    seed_scored(conn, cfg, [dict(n=1, score=70)])
    conn.close()
    result = invoke(["label", "not-an-id", "star"])
    assert result.exit_code == 1 and "not an article id" in result.output
    for args in (
        ["version", "-v"],
        ["doctor", "-v"],
        ["run", "--shadow", "-v"],
        ["score", "-v"],
        ["progress", "-v"],
        ["catch-up", "-v"],
        ["blast", "-v"],
        ["prune", "--dry-run", "-v"],
        ["undo", "-v"],
        ["backup", "-v"],
        ["rebuild", "--dry-run", "-v"],
        ["clear-old", "--dry-run", "-v"],
        ["bankruptcy", "--dry-run", "-v"],
        ["status", "-v"],
        ["stop", "-v"],
        ["halt", "-v"],
        ["schedule", "status", "-v"],
        ["safety", "-v"],
        ["autocut", "on", "-v"],
        ["enforce", "off", "-v"],
        ["why", "1", "-v"],
        ["mark", "1", "great", "-v"],
        ["label", "1", "star", "-v"],
        ["steer", "more", "x", "-v"],
        ["want", "x", "-v"],
        ["meh", "x", "-v"],
        ["profile", "show", "-v"],
        ["weekly", "-v"],
        ["report", "-v"],
        ["dashboard", "-v"],
    ):
        result = invoke(args)
        assert "No such option" not in result.output, (args, result.output)
        assert result.exit_code == 0, (args, result.output, result.exception)
    for name in ("console", "panel"):
        assert "--verbose" in invoke([name, "--help"]).output, name


def test_renamed_commands_print_the_new_name_and_work(tmp_path, monkeypatch):
    """The command names before October 2026 run for one release, after one line naming the
    new command; the new names and `stop --all` work."""
    root = _project(tmp_path, monkeypatch)
    _fakes(root, monkeypatch)
    for old, new, expect in (
        (["want", "reefs"], "steer more", "wants more on reefs"),
        (["meh", "crypto"], "steer less", "wants less of crypto"),
        (["enforce", "off"], "autocut", "scoring-only"),
        (["bankruptcy", "--dry-run"], "clear-old", None),
        (["blast"], "catch-up", None),
        (["score"], "run --shadow", None),
        (["halt"], "stop --all", "everything is stopped"),
    ):
        result = invoke(old)
        assert f"is now `bodh {new}" in result.output, (old, result.output)
        if expect:
            assert expect in result.output, (old, result.output)
    assert "wants more on reefs" in (root / "profile" / "learned.md").read_text(encoding="utf-8")
    result = invoke(["steer", "sideways", "x"])
    assert result.exit_code == 1 and "say more or less" in result.output
    result = invoke(["autocut", "on"])
    assert result.exit_code == 0 and "autocut cuts" in result.output
    result = invoke(["stop", "--all"])
    assert result.exit_code == 0 and "everything is stopped" in result.output
    help_text = invoke(["--help"]).output
    for hidden in ("blast", "bankruptcy", "halt", "enforce", "panel", "label", "want", "meh", "score"):
        assert f"  {hidden} " not in help_text and f"│ {hidden} " not in help_text, hidden
    for shown in ("catch-up", "clear-old", "console", "mark", "steer", "autocut"):
        assert shown in help_text, shown
