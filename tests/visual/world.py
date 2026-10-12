"""The worlds the visual tests and `scripts/screenshots.py` render: the fixture of
`scripts/make_fixture_db.py` at a few thousand articles, dated from one fixed instant so a
screenshot is the same every day, with one of the page's states applied."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from bodh import store  # noqa: E402
from bodh.config import Config, load_config  # noqa: E402

FIXED = datetime(2026, 10, 11, 15, 56, 0, tzinfo=timezone.utc)  # 16:56 in Lisbon
STATES = ("idle", "running", "attention", "empty")
ARTICLES = 1200
CONFIG = """timezone: "Europe/Lisbon"
autocut:
  enabled: false
folders:
  Essays: {}
  News: {cutoff: 50}
  Always read: {protected: true}
"""


class Frozen(datetime):
    """`datetime.now()` at the fixed instant, for the modules that date the page."""

    @classmethod
    def now(cls, tz: object = None) -> Frozen:  # type: ignore[override]
        at = FIXED.astimezone(tz) if tz is not None else FIXED  # type: ignore[arg-type]
        return cls(at.year, at.month, at.day, at.hour, at.minute, at.second, at.microsecond, at.tzinfo)


def make_world(
    root: Path, state: str = "idle", articles: int = ARTICLES
) -> tuple[Config, sqlite3.Connection]:
    """A project folder under `root` with the fixture database in the given state."""
    from make_fixture_db import build

    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "profile").mkdir(exist_ok=True)
    (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (root / "profile" / "about.md").write_text(
        "# About the student\nA reader of essays and science.\n", encoding="utf-8"
    )
    (root / "profile" / "learned.md").write_text(
        "## What the student has been learning\n- how coral reefs recover after bleaching\n- compilers that explain their errors\n",
        encoding="utf-8",
    )
    db = root / "data" / "bodh.db"
    if state == "empty":
        if db.exists():
            db.unlink()
        store.connect(db).close()
    else:
        build(db, articles, now=FIXED)
    cfg = load_config(root)
    conn = store.connect(cfg.db_path)
    if state == "running":
        store.kv_set(conn, store.PHASE_KEY, "judging 240 of 1,200")
    if state == "attention":
        store.kv_set(conn, store.SAFETY_KEY, "2880")
    return cfg, conn


def freeze(monkeypatch: object) -> None:
    """Pin the clock of the modules that date the page (a pytest monkeypatch or anything with
    `setattr`)."""
    from bodh.web import document

    monkeypatch.setattr(document, "datetime", Frozen)  # type: ignore[attr-defined]
