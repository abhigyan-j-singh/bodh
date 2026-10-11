"""Failure drills: the things a year of daily use will bring, each as one test. BazQux down
for a whole run; the model out mid-run; a crash between the tag and the read mark; a laptop
asleep for two days; the budget reached mid-run; two runs at once; a corrupt progress.json; a
typo in config.yaml; a model id the provider does not know."""

from __future__ import annotations

import json
import threading
import time

import anthropic
import httpx
import pytest
from fakes import FakeBazQuxServer, FakeLLM, FakeReader, api_error, make_item

import bodh.readers.bazqux as bq
from bodh import dashboard, locks, progress, run, store
from bodh.config import Secrets
from bodh.errors import ModelError, ReaderError
from bodh.readers.bazqux import BazQuxError


class Pings:
    """What the heartbeat sent: (suffix, body) per post."""

    def __init__(self, monkeypatch) -> None:
        self.sent: list[tuple[str, str]] = []

        def post(url: str, content: bytes = b"", timeout: float = 0.0) -> None:
            self.sent.append((url.replace("https://hc-ping.com/uuid", ""), content.decode()))

        monkeypatch.setattr("bodh.heartbeat.httpx.post", post)

    @property
    def suffixes(self) -> list[str]:
        return [s for s, _ in self.sent]


def _items(titles: list[str]) -> list:
    return [
        make_item(n, feed="Feed A" if n % 2 else "Feed B", title=t, crawled_msec=1_800_000_000_000 + n)
        for n, t in enumerate(titles, start=1)
    ]


def _ctx(cfg, conn, client, heartbeat: bool = False):
    ctx = run.Context(cfg, Secrets(healthcheck_url="https://hc-ping.com/uuid" if heartbeat else ""), conn)
    ctx._client = client
    ctx._llm = FakeLLM(cfg, conn)
    return ctx


def test_bazqux_down_for_the_whole_run(cfg, conn, monkeypatch):
    """Every request fails: the run records a failed run, pings /fail, and leaves the
    database as it was: nothing fetched, nothing judged, nothing spent."""
    monkeypatch.setattr(bq, "MIN_REQUEST_INTERVAL", 0.0)
    monkeypatch.setattr(bq.time, "sleep", lambda _s: None)
    pings = Pings(monkeypatch)
    cfg.models.batch = False
    server = FakeBazQuxServer(_items(["gold essay", "junk listicle"]))
    server.raise_next["/accounts/ClientLogin"] = [httpx.ConnectError("down")] * bq.MAX_RETRIES
    ctx = _ctx(cfg, conn, server.client(), heartbeat=True)
    with pytest.raises(ReaderError, match="after 4 attempts"):
        run.run(ctx, enforce=True)
    last = store.last_runs(conn, 1)[0]
    assert last["kind"] == "run" and not last["ok"] and "BazQuxError" in last["error"]
    assert pings.suffixes == ["/start", "/fail"] and "down" in pings.sent[-1][1]
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 0 and ctx.llm.calls == []
    assert store.kv_get(conn, store.PHASE_KEY) == ""  # the live views do not show a phase for a run that died
    # BazQux comes back: the next run is a normal one
    res = run.run(ctx, enforce=True)
    assert res.ok and res.new == 2 and res.scored == 2 and pings.suffixes[-1] == ""


def test_model_outage_mid_run_keeps_what_was_judged(cfg, conn):
    """The API answers 5xx from the third call on: the two judged articles are judged and
    tagged, the rest stay pending for the next run, the run is ok and says so."""
    cfg.models.batch = False
    cfg.judge.articles_per_call = 1
    client = FakeReader(_items(["gold essay", "junk listicle", "plain a", "plain b", "plain c", "plain d"]))
    ctx = _ctx(cfg, conn, client)
    for n in range(3, 20):
        ctx.llm.api.fail_calls[n] = api_error(anthropic.InternalServerError, "overloaded", 500)
    res = run.run(ctx, enforce=False, workers=1)
    assert res.ok and res.scored == 2 and res.pending == 4
    assert "4 batch(es) failed and stay pending" in res.summary()
    assert len(store.scored_ids(conn)) == 2 and {name for name, _ in client.tagged} == {
        "score-50"
    }  # newest first
    assert conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 2  # only what was answered is paid for
    ctx.llm.api.fail_calls.clear()
    res = run.run(ctx, enforce=False, workers=2)
    assert res.scored == 4 and res.pending == 0


