from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest
from fakes import FakeClient, FakeItem, FakeLLM, FakeReader, make_item  # noqa: F401  (re-exported for the tests)

from bodh import store
from bodh.config import Config


@pytest.fixture(autouse=True)
def _no_real_launchd(monkeypatch):
    """The suite runs on the owner's Mac too: never touch the real launchd jobs or processes."""
    from bodh import jobs

    monkeypatch.setattr(jobs, "_plist_path", lambda label: (None, ""))
    monkeypatch.setattr(jobs, "launchctl_available", lambda: False)
    monkeypatch.setattr(jobs, "stray_processes", lambda root=None: [])


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config()
    c.root = tmp_path
    (tmp_path / "data").mkdir()
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "about.md").write_text("# About the student\nStudent: test.\n", encoding="utf-8")
    (tmp_path / "profile" / "learned.md").write_text(
        "## What the student has been learning\nnothing\n\n## Expansion topics for next week\n- nuclear power: why\n- medieval history, because\n",
        encoding="utf-8",
    )
    return c


@pytest.fixture(autouse=True)
def _close_connections(monkeypatch):
    """Every connection the code under test opens is closed at teardown: the suite runs with
    ResourceWarning as an error, so an unclosed database fails the test that leaked it."""
    opened: list[sqlite3.Connection] = []
    real = store.connect

    def tracking(path):
        c = real(path)
        opened.append(c)
        return c

    monkeypatch.setattr(store, "connect", tracking)
    yield
    for c in opened:
        with contextlib.suppress(Exception):
            c.close()


@pytest.fixture
def conn(cfg: Config) -> sqlite3.Connection:
    return store.connect(cfg.db_path)


def seed_scored(conn: sqlite3.Connection, cfg: Config, specs: Iterable[dict]) -> list[str]:
    """specs: dicts with n, score, and optional folder, feed, concept, confidence, starred, protected, quality."""
    ids = []
    for s in specs:
        item = make_item(
            s["n"],
            feed=s.get("feed", "Feed A"),
            title=s.get("title"),
            url=s.get("url"),
            crawled_msec=s.get("crawled_msec", 1_700_000_000_000 + s["n"]),
        )
        store.upsert_article(conn, item, s.get("folder"))
        verdict = {
            "quality": s.get("quality", int(s["score"])),
            "learning": int(s["score"]),
            "fit": int(s["score"]),
            "breadth": int(s["score"]),
            "relevance": int(s["score"]),
            "lesson": "l",
            "concept": s.get("concept", "c"),
            "kind": "reporting",
            "flags": [],
            "dupe_of": s.get("dupe_of"),
            "confidence": s.get("confidence", "high"),
        }
        store.record_score(
            conn,
            item.id,
            verdict,
            s["score"],
            s["score"],
            cfg.tags.band_for(s["score"]),
            cfg.judge.prompt_version,
            "test",
        )
        flags = {}
        if s.get("starred"):
            flags["starred"] = 1
        if s.get("protected"):
            flags["protected"] = 1
        if flags:
            store.set_flags(conn, [item.id], **flags)
        ids.append(item.id)
    return ids
