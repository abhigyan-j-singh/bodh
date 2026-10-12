"""The reader contract: what every reader implementation must do with ids, contents, tags and
read marks. Parametrised over the implementations: the real BazQux client over the fake
server, and the in-memory FakeReader the pipeline tests use (so the fake cannot drift from
the real thing). A new reader is accepted only with this suite green."""

from __future__ import annotations

import pytest
from fakes import FakeBazQuxServer, FakeReader, make_item

import bodh.readers.bazqux as bq
from bodh.errors import ReaderError
from bodh.ids import short_to_long
from bodh.readers.bazqux import BazQuxError


def _items():
    return [
        make_item(
            n, feed="Feed A" if n % 2 else "Feed B", title=f"Article {n}", crawled_msec=1_800_000_000_000 + n
        )
        for n in range(1, 7)
    ]


class World:
    """One scenario, and a way to look at the reader's state and to break a label."""

    def __init__(self, kind: str, page_size: int = 1000) -> None:
        self.kind = kind
        self.items = _items()
        ids = [i.id for i in self.items]
        unread = [i for i in ids if i != short_to_long(4)]
        labels = {"Must read": [short_to_long(1)], "score-90": [short_to_long(3)]}
        folders = {"Feed A": "Essays"}
        if kind == "bazqux":
            self.server = FakeBazQuxServer(
                self.items,
                unread=unread,
                starred=[short_to_long(2)],
                labels=labels,
                folders=folders,
                page_size=page_size,
            )
            self.reader = self.server.client()
        else:
            self.reader = FakeReader(
                self.items, unread=unread, starred=[short_to_long(2)], labels=labels, folders=folders
            )

    def unread(self) -> set[str]:
        return set(self.server.unread) if self.kind == "bazqux" else set(self.reader.unread)

    def label(self, name: str) -> set[str]:
        if self.kind == "bazqux":
            return set(self.server.labels.get(name, set()))
        value = self.reader.labels.get(name, [])
        return set(value) if not isinstance(value, Exception) else set()

    def break_label(self, name: str) -> None:
        if self.kind == "bazqux":
            self.server.fail_next["/reader/api/0/stream/items/ids"] = [500] * bq.MAX_RETRIES
        else:
            self.reader.labels[name] = BazQuxError("stream unavailable")


@pytest.fixture(params=["bazqux", "fake"])
def world(request, monkeypatch) -> World:
    monkeypatch.setattr(bq, "MIN_REQUEST_INTERVAL", 0.0)
    monkeypatch.setattr(bq.time, "sleep", lambda _s: None)
    return World(request.param, page_size=2)


def test_ids_by_stream(world: World):
    r = world.reader
    assert [x.id for x in r.unread_ids()] == [short_to_long(n) for n in (1, 2, 3, 5, 6)]
    assert r.starred_ids() == [short_to_long(2)]
    assert r.label_ids("Must read") == [short_to_long(1)]
    assert r.label_ids("Essays", unread_only=True) == [
        short_to_long(n) for n in (1, 3, 5)
    ]  # a folder is a stream
    assert r.label_ids("never used") == []
    assert len(r.item_refs(bq.READING_LIST)) == 6 and len(r.item_refs(bq.READING_LIST, exclude=bq.READ)) == 5


def test_contents_and_vanished(world: World):
    items, vanished = world.reader.contents([short_to_long(1), short_to_long(2), short_to_long(99)])
    assert [i.id for i in items] == [short_to_long(1), short_to_long(2)] and vanished == [short_to_long(99)]
    one = items[0]
    assert one.title == "Article 1" and one.feed_title == "Feed A" and one.url.endswith("/FeedA/1")
    assert one.feed_stream_id.startswith("feed/") and one.html and one.crawled_msec == 1_800_000_000_001


def test_tag_and_untag_are_idempotent(world: World):
    r = world.reader
    ids = [short_to_long(5), short_to_long(6)]
    assert r.tag(ids, "score-50") == 2 and r.tag(ids, "score-50") == 2
    assert world.label("score-50") == set(ids) and r.label_ids("score-50") == ids
    assert r.untag(ids, "score-50") == 2 and r.untag(ids, "score-50") == 2
    assert world.label("score-50") == set() and r.label_ids("score-50") == []
    assert world.label("score-90") == {short_to_long(3)}  # other tags untouched


def test_mark_read_and_unread(world: World):
    r = world.reader
    assert r.mark_read([short_to_long(1), short_to_long(2)]) == 2
    assert world.unread() == {short_to_long(n) for n in (3, 5, 6)}
    assert [x.id for x in r.unread_ids()] == [short_to_long(n) for n in (3, 5, 6)]
    assert r.mark_unread([short_to_long(1), short_to_long(4)]) == 2 and r.mark_unread([short_to_long(1)]) == 1
    assert world.unread() == {short_to_long(n) for n in (1, 3, 4, 5, 6)}
    assert r.mark_read([]) == 0


def test_label_listing_empty_against_error(world: World):
    world.break_label("Must read")
    assert world.reader.label_ids("Must read") == []  # unavailable reads as empty
    world.break_label("Must read")
    with pytest.raises(ReaderError):  # unless the caller's decision depends on the answer
        world.reader.label_ids("Must read", strict=True)


def test_subscriptions_and_folders(world: World):
    subs = {s.title: s for s in world.reader.subscriptions()}
    assert set(subs) == {"Feed A", "Feed B"}
    assert subs["Feed A"].folders == ["Essays"] and subs["Feed B"].folders == []
    assert all(s.stream_id.startswith("feed/") for s in subs.values())
    assert world.reader.user_info()["userEmail"].endswith("@example.com")
