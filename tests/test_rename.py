"""The name changed from triage to bodh: an install from before keeps working for one release."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bodh import __version__, jobs
from bodh.cli import app
from bodh.config import Config, find_root

ROOT = Path(__file__).resolve().parents[1]


def test_db_path_keeps_an_existing_old_database(tmp_path):
    cfg = Config()
    cfg.root = tmp_path
    (tmp_path / "data").mkdir()
    assert cfg.db_path.name == "bodh.db"
    (tmp_path / "data" / "triage.db").write_bytes(b"")
    assert cfg.db_path.name == "triage.db"  # the owner's data stays where it is
    (tmp_path / "data" / "bodh.db").write_bytes(b"")
    assert cfg.db_path.name == "bodh.db"  # once a new one exists, it wins


def test_old_home_variable_still_works_with_a_warning(tmp_path, monkeypatch, caplog):
    import logging

    (tmp_path / "config.yaml").write_text("timezone: UTC\n", encoding="utf-8")
    monkeypatch.delenv("BODH_HOME", raising=False)
    monkeypatch.setenv("TRIAGE_HOME", str(tmp_path))
    with caplog.at_level(logging.WARNING):
        assert find_root() == tmp_path.resolve()
    assert "TRIAGE_HOME is the old name" in caplog.text
    monkeypatch.setenv("BODH_HOME", str(tmp_path))
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert find_root() == tmp_path.resolve()
    assert caplog.text == ""


def test_log_file_is_the_new_folder_on_a_mac_and_data_logs_elsewhere(tmp_path, monkeypatch):
    """The old ~/Library/Logs/triage is not kept: the installers create the new folder before
    any run, so keeping the old one would have split the logs across two folders."""
    from bodh import logs

    home = tmp_path / "home"
    (home / "Library" / "Logs" / "triage").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert logs.log_file(tmp_path / "data") == home / "Library" / "Logs" / "bodh" / "bodh.log"
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    assert logs.log_file(tmp_path / "data") == tmp_path / "data" / "logs" / "bodh.log"


def test_the_owners_files_are_not_tracked():
    """config.yaml is the owner's, like .env and profile/; the one-person prompt is gone."""
    tracked = subprocess.run(
        ["git", "ls-files", "config.yaml", "src/bodh/prompts", ".gitignore"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    ).stdout.split()
    if ".gitignore" not in tracked:
        pytest.skip("not a git checkout")
    assert "config.yaml" not in tracked and "src/bodh/prompts/teacher.v1.md" not in tracked, tracked
    assert "/config.yaml" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_old_command_name_runs_with_a_note():
    res = subprocess.run(
        [
            sys.executable,
            "-c",
            "from bodh.cli import triage_alias; import sys; sys.argv=['triage','version']; triage_alias()",
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0 and f"bodh {__version__}" in res.stdout and "now `bodh`" in res.stderr
    assert CliRunner().invoke(app, ["version"]).output.strip() == f"bodh {__version__}"


def test_installer_migrates_the_old_labels_and_names_match_the_code():
    installer = (ROOT / "install-launchd.sh").read_text(encoding="utf-8")
    for old in jobs.OLD_JOBS:
        assert old in installer, old
    loop = next(line for line in installer.splitlines() if line.startswith("for job in "))
    assert [w.strip(";") for w in loop.split()[3:-1]] == list(jobs.JOBS.values())
    for label in jobs.JOBS.values():
        assert (ROOT / "launchd" / f"{label}.plist").exists()
    assert "com.antriksh" not in "".join(
        p.read_text(encoding="utf-8") for p in (ROOT / "launchd").glob("*.plist")
    )


def test_stray_pattern_covers_both_command_names(tmp_path):
    import re

    pattern = re.compile(jobs.stray_pattern(tmp_path))
    assert pattern.search(f"{tmp_path}/.venv/bin/bodh run") and pattern.search(
        f"{tmp_path}/.venv/bin/triage run"
    )


def test_version_is_single_sourced():
    import importlib.metadata

    try:
        assert importlib.metadata.version("bodh") == __version__
    except importlib.metadata.PackageNotFoundError:
        pass  # a PYTHONPATH=src run without the package installed: the file check below still holds
    assert "version" not in (ROOT / "pyproject.toml").read_text(encoding="utf-8").split("[project.optional")[
        0
    ].replace('dynamic = ["version"]', "")


def test_old_module_names_import_with_a_deprecation_warning():
    """`bodh.teacher`, `bodh.learner`, `bodh.cut`, `bodh.pipeline`, `bodh.control`, `bodh.panel`,
    `bodh.bazqux` and `bodh.ops` re-export their successors for one release."""
    import importlib
    import warnings

    pairs = {
        "teacher": ("judge", "Judge"),
        "learner": ("profiler", "weekly_rewrite"),
        "cut": ("cutting", "select_autocut"),
        "pipeline": ("run", "Context"),
        "control": ("jobs", "stop_all"),
        "panel": ("console", "Console"),
        "bazqux": ("readers.bazqux", "BazQuxClient"),
        "ops": ("locks", "run_lock"),
    }
    for old, (new, name) in pairs.items():
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            sys.modules.pop(f"bodh.{old}", None)
            shim = importlib.import_module(f"bodh.{old}")
        assert any(
            issubclass(w.category, DeprecationWarning) and f"bodh.{new.split('.')[0]}" in str(w.message)
            for w in caught
        ), old
        assert getattr(shim, name) is getattr(importlib.import_module(f"bodh.{new}"), name), (old, name)
