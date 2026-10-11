"""The command line, driven through typer's runner against a temporary project."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from viveka.cli import app

runner = CliRunner()


def test_refuses_without_config(tmp_path, monkeypatch):
    """A folder with no config.yaml gets a one-line refusal, and nothing is created in it."""
    monkeypatch.delenv("VIVEKA_HOME", raising=False)
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
    (tmp_path / "profile" / "learner.md").write_text("## What the student has been learning\nx\n", encoding="utf-8")
    monkeypatch.setenv("VIVEKA_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_weekly_want_meh_and_revert_take_the_lock(tmp_path, monkeypatch):
    """They write learner.md, which a run may be writing too (a steer tag, the gate)."""
    from viveka import ops

    root = _project(tmp_path, monkeypatch)
    with ops.run_lock(root / "data", "run"):
        for args in (["weekly"], ["want", "reefs"], ["meh", "crypto"], ["profile", "revert"]):
            result = runner.invoke(app, args)
            assert result.exit_code == 5 and "run is in progress" in result.output, (args, result.output)
    monkeypatch.setattr("viveka.learner.git_commit", lambda *a, **k: True)
    result = runner.invoke(app, ["want", "reefs"])
    assert result.exit_code == 0 and "wants more on reefs" in result.output
    assert "wants more on reefs" in (root / "profile" / "learner.md").read_text(encoding="utf-8")


def test_doctor_exit_code_on_missing_secrets(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    for name in ("BAZQUX_EMAIL", "BAZQUX_PASSWORD", "ANTHROPIC_API_KEY", "HEALTHCHECK_URL"):
        monkeypatch.delenv(name, raising=False)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1 and "MISSING" in result.output and "problem(s)" in result.output
    (root / ".env").write_text("BAZQUX_EMAIL=a@b.c\nBAZQUX_PASSWORD=pw\nANTHROPIC_API_KEY=sk-ant-test\n", encoding="utf-8")
    example = (Path(__file__).resolve().parents[1] / "profile.example" / "seed.md").read_text(encoding="utf-8")
    (root / "profile" / "seed.md").write_text(example, encoding="utf-8")  # what ./install.sh leaves behind
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1 and "STILL THE EXAMPLE" in result.output, result.output
    (root / "profile" / "seed.md").write_text("# Seed\nStudent: test.\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0 and result.output.rstrip().endswith("ok"), result.output
    assert "sk-ant-test" not in result.output and "pw" not in result.output.split("credentials")[1][:20]


def test_prune_confirms_and_rejects_negative_keep(tmp_path, monkeypatch):
    from conftest import FakeClient, seed_scored

    from viveka import pipeline, store
    from viveka.config import Config

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
    monkeypatch.setattr(pipeline.Context, "client", property(lambda self: client))
    # a dry run shows and cuts nothing
    result = runner.invoke(app, ["prune", "--keep", "1", "--dry-run"])
    assert result.exit_code == 0 and "would cut 2 of 3" in result.output and client.read == []
    # in a terminal, a declined confirmation cuts nothing; `n` is the answer
    monkeypatch.setattr("viveka.cli._interactive", lambda: True)
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
