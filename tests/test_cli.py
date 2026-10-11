"""The command line, driven through typer's runner against a temporary project."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from bodh.cli import app

runner = CliRunner()


def test_refuses_without_config(tmp_path, monkeypatch):
    """A folder with no config.yaml gets a one-line refusal, and nothing is created in it."""
    monkeypatch.delenv("BODH_HOME", raising=False)
    monkeypatch.delenv("TRIAGE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # a real ~/bodh on this machine must not answer
    monkeypatch.chdir(tmp_path)
    for args in (["status"], ["run", "--shadow"], ["prune", "--dry-run"], ["doctor"], ["enforce", "off"], ["stop"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 2, (args, result.output)
        assert "no config.yaml" in result.output
    assert list(tmp_path.iterdir()) == []
    assert runner.invoke(app, ["version"]).exit_code == 0


def _project(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("timezone: UTC\n", encoding="utf-8")
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "learned.md").write_text("## What the student has been learning\nx\n", encoding="utf-8")
    monkeypatch.setenv("BODH_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_weekly_want_meh_and_revert_take_the_lock(tmp_path, monkeypatch):
    """They write learned.md, which a run may be writing too (a steer tag, the gate)."""
    from bodh import locks

    root = _project(tmp_path, monkeypatch)
    with locks.run_lock(root / "data", "run"):
        for args in (["weekly"], ["want", "reefs"], ["meh", "crypto"], ["profile", "revert"]):
            result = runner.invoke(app, args)
            assert result.exit_code == 5 and "run is in progress" in result.output, (args, result.output)
    monkeypatch.setattr("bodh.profiler.git_commit", lambda *a, **k: True)
    result = runner.invoke(app, ["want", "reefs"])
    assert result.exit_code == 0 and "wants more on reefs" in result.output
    assert "wants more on reefs" in (root / "profile" / "learned.md").read_text(encoding="utf-8")


def test_doctor_exit_code_on_missing_secrets(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    for name in ("BAZQUX_EMAIL", "BAZQUX_PASSWORD", "ANTHROPIC_API_KEY", "HEALTHCHECK_URL"):
        monkeypatch.delenv(name, raising=False)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1 and "MISSING" in result.output and "problem(s)" in result.output
    (root / ".env").write_text("BAZQUX_EMAIL=a@b.c\nBAZQUX_PASSWORD=pw\nANTHROPIC_API_KEY=sk-ant-test\n", encoding="utf-8")
    example = (Path(__file__).resolve().parents[1] / "profile.example" / "about.md").read_text(encoding="utf-8")
    (root / "profile" / "about.md").write_text(example, encoding="utf-8")  # what ./install.sh leaves behind
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1 and "STILL THE EXAMPLE" in result.output, result.output
    for args in (["run", "--shadow"], ["blast", "--yes"]):   # refused before any network, exit code 2
        result = runner.invoke(app, args)
        assert result.exit_code == 2 and "still the worked example" in result.output, (args, result.output)
    (root / "profile" / "about.md").write_text("# About the student\nStudent: test.\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0 and result.output.rstrip().endswith("ok"), result.output
    assert "sk-ant-test" not in result.output and "pw" not in result.output.split("credentials")[1][:20]


def test_prune_confirms_and_rejects_negative_keep(tmp_path, monkeypatch):
    from conftest import FakeClient, seed_scored

    from bodh import run, store
    from bodh.config import Config

    root = _project(tmp_path, monkeypatch)
    cfg = Config()
    cfg.root = root
    conn = store.connect(cfg.db_path)
    seed_scored(conn, cfg, [dict(n=1, score=90), dict(n=2, score=20), dict(n=3, score=30)])
    conn.close()
    assert runner.invoke(app, ["prune", "--keep", "-1"]).exit_code == 1
    assert runner.invoke(app, ["prune", "--fraction", "2"]).exit_code == 1
    assert runner.invoke(app, ["prune", "--keep", "1", "--below", "5"]).exit_code == 1
    client = FakeClient(unread=[])
    monkeypatch.setattr(run.Context, "client", property(lambda self: client))
    # a dry run shows and cuts nothing
    result = runner.invoke(app, ["prune", "--keep", "1", "--dry-run"])
    assert result.exit_code == 0 and "would cut 2 of 3" in result.output and client.read == []
    # in a terminal, a declined confirmation cuts nothing; `n` is the answer
    monkeypatch.setattr("bodh.cli._interactive", lambda: True)
    result = runner.invoke(app, ["prune", "--keep", "1"], input="n\n")
    assert result.exit_code == 0 and client.read == [] and "Cut these 2" in result.output
    # --yes cuts without asking
    result = runner.invoke(app, ["prune", "--keep", "1", "--yes"])
    assert result.exit_code == 0 and len(client.read[0]) == 2 and "undo" in result.output


def test_label_bad_id_and_verbose_everywhere(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    result = runner.invoke(app, ["label", "not-an-id", "star"])
    assert result.exit_code == 1 and "not an article id" in result.output
    for args in (["status", "-v"], ["why", "1", "-v"], ["safety", "-v"], ["schedule", "status", "-v"],
                 ["profile", "show", "-v"], ["report", "-v"], ["dashboard", "-v"], ["stop", "-v"]):
        result = runner.invoke(app, args)
        assert "No such option" not in result.output, (args, result.output)


def test_mark_records_verdicts_steers_and_rescues(tmp_path, monkeypatch):
    """`bodh mark ID great|…|rescue` is the tag from the terminal: grades and levels are recorded
    in their exclusive groups, a steer writes the dated line, and a rescue brings a cut article
    back unread in BazQux and here and keeps it from every cut."""
    from conftest import FakeClient, seed_scored

    from bodh import run, store
    from bodh.config import load_config

    root = _project(tmp_path, monkeypatch)
    monkeypatch.setattr("bodh.profiler.git_commit", lambda *a, **k: True)
    cfg = load_config(root)
    conn = store.connect(cfg.db_path)
    ids = seed_scored(conn, cfg, [dict(n=1, score=80, concept="reefs"), dict(n=2, score=20)])
    store.mark_cut(conn, ids[1:], 7, "autocut")
    store.set_flags(conn, ids[1:], cut_tagged=1, read_marked=1)
    conn.close()
    client = FakeClient(unread=ids[:1])
    monkeypatch.setattr(run.Context, "client", property(lambda self: client))
    assert runner.invoke(app, ["mark", "1", "great"]).exit_code == 0
    assert runner.invoke(app, ["mark", "1", "heavy"]).exit_code == 0
    result = runner.invoke(app, ["mark", "1", "more"])
    assert result.exit_code == 0 and "steer: reefs" in result.output
    result = runner.invoke(app, ["mark", "2", "rescue"])
    assert result.exit_code == 0 and "rescued" in result.output
    assert runner.invoke(app, ["mark", "1", "maybe"]).exit_code == 1
    assert "not an article id" in runner.invoke(app, ["mark", "not-an-id", "great"]).output
    conn = store.connect(cfg.db_path)
    assert store.verdicts(conn, store.GRADES) == {ids[0]: "great"} and store.verdicts(conn, store.LEVELS) == {ids[0]: "heavy"}
    assert store.verdicts(conn, store.STEERS) == {ids[0]: "more"}
    assert "wants more on reefs" in (root / "profile" / "learned.md").read_text(encoding="utf-8")
    row = store.article(conn, ids[1])
    assert row["status"] == "unread" and row["rescued_at"] and client.unread_marked == [[ids[1]]]
    assert ("cut", [ids[1]]) in client.untagged
    assert {k for k, _ in store.feedback_for(conn, ids[1])} == {"rescue", "wrong"}
    conn.close()


def test_renamed_commands_print_the_new_name_and_work(tmp_path, monkeypatch):
    """The command names before October 2026 run for one release, after one line naming the
    new command; the new names and `stop --all` work."""
    root = _project(tmp_path, monkeypatch)
    monkeypatch.setattr("bodh.profiler.git_commit", lambda *a, **k: True)
    for old, new, expect in (
        (["want", "reefs"], "steer more", "wants more on reefs"),
        (["meh", "crypto"], "steer less", "wants less of crypto"),
        (["enforce", "off"], "autocut", "scoring-only"),
        (["bankruptcy", "--dry-run"], "clear-old", None),
        (["blast"], "catch-up", None),
        (["score"], "run --shadow", None),
        (["halt"], "stop --all", "everything is stopped"),
    ):
        result = runner.invoke(app, old)
        assert f"is now `bodh {new}" in result.output, (old, result.output)
        if expect:
            assert expect in result.output, (old, result.output)
    assert "wants more on reefs" in (root / "profile" / "learned.md").read_text(encoding="utf-8")
    result = runner.invoke(app, ["steer", "sideways", "x"])
    assert result.exit_code == 1 and "say more or less" in result.output
    result = runner.invoke(app, ["autocut", "on"])
    assert result.exit_code == 0 and "autocut cuts" in result.output
    result = runner.invoke(app, ["stop", "--all"])
    assert result.exit_code == 0 and "everything is stopped" in result.output
    help_text = runner.invoke(app, ["--help"]).output
    for hidden in ("blast", "bankruptcy", "halt", "enforce", "panel", "label", "want", "meh", "score"):
        assert f"  {hidden} " not in help_text and f"│ {hidden} " not in help_text, hidden
    for shown in ("catch-up", "clear-old", "console", "mark", "steer", "autocut"):
        assert shown in help_text, shown