def test_crash_between_the_tag_and_the_read_mark_is_finished_next_run(cfg, conn):
    """The cut tag is written, then BazQux fails before the read mark: the run fails, the
    article stays tagged and unread, and the next run marks it read without re-judging."""
    cfg.models.batch = False
    cfg.autocut.new_ground_share = 0.0
    client = FakeReader(_items(["gold essay", "junk listicle"]))
    calls = {"n": 0}
    real = client.mark_read

    def flaky(ids):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BazQuxError("edit-tag returned 'ERR'")
        return real(ids)

    client.mark_read = flaky
    ctx = _ctx(cfg, conn, client)
    with pytest.raises(ReaderError):
        run.run(ctx, enforce=True)
    junk = store.article(conn, client.unread[1] if len(client.unread) > 1 else list(client.items)[1])
    assert junk["status"] == "cut" and ("cut", [junk["id"]]) in client.tagged and client.read == []
    assert store.cut_pending(conn) == ([], [junk["id"]])  # tagged, not yet marked read
    res = run.run(ctx, enforce=True)
    assert res.ok and res.scored == 0 and res.cut_autocut == 0 and client.read == [[junk["id"]]]
    assert store.cut_pending(conn) == ([], []) and store.article(conn, junk["id"])["status"] == "cut"


def test_laptop_asleep_for_two_days(cfg, conn):
    """A batch still in progress after 26 hours is given up on and its articles resubmitted,
    except the ones older than limits.max_age_days, which are no longer judged at all."""
    cfg.judge.articles_per_call = 1
    cfg.limits.max_age_days = 14
    client = FakeReader(_items(["gold essay", "junk listicle", "plain news"]))
    ctx = _ctx(cfg, conn, client)
    res = run.run(ctx, enforce=False)
    assert res.submitted == 3 and len(ctx.llm.batches) == 1
    # two days pass: the batch sits in the API as it was, and one article turns 20 days old
    conn.execute("UPDATE batches SET created_at = ?", (store.days_ago_iso(2),))
    old_ms = int((time.time() - 20 * 86400) * 1000)
    conn.execute("UPDATE articles SET crawled_msec = ? WHERE id = ?", (old_ms, client.unread[2]))
    res = run.run(ctx, enforce=False)
    assert res.ok and "given up on" in res.summary() and "still in_progress after 26h" in res.summary()
    assert [b["status"] for b in store.recent_batches(conn)][-1] == "lost"
    assert res.submitted == 2 and res.pending == 0  # the old one is outside the window, not pending
    assert store.article(conn, client.unread[2])["score"] is None
    assert progress.snapshot(conn, cfg)["too_old"] == 1


def test_budget_reached_mid_run_stops_the_judging_and_cuts_nothing(cfg, conn):
    """Scoring stops at the budget, the judged articles are tagged, nothing is cut on a
    half-judged run, and the summary says so; tomorrow's runs cut as usual."""
    cfg.models.batch = False
    cfg.judge.articles_per_call = 1
    cfg.autocut.new_ground_share = 0.0
    cfg.models.daily_budget_usd = 0.005  # two calls at $0.003 each land, the third is refused
    client = FakeReader(_items(["junk a", "junk b", "junk c", "junk d"]))
    ctx = _ctx(cfg, conn, client)
    res = run.run(ctx, enforce=True, workers=1)
    assert res.ok and res.scored == 2 and res.pending == 2 and res.cut_autocut == 0
    assert "daily budget reached" in res.summary() and "nothing cut this run" in res.summary()
    assert client.read == [] and {name for name, _ in client.tagged} == {"score-0"}
    # the next run the same day: still over budget, still nothing cut
    res = run.run(ctx, enforce=True, workers=1)
    assert res.scored == 0 and res.cut_autocut == 0 and "nothing cut this run" in res.summary()
    # midnight: the budget is back, the rest is judged and the cut happens
    conn.execute("UPDATE costs SET created_at = '2020-01-01T00:00:00+00:00'")
    res = run.run(ctx, enforce=True, workers=1)
    assert res.scored == 2 and res.cut_autocut == 4 and len(client.read[0]) == 4


