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
