from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bodh import store
from bodh.config import Config
from bodh.ids import short_to_long


@dataclass
class FakeItem:
    id: str
    short_id: int
    feed_stream_id: str
    feed_title: str
    feed_html_url: str = ""
    url: str = ""
    title: str = ""
    author: str = ""
    published: int | None = None
    crawled_msec: int | None = None
    html: str = ""
    categories: list[str] = field(default_factory=list)


def make_item(n: int, feed: str = "Feed A", title: str | None = None, url: str | None = None,
              html: str = "<p>Some text about things.</p>", crawled_msec: int | None = None) -> FakeItem:
    return FakeItem(
        id=short_to_long(n), short_id=n, feed_stream_id=f"feed/{feed}", feed_title=feed,
        url=url or f"https://example.com/{feed.replace(' ', '')}/{n}", title=title or f"Article {n}",
        html=html, crawled_msec=crawled_msec,
    )


@pytest.fixture(autouse=True)
def _no_real_launchd(monkeypatch):
    """The suite runs on the owner's Mac too: never touch the real launchd jobs or processes."""
    from bodh import control

    monkeypatch.setattr(control, "_plist_path", lambda label: (None, ""))
    monkeypatch.setattr(control, "launchctl_available", lambda: False)
    monkeypatch.setattr(control, "stray_processes", lambda root=None: [])


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config()
    c.root = tmp_path
    (tmp_path / "data").mkdir()
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "seed.md").write_text("# Seed\nStudent: test.\n", encoding="utf-8")
    (tmp_path / "profile" / "learner.md").write_text(
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
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def conn(cfg: Config) -> sqlite3.Connection:
    return store.connect(cfg.db_path)


def seed_scored(conn: sqlite3.Connection, cfg: Config, specs: Iterable[dict]) -> list[str]:
    """specs: dicts with n, score, and optional folder, feed, concept, confidence, starred, protected, quality."""
    ids = []
    for s in specs:
        item = make_item(s["n"], feed=s.get("feed", "Feed A"), title=s.get("title"), url=s.get("url"),
                         crawled_msec=s.get("crawled_msec", 1_700_000_000_000 + s["n"]))
        store.upsert_article(conn, item, s.get("folder"))
        verdict = {"quality": s.get("quality", int(s["score"])), "learning": int(s["score"]), "fit": int(s["score"]),
                   "breadth": int(s["score"]), "relevance": int(s["score"]), "lesson": "l", "concept": s.get("concept", "c"),
                   "kind": "reporting", "flags": [], "dupe_of": s.get("dupe_of"), "confidence": s.get("confidence", "high")}
        store.record_score(conn, item.id, verdict, s["score"], s["score"], cfg.tags.band_for(s["score"]),
                           cfg.judge.prompt_version, "test")
        flags = {}
        if s.get("starred"):
            flags["starred"] = 1
        if s.get("protected"):
            flags["protected"] = 1
        if flags:
            store.set_flags(conn, [item.id], **flags)
        ids.append(item.id)
    return ids


class FakeClient:
    """Records every write; serves configured reads."""

    def __init__(self, unread: list[str] | None = None, starred: list[str] | None = None,
                 labels: dict[str, list[str]] | None = None) -> None:
        self.unread = list(unread or [])
        self.starred = list(starred or [])
        self.labels = {k: list(v) for k, v in (labels or {}).items()}
        self.tagged: list[tuple[str, list[str]]] = []
        self.untagged: list[tuple[str, list[str]]] = []
        self.read: list[list[str]] = []
        self.unread_marked: list[list[str]] = []

    def unread_ids(self):
        from bodh.bazqux import ItemRef

        return [ItemRef(id=i, timestamp_usec=None) for i in self.unread]

    def starred_ids(self):
        return list(self.starred)

    def label_ids(self, name: str, unread_only: bool = False, strict: bool = False):
        value = self.labels.get(name, [])
        if isinstance(value, Exception):  # a label BazQux cannot serve right now
            if strict:
                raise value
            return []
        return list(value)

    def tag(self, ids, name):
        self.tagged.append((name, list(ids)))
        return len(list(ids))

    def untag(self, ids, name):
        self.untagged.append((name, list(ids)))
        return len(list(ids))

    def mark_read(self, ids):
        ids = list(ids)
        self.read.append(ids)
        self.unread = [i for i in self.unread if i not in set(ids)]
        return len(ids)

    def mark_unread(self, ids):
        ids = list(ids)
        self.unread_marked.append(ids)
        self.unread += [i for i in ids if i not in self.unread]
        return len(ids)
