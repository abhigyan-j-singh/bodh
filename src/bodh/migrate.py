"""`bodh migrate-home FROM TO`: move a project from one folder to another (the Mac app's first
launch moves `~/bodh` into Application Support this way). What moves: config.yaml, .env,
data/, profile/ (its own git repository goes with it), dashboard/, prompts/ and the backups.
Nothing is rewritten: the paths in config.yaml are relative, so they still hold. Idempotent:
what is already in TO stays, what is missing from FROM is skipped, and a second run moves
nothing and says so."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

ITEMS = ("config.yaml", ".env", "data", "profile", "dashboard", "prompts")


@dataclass
class Plan:
    source: Path
    target: Path
    moves: list[str] = field(default_factory=list)  # names that will move
    kept: list[str] = field(default_factory=list)  # names already in TO (left as they are)
    missing: list[str] = field(default_factory=list)  # names not in FROM

    @property
    def nothing_to_do(self) -> bool:
        return not self.moves


def plan(source: Path, target: Path) -> Plan:
    """What `migrate_home` would do, checked: FROM must be a project (or already empty of one
    when TO holds it), and the two must differ."""
    source, target = source.expanduser().resolve(), target.expanduser().resolve()
    if source == target:
        raise ConfigError("FROM and TO are the same folder")
    if target in source.parents or source in target.parents:
        raise ConfigError("one folder is inside the other; choose two separate folders")
    if not (source / "config.yaml").exists() and not (target / "config.yaml").exists():
        raise ConfigError(f"{source} holds no config.yaml: not a bodh project")
    out = Plan(source, target)
    for name in ITEMS:
        if (target / name).exists():
            out.kept.append(name)
        elif (source / name).exists():
            out.moves.append(name)
        else:
            out.missing.append(name)
    return out


def migrate_home(source: Path, target: Path, dry_run: bool = False) -> Plan:
    """Move the project; returns the plan it followed. A locked database (a run in progress)
    refuses: the lock file lives in data/, and `bodh stop` ends the run first."""
    from .locks import run_in_progress

    out = plan(source, target)
    if "data" in out.moves and run_in_progress(out.source / "data"):
        raise ConfigError("a run is in progress in FROM; `bodh stop` it first")
    if dry_run or out.nothing_to_do:
        return out
    out.target.mkdir(parents=True, exist_ok=True)
    for name in out.moves:
        shutil.move(str(out.source / name), str(out.target / name))
    return out


def describe(out: Plan, dry_run: bool = False) -> list[str]:
    verb = "would move" if dry_run else "moved"
    lines = [f"{verb} {len(out.moves)} item(s) from {out.source} to {out.target}"]
    lines += [f"  {name}: {verb}" for name in out.moves]
    lines += [f"  {name}: already in {out.target.name}, left as it is" for name in out.kept]
    lines += [f"  {name}: not in {out.source.name}, nothing to move" for name in out.missing]
    if not dry_run and out.moves:
        lines.append(f"set BODH_HOME={out.target} (the app does this for you) and run `bodh doctor` there")
    return lines
