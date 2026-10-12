"""`bodh migrate-home`: a project moves whole, nothing is rewritten, a second run moves nothing."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from bodh import migrate, store
from bodh.cli import app
from bodh.errors import ConfigError


def _project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "config.yaml").write_text("autocut:\n  enabled: false\n", encoding="utf-8")
    (root / ".env").write_text("ANTHROPIC_API_KEY=x\n", encoding="utf-8")
    (root / "data").mkdir()
    conn = store.connect(root / "data" / "bodh.db")
    store.kv_set(conn, "k", "v")
    conn.commit()
    conn.close()
    (root / "data" / "backups").mkdir()
    (root / "profile").mkdir()
    (root / "profile" / "about.md").write_text("# About the student\n", encoding="utf-8")
    (root / "profile" / ".git").mkdir()
    (root / "dashboard").mkdir()
    (root / "dashboard" / "index.html").write_text("<p>x</p>", encoding="utf-8")


def test_plan_and_move(tmp_path):
    src, dst = tmp_path / "bodh", tmp_path / "Library" / "Application Support" / "bodh"
    _project(src)
    plan = migrate.plan(src, dst)
    assert plan.moves == ["config.yaml", ".env", "data", "profile", "dashboard"] and plan.missing == [
        "prompts"
    ]
    dry = migrate.migrate_home(src, dst, dry_run=True)
    assert dry.moves == plan.moves and (src / "config.yaml").exists() and not dst.exists()
    done = migrate.migrate_home(src, dst)
    assert done.moves == plan.moves
    assert (
        (dst / "config.yaml").exists()
        and (dst / "profile" / ".git").is_dir()
        and (dst / "data" / "backups").is_dir()
    )
    assert not (src / "config.yaml").exists() and not (src / "data").exists()
    conn = store.connect(dst / "data" / "bodh.db")
    assert store.kv_get(conn, "k") == "v"
    conn.close()
    lines = migrate.describe(done)
    assert lines[0].startswith("moved 5 item(s)") and any("BODH_HOME" in line for line in lines)
    # a second run: everything is already there, nothing moves
    again = migrate.migrate_home(src, dst)
    assert again.moves == [] and again.nothing_to_do and sorted(again.kept) == sorted(plan.moves)
    # something left behind in FROM moves on a later run; what TO already has stays
    (src / "prompts").mkdir()
    (src / "prompts" / "teacher.v9.md").write_text("x", encoding="utf-8")
    (src / "config.yaml").write_text("stale\n", encoding="utf-8")
    later = migrate.migrate_home(src, dst)
    assert later.moves == ["prompts"] and "config.yaml" in later.kept
    assert (dst / "config.yaml").read_text(encoding="utf-8").startswith("autocut")


def test_refusals(tmp_path):
    src = tmp_path / "bodh"
    _project(src)
    with pytest.raises(ConfigError, match="same folder"):
        migrate.plan(src, src)
    with pytest.raises(ConfigError, match="inside the other"):
        migrate.plan(src, src / "inner")
    with pytest.raises(ConfigError, match="not a bodh project"):
        migrate.plan(tmp_path / "empty", tmp_path / "elsewhere")
    from bodh.locks import run_lock

    with run_lock(src / "data", "run"), pytest.raises(ConfigError, match="run is in progress"):
        migrate.migrate_home(src, tmp_path / "new")


def test_command(tmp_path):
    src, dst = tmp_path / "bodh", tmp_path / "new"
    _project(src)
    runner = CliRunner()
    res = runner.invoke(app, ["migrate-home", str(src), str(dst), "--dry-run"])
    assert res.exit_code == 0 and "would move 5 item(s)" in res.output and not dst.exists()
    res = runner.invoke(app, ["migrate-home", str(src), str(dst)])
    assert res.exit_code == 0 and "moved 5 item(s)" in res.output and (dst / "config.yaml").exists()
    res = runner.invoke(app, ["migrate-home", str(src), str(src)])
    assert res.exit_code == 2 and "same folder" in res.output