def test_two_runs_started_at_once(cfg, conn, tmp_path):
    """The second refuses at once (LockedError, exit 5 through the CLI), the first finishes."""
    data = cfg.root / cfg.data_dir
    started = threading.Event()
    release = threading.Event()
    refused: list[float] = []

    def first() -> None:
        with locks.run_lock(data, "run"):
            started.set()
            release.wait(5)

    t = threading.Thread(target=first)
    t.start()
    try:
        assert started.wait(5)
        t0 = time.monotonic()
        with pytest.raises(locks.AlreadyRunning), locks.run_lock(data, "run"):  # the second: at once
            pass
        refused.append(time.monotonic() - t0)
    finally:
        release.set()
        t.join(5)
    assert refused[0] < 2.0
    assert locks.AlreadyRunning().exit_code == 5
    with locks.run_lock(data, "run"):  # the lock is free again once the first is done
        pass


def test_corrupt_progress_json_still_renders(cfg, conn):
    """A truncated progress.json (a crash mid-write on an old version, a full disk): the
    dashboard renders, the console's status answers, and the next write replaces the file."""
    out = cfg.root / cfg.dashboard_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "progress.json").write_text('{"unread": 12, "folders": [{"fol', encoding="utf-8")
    dashboard.render(cfg, conn)
    assert (out / "index.html").exists() and "progress.json" in (out / "index.html").read_text(
        encoding="utf-8"
    )
    snap = progress.snapshot(conn, cfg)
    assert snap["unread"] == 0 and "RUN IN PROGRESS" not in progress.render_text(snap)
    path = progress.write_live(cfg, conn, min_interval=0.0)
    assert path is not None and json.loads(path.read_text(encoding="utf-8"))["unread"] == 0


def test_a_typo_in_config_yaml_is_exit_2_with_the_key_named(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from bodh.cli import app

    (tmp_path / "config.yaml").write_text("timezone: UTC\nautocutt:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("BODH_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 2 and "autocutt" in result.output, result.output
    (tmp_path / "config.yaml").write_text("timezone: UTC\nautocut:\n  cutoff: 101\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 2 and "cutoff" in result.output, result.output


def test_a_model_id_the_provider_does_not_know_is_exit_4_with_the_name(cfg, conn, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from bodh.cli import app

    cfg.models.batch = False
    client = FakeReader(_items(["plain news"]))
    ctx = _ctx(cfg, conn, client)
    ctx.llm.api.fail_with = api_error(anthropic.NotFoundError, "model: claude-nope-9 not found", 404)
    with pytest.raises(ModelError) as exc:
        run.run(ctx, enforce=False)
    assert exc.value.exit_code == 4 and "claude-nope-9" in str(exc.value)
    assert not store.last_runs(conn, 1)[0]["ok"] and "claude-nope-9" in store.last_runs(conn, 1)[0]["error"]
    conn.close()
    (cfg.root / "config.yaml").write_text("timezone: UTC\nmodels:\n  batch: false\n", encoding="utf-8")
    monkeypatch.setenv("BODH_HOME", str(cfg.root))
    llm = ctx.llm
    monkeypatch.setattr(run.Context, "client", property(lambda self: client))
    monkeypatch.setattr(run.Context, "llm", property(lambda self: llm))
    result = CliRunner().invoke(app, ["run", "--sync"])
    assert result.exit_code == 4 and "claude-nope-9" in result.output and "sk-ant" not in result.output
