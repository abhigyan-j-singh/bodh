"""The command line, driven through typer's runner against a temporary project."""

from __future__ import annotations

from typer.testing import CliRunner

from triage.cli import app

runner = CliRunner()


def test_refuses_without_config(tmp_path, monkeypatch):
    """A folder with no config.yaml gets a one-line refusal, and nothing is created in it."""
    monkeypatch.delenv("TRIAGE_HOME", raising=False)
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
    monkeypatch.setenv("TRIAGE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_weekly_want_meh_and_revert_take_the_lock(tmp_path, monkeypatch):
    """They write learner.md, which a run may be writing too (a steer tag, the gate)."""
    from triage import ops

    root = _project(tmp_path, monkeypatch)
    with ops.run_lock(root / "data", "run"):
        for args in (["weekly"], ["want", "reefs"], ["meh", "crypto"], ["profile", "revert"]):
            result = runner.invoke(app, args)
            assert result.exit_code == 5 and "run is in progress" in result.output, (args, result.output)
    monkeypatch.setattr("triage.learner.git_commit", lambda *a, **k: True)
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
    (root / "profile" / "seed.md").write_text("# Seed\nStudent: test.\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0 and result.output.rstrip().endswith("ok"), result.output
    assert "sk-ant-test" not in result.output and "pw" not in result.output.split("credentials")[1][:20]
