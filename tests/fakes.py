"""The fakes every test runs against; nothing here touches the network.

- ``FakeReader``: the BazQux client's interface in memory (ids, contents, tags, read marks),
  recording every write. The run tests use it; the contract suite checks it against the real
  client over the fake server.
- ``FakeBazQuxServer``: BazQux's HTTP API behind ``httpx.MockTransport``: login, the write
  token, ``stream/items/ids`` with pagination, ``stream/items/contents``, ``edit-tag``, and the
  failure modes seen live (a stale auth token, a bad login, a 500 once). The real
  ``BazQuxClient`` runs against it.
- ``FakeAnthropic``: the Anthropic SDK's surface the code uses (``messages.create``, the
  batches, ``with_options``), scripted: verdicts by title, a failure on the nth call,
  structured outputs or the 1-hour cache rejected once, batches that end ``expired`` or
  ``canceled`` or vanish, results for the wrong ids, prose instead of JSON.
- ``FakeLLM``: the real ``LLM`` over a ``FakeAnthropic``, so cost accounting, the budget, the
  fallbacks and the batch lifecycle run as in production; a few knobs from the older fakes
  (``batches``, ``fail_ids``, ``finish_all``, ``calls``, ``recorded``) stay for the tests.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

from bodh.config import Config, Secrets
from bodh.ids import long_to_short, normalize_item_id, short_to_long
from bodh.llm import LLM
from bodh.readers.bazqux import READ, READING_LIST, STARRED, BazQuxClient, ItemRef, Subscription, label

# -- articles -------------------------------------------------------------------


@dataclass
class FakeItem:
    """What the reader hands the pipeline: the client's ``Item`` shape, built by hand."""

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


def make_item(
    n: int,
    feed: str = "Feed A",
    title: str | None = None,
    url: str | None = None,
    html: str = "<p>Some text about things.</p>",
    crawled_msec: int | None = None,
    author: str = "",
    published: int | None = None,
) -> FakeItem:
    return FakeItem(
        id=short_to_long(n),
        short_id=n,
        feed_stream_id=f"feed/https://example.com/{feed.replace(' ', '')}/rss",
        feed_title=feed,
        feed_html_url=f"https://example.com/{feed.replace(' ', '')}/",
        url=url or f"https://example.com/{feed.replace(' ', '')}/{n}",
        title=title or f"Article {n}",
        author=author,
        published=published,
        html=html,
        crawled_msec=crawled_msec,
    )


# -- the reader, in memory ------------------------------------------------------


class FakeReader:
    """The client's interface over a dictionary of items. Reads come from the lists given;
    every write is recorded and applied, so a test can check what BazQux would show."""

    def __init__(
        self,
        items: Iterable[FakeItem] = (),
        unread: list[str] | None = None,
        starred: list[str] | None = None,
        labels: dict[str, Any] | None = None,
        folders: dict[str, str] | None = None,
        email: str = "test@example.com",
    ) -> None:
        self.items: dict[str, FakeItem] = {i.id: i for i in items}
        self.unread = list(unread if unread is not None else self.items)
        self.starred = list(starred or [])
        # a label's value may be an exception: a stream BazQux cannot serve right now
        self.labels: dict[str, Any] = {
            k: (v if isinstance(v, Exception) else list(v)) for k, v in (labels or {}).items()
        }
        self.folders = dict(folders or {})  # feed title -> folder name
        self.email = email
        self.tagged: list[tuple[str, list[str]]] = []
        self.untagged: list[tuple[str, list[str]]] = []
        self.read: list[list[str]] = []
        self.unread_marked: list[list[str]] = []

    # reads
    def login(self) -> str:
        return "fake-auth"

    def user_info(self) -> dict[str, Any]:
        return {"userId": "1", "userEmail": self.email}

    def subscriptions(self) -> list[Subscription]:
        seen: dict[str, Subscription] = {}
        for i in self.items.values():
            if i.feed_stream_id not in seen:
                folder = self.folders.get(i.feed_title)
                seen[i.feed_stream_id] = Subscription(
                    stream_id=i.feed_stream_id,
                    title=i.feed_title,
                    html_url=i.feed_html_url,
                    folders=[folder] if folder else [],
                )
        return list(seen.values())

    def tags(self) -> list[str]:
        names = sorted(
            set(self.folders.values()) | {k for k in self.labels if not isinstance(self.labels[k], Exception)}
        )
        return [label(n) for n in names]

    def item_refs(self, stream: str, exclude: str | None = None, limit: int | None = None) -> list[ItemRef]:
        if stream == READING_LIST:
            ids = list(self.items)
        elif stream == STARRED:
            ids = list(self.starred)
        elif stream.startswith("user/-/label/"):
            ids = self.label_ids(stream[len("user/-/label/") :], strict=True)
        else:
            ids = [i.id for i in self.items.values() if i.feed_stream_id == stream]
        if exclude == READ:
            unread = set(self.unread)
            ids = [i for i in ids if i in unread]
        refs = [ItemRef(id=i, timestamp_usec=None) for i in ids]
        return refs[:limit] if limit else refs

    def unread_ids(self) -> list[ItemRef]:
        return [ItemRef(id=i, timestamp_usec=None) for i in self.unread]

    def starred_ids(self) -> list[str]:
        return list(self.starred)

    def label_ids(self, name: str, unread_only: bool = False, strict: bool = False) -> list[str]:
        value = self.labels.get(name)
        if isinstance(value, Exception):
            if strict:
                raise value
            return []
        ids = list(value or [])
        if not ids and name in self.folders.values():
            ids = [i.id for i in self.items.values() if self.folders.get(i.feed_title) == name]
        if unread_only:
            ids = [i for i in ids if i in set(self.unread)]
        return ids

    def contents(self, ids: Iterable[str]) -> tuple[list[FakeItem], list[str]]:
        wanted = [normalize_item_id(i) for i in ids]
        found = [self.items[i] for i in wanted if i in self.items]
        return found, [i for i in wanted if i not in self.items]

    # writes
    def edit_tag(self, item_ids: Iterable[str], add: str | None = None, remove: str | None = None) -> int:
        ids = [normalize_item_id(i) for i in item_ids]
        if not ids or not (add or remove):
            return 0
        if add == READ:
            return self.mark_read(ids)
        if remove == READ:
            return self.mark_unread(ids)
        if add:
            return self.tag(ids, add.removeprefix("user/-/label/"))
        assert remove is not None
        return self.untag(ids, remove.removeprefix("user/-/label/"))

    def tag(self, ids: Iterable[str], name: str) -> int:
        ids = list(ids)
        self.tagged.append((name, ids))
        current = self.labels.get(name)
        if not isinstance(current, Exception):
            self.labels[name] = list(dict.fromkeys(list(current or []) + ids))
        return len(ids)

    def untag(self, ids: Iterable[str], name: str) -> int:
        ids = list(ids)
        self.untagged.append((name, ids))
        current = self.labels.get(name)
        if current and not isinstance(current, Exception):
            gone = set(ids)
            self.labels[name] = [i for i in current if i not in gone]
        return len(ids)

    def mark_read(self, ids: Iterable[str]) -> int:
        ids = list(ids)
        self.read.append(ids)
        gone = set(ids)
        self.unread = [i for i in self.unread if i not in gone]
        return len(ids)

    def mark_unread(self, ids: Iterable[str]) -> int:
        ids = list(ids)
        self.unread_marked.append(ids)
        self.unread += [i for i in ids if i not in self.unread]
        return len(ids)


FakeClient = FakeReader  # the name the tests used before Phase 4


# -- the BazQux HTTP API --------------------------------------------------------


class FakeBazQuxServer:
    """BazQux as the client sees it, behind ``httpx.MockTransport``. State: items, the unread
    set, stars, labels (tags, smart streams and folders). Knobs: ``expire_auth_after`` (the
    auth token goes stale after that many authenticated requests: 401 once, then a new login
    works), ``fail_next[path]`` (status codes to answer with, once each, before the real
    answer), ``page_size`` (items per page of ``stream/items/ids``)."""

    def __init__(
        self,
        items: Iterable[FakeItem] = (),
        *,
        unread: Iterable[str] | None = None,
        starred: Iterable[str] = (),
        labels: dict[str, Iterable[str]] | None = None,
        folders: dict[str, str] | None = None,
        email: str = "reader@example.com",
        password: str = "hunter2",
        page_size: int = 1000,
    ) -> None:
        self.items: dict[str, FakeItem] = {i.id: i for i in items}
        self.unread: set[str] = set(unread if unread is not None else self.items)
        self.starred: set[str] = set(starred)
        self.labels: dict[str, set[str]] = {k: set(v) for k, v in (labels or {}).items()}
        self.folders = dict(folders or {})  # feed title -> folder
        self.email, self.password = email, password
        self.page_size = page_size
        self.expire_auth_after: int | None = None
        self.reject_auth_always = False  # every authenticated request is a 401, logins included
        self.login_without_auth = False  # a 200 from ClientLogin with no Auth= line
        self.fail_next: dict[str, list[int]] = {}
        self.raise_next: dict[str, list[Exception]] = {}  # transport errors, once each
        self.bad_json_next: set[str] = set()  # paths whose next answer is not JSON
        self.loop_continuation = False  # a stream whose continuation never advances
        self.edit_reply = "OK"
        self.garbage_items: list[dict[str, Any]] = []  # appended to every contents answer
        self.logins = 0
        self.write_tokens = 0
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.edits: list[tuple[list[str], str | None, str | None]] = []
        self._auth: str | None = None
        self._authed_requests = 0
        self._token_auth: str | None = None

    # the wiring
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self, email: str | None = None, password: str | None = None) -> BazQuxClient:
        return BazQuxClient(email or self.email, password or self.password, transport=self.transport())

    # state helpers
    def stream_ids(self, stream: str) -> list[str]:
        if stream == READING_LIST:
            return list(self.items)
        if stream == STARRED:
            return [i for i in self.items if i in self.starred]
        if stream.startswith("user/-/label/"):
            name = stream[len("user/-/label/") :]
            if name in self.labels:
                return [i for i in self.items if i in self.labels[name]]
            return [i.id for i in self.items.values() if self.folders.get(i.feed_title) == name]
        return [i.id for i in self.items.values() if i.feed_stream_id == stream]

    def _item_json(self, item: FakeItem) -> dict[str, Any]:
        folder = self.folders.get(item.feed_title)
        categories = [READING_LIST] + ([label(folder)] if folder else [])
        categories += [label(n) for n, ids in self.labels.items() if item.id in ids]
        if item.id not in self.unread:
            categories.append(READ)
        if item.id in self.starred:
            categories.append(STARRED)
        return {
            "id": item.id,
            "title": item.title,
            "author": item.author,
            "published": item.published,
            "crawlTimeMsec": str(item.crawled_msec) if item.crawled_msec is not None else None,
            "alternate": [{"href": item.url, "type": "text/html"}],
            "summary": {"content": item.html},
            "origin": {
                "streamId": item.feed_stream_id,
                "title": item.feed_title,
                "htmlUrl": item.feed_html_url,
            },
            "categories": categories,
        }

    # the handler
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path, dict(request.url.params)))
        errors = self.raise_next.get(path)
        if errors:
            raise errors.pop(0)
        codes = self.fail_next.get(path)
        if codes:
            return httpx.Response(codes.pop(0), text="server error")
        if path == "/accounts/ClientLogin":
            return self._login(request)
        auth = request.headers.get("Authorization", "")
        if self.reject_auth_always or not self._auth or auth != f"GoogleLogin auth={self._auth}":
            return httpx.Response(401, text="Unauthorized")
        self._authed_requests += 1
        if self.expire_auth_after is not None and self._authed_requests > self.expire_auth_after:
            self._auth, self._authed_requests, self.expire_auth_after = None, 0, None
            return httpx.Response(401, text="Unauthorized")  # the session expired under the client
        if path in self.bad_json_next:
            self.bad_json_next.discard(path)
            return httpx.Response(200, text="<html>not json</html>")
        routes = {
            "/reader/api/0/token": self._token,
            "/reader/api/0/user-info": lambda _r: httpx.Response(
                200, json={"userId": "1", "userEmail": self.email}
            ),
            "/reader/api/0/subscription/list": lambda _r: httpx.Response(
                200, json={"subscriptions": self._subscriptions()}
            ),
            "/reader/api/0/tag/list": self._tag_list,
            "/reader/api/0/stream/items/ids": self._ids,
            "/reader/api/0/stream/items/contents": self._contents,
            "/reader/api/0/edit-tag": self._edit_tag,
        }
        route = routes.get(path)
        return route(request) if route else httpx.Response(404, text="not found")

    def _token(self, _request: httpx.Request) -> httpx.Response:
        self.write_tokens += 1
        self._token_auth = self._auth
        return httpx.Response(200, text=f"T{self.write_tokens}")

    def _tag_list(self, _request: httpx.Request) -> httpx.Response:
        names = set(self.folders.values()) | set(self.labels)
        return httpx.Response(
            200, json={"tags": [{"id": label(n)} for n in sorted(names)] + [{"id": STARRED}]}
        )

    def _login(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        self.logins += 1
        if form.get("Email", [""])[0] != self.email or form.get("Passwd", [""])[0] != self.password:
            return httpx.Response(
                403, text="Error=BadAuthentication", headers={"X-BQ-LoginErrorReason": "BadAuthentication"}
            )
        self._auth = f"auth{self.logins}"
        self._authed_requests = 0
        if self.login_without_auth:
            return httpx.Response(200, text="SID=sid\nLSID=lsid\n")
        return httpx.Response(200, text=f"SID=sid\nLSID=lsid\nAuth={self._auth}\n")

    def _subscriptions(self) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for i in self.items.values():
            if i.feed_stream_id in seen:
                continue
            folder = self.folders.get(i.feed_title)
            seen[i.feed_stream_id] = {
                "id": i.feed_stream_id,
                "title": i.feed_title,
                "htmlUrl": i.feed_html_url,
                "categories": [{"id": label(folder), "label": folder}] if folder else [],
            }
        return list(seen.values())

    def _ids(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        ids = self.stream_ids(params.get("s", READING_LIST))
        if params.get("xt") == READ:
            ids = [i for i in ids if i in self.unread]
        n = min(int(params.get("n", "20")), self.page_size)
        start = int(params.get("c", "0") or 0)
        if self.loop_continuation:
            start = 0
        page = ids[start : start + n]
        body: dict[str, Any] = {
            "itemRefs": [
                {
                    "id": str(long_to_short(i)),
                    "timestampUsec": str((self.items[i].crawled_msec or 0) * 1000),
                    "directStreamIds": [self.items[i].feed_stream_id],
                }
                for i in page
            ]
        }
        if start + n < len(ids):
            body["continuation"] = "0" if self.loop_continuation else str(start + n)
        return httpx.Response(200, json=body)

    def _write_token_ok(self, form: dict[str, list[str]]) -> bool:
        token = form.get("T", [""])[0]
        return bool(token) and self._token_auth == self._auth

    def _contents(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        if not self._write_token_ok(form):
            return httpx.Response(401, text="Unauthorized")
        wanted = [normalize_item_id(i) for i in form.get("i", [])]
        if len(wanted) > 250:
            return httpx.Response(400, text="too many items")
        items = [self._item_json(self.items[i]) for i in wanted if i in self.items] + self.garbage_items
        return httpx.Response(200, json={"items": items})

    def _edit_tag(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        if not self._write_token_ok(form):
            return httpx.Response(401, text="Unauthorized")
        ids = [normalize_item_id(i) for i in form.get("i", [])]
        if len(ids) > 10_000:
            return httpx.Response(400, text="too many items")
        add, remove = form.get("a", [None])[0], form.get("r", [None])[0]
        self.edits.append((ids, add, remove))
        for stream, on in ((add, True), (remove, False)):
            if not stream:
                continue
            if stream == READ:
                (self.unread.difference_update if on else self.unread.update)(ids)
            elif stream == STARRED:
                (self.starred.update if on else self.starred.difference_update)(ids)
            elif stream.startswith("user/-/label/"):
                name = stream[len("user/-/label/") :]
                members = self.labels.setdefault(name, set())
                (members.update if on else members.difference_update)(ids)
        return httpx.Response(200, text=self.edit_reply)


# -- the model ------------------------------------------------------------------


def score_for(title: str, purpose: str) -> int:
    """The scripted judge: 'gold' is excellent, 'junk' is junk, 'edge' sits just under the
    cut-off on the first pass and is saved by the review, 'edge2' is confirmed junk by the
    review; anything else is middling."""
    title = title.lower()
    if "gold" in title:
        return 90
    if "junk" in title:
        return 10
    if "edge2" in title:
        return 34 if purpose == "second_look" else 33
    if "edge" in title:
        return 60 if purpose == "second_look" else 33
    return 55


# the shapes the SDK hands back, reduced to what the code reads
@dataclass
class FakeText:
    text: str
    type: str = "text"


@dataclass
class FakeCacheCreation:
    ephemeral_1h_input_tokens: int = 0
    ephemeral_5m_input_tokens: int = 0


@dataclass
class FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation: FakeCacheCreation | None = None


@dataclass
class FakeMessage:
    content: list[Any]
    usage: FakeUsage = field(default_factory=FakeUsage)
    stop_reason: str = "end_turn"


@dataclass
class FakeSucceeded:
    message: FakeMessage
    type: str = "succeeded"


@dataclass
class FakeErrorBody:
    message: str
    type: str = "api_error"


@dataclass
class FakeError:
    error: FakeErrorBody


@dataclass
class FakeErrored:
    error: FakeError
    type: str = "errored"


@dataclass
class FakeEnded:
    """An ``expired`` or ``canceled`` result: no message, no error body."""

    type: str


@dataclass
class FakeResponse:
    custom_id: str
    result: Any


@dataclass
class FakeBatch:
    id: str
    processing_status: str


def api_error(cls: type[Exception], message: str, status: int = 400) -> Exception:
    """A real SDK error (``anthropic.BadRequestError`` and the rest) carrying a message."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    response = httpx.Response(status, request=request, json=body)
    return cls(message, response=response, body=body)  # type: ignore[call-arg]


_BLOCK = re.compile(r"\[(\d+)\]\n.*?\ntitle: (.*?)\n", re.S)


class FakeAnthropic:
    """The SDK's surface the code uses. Scripted through attributes; see the class docstring
    of this module for the list."""

    def __init__(self, scores: Any = score_for) -> None:
        self.scores = scores
        self.purpose = threading.local()  # set by FakeLLM.judge: the purpose of the call in this thread
        self.calls: list[dict[str, Any]] = []  # every messages.create's params
        self.fail_calls: dict[int, Exception] = {}  # the nth create raises this
        self.fail_with: Exception | None = None  # every create raises this
        self.reject_structured_once = False
        self.reject_long_ttl_once = False
        self.prose = False  # every answer is prose: no judgment in it
        self.prose_ns: set[int] = set()  # these article numbers are left out of the answer
        self.completion: str | None = None  # the text every plain completion answers with, when set
        self.usage = FakeUsage(input_tokens=1000, output_tokens=100)
        self.delay = 0.0
        self.in_flight, self.peak = 0, 0
        self.options: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        # batches
        self.batches: dict[str, list[dict[str, Any]]] = {}
        self.status: dict[str, str] = {}
        self.status_sequence: dict[str, list[str]] = {}  # per batch: the statuses retrieve answers, in turn
        self.fail_ids: set[str] = set()  # custom ids whose result is an error
        self.fail_message = "overloaded"
        self.expire_ids: set[str] = set()
        self.cancel_ids: set[str] = set()
        self.missing_batches: set[str] = set()  # retrieve and results raise NotFoundError
        self.broken_results: set[str] = set()  # results raise a connection error
        self.wrong_ids: dict[str, list[str]] = {}  # results answer these custom ids instead
        self.duplicate_results = False
        self.messages = _Messages(self)

    def with_options(self, **kwargs: Any) -> FakeAnthropic:
        self.options.append(kwargs)
        return self

    def finish_all(self) -> None:
        for bid in self.status:
            self.status[bid] = "ended"

    # the answer
    def _verdicts(self, user_text: str, purpose: str) -> list[dict[str, Any]]:
        out = []
        for m in _BLOCK.finditer(user_text):
            n, title = int(m.group(1)), m.group(2)
            if n in self.prose_ns:
                continue
            s = self.scores(title, purpose)
            out.append(
                {
                    "n": n,
                    "s": [s, s, s, s, s],
                    "lesson": f"lesson {n}",
                    "concept": "gold things" if "gold" in title.lower() else f"topic {n % 3}",
                    "kind": "news",
                    "flags": [],
                    "dupe": None,
                    "conf": "high",
                }
            )
        return out

    def answer(self, params: dict[str, Any], purpose: str) -> FakeMessage:
        user_text = params["messages"][0]["content"]
        if self.prose:
            return FakeMessage([FakeText("I would rather not score these today.")], usage=self.usage)
        if isinstance(params.get("system"), str):  # a plain completion: the profiler's weekly rewrite
            text = self.completion or (
                "## What the student has been learning\nGold things.\n\n## Moving past\nnothing\n\n"
                "## Concepts covered, last 90 days\ngold things\n\n## Expansion topics for next week\n- silver things: adjacent\n\n"
                "## Notes to the teacher\nbe kind"
            )
            return FakeMessage([FakeText(text)], usage=self.usage)
        return FakeMessage(
            [FakeText(json.dumps({"judgments": self._verdicts(user_text, purpose)}))], usage=self.usage
        )


class _Messages:
    def __init__(self, api: FakeAnthropic) -> None:
        self.api = api
        self.batches = _Batches(api)

    def create(self, **params: Any) -> FakeMessage:
        import anthropic

        api = self.api
        with api._lock:
            api.calls.append(params)
            n = len(api.calls)
            api.in_flight += 1
            api.peak = max(api.peak, api.in_flight)
        try:
            if api.delay:
                time.sleep(api.delay)
            if api.fail_with is not None:
                raise api.fail_with
            if n in api.fail_calls:
                raise api.fail_calls.pop(n)
            if api.reject_structured_once and "output_config" in params:
                api.reject_structured_once = False
                raise api_error(
                    anthropic.BadRequestError,
                    "output_config.format: structured outputs are not supported by this model",
                )
            system = params.get("system")
            if (
                api.reject_long_ttl_once
                and isinstance(system, list)
                and any(b.get("cache_control", {}).get("ttl") == "1h" for b in system)
            ):
                api.reject_long_ttl_once = False
                raise api_error(
                    anthropic.BadRequestError, "cache_control: ttl of 1h is not supported for this model"
                )
            purpose = getattr(api.purpose, "value", "teacher")
            return api.answer(params, purpose)
        finally:
            with api._lock:
                api.in_flight -= 1


class _Batches:
    def __init__(self, api: FakeAnthropic) -> None:
        self.api = api

    def create(self, requests: list[dict[str, Any]]) -> FakeBatch:
        api = self.api
        if api.fail_with is not None:
            raise api.fail_with
        bid = f"msgbatch_{len(api.batches) + 1:03d}"
        api.batches[bid] = [dict(r) for r in requests]
        api.status[bid] = "in_progress"
        return FakeBatch(bid, "in_progress")

    def retrieve(self, batch_id: str) -> FakeBatch:
        import anthropic

        api = self.api
        if batch_id in api.missing_batches or batch_id not in api.batches:
            raise api_error(anthropic.NotFoundError, f"batch {batch_id} not found", 404)
        sequence = api.status_sequence.get(batch_id)
        if sequence:
            api.status[batch_id] = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return FakeBatch(batch_id, api.status[batch_id])

    def results(self, batch_id: str) -> Iterable[FakeResponse]:
        import anthropic

        api = self.api
        if batch_id in api.missing_batches or batch_id not in api.batches:
            raise api_error(anthropic.NotFoundError, f"batch {batch_id} not found", 404)
        if batch_id in api.broken_results:
            raise anthropic.APIConnectionError(
                request=httpx.Request("GET", "https://api.anthropic.com/v1/messages/batches")
            )
        out = []
        for req in api.batches[batch_id]:
            cid = req["custom_id"]
            purpose = "second_look" if cid.startswith("s") else "teacher"
            if cid in api.fail_ids:
                out.append(FakeResponse(cid, FakeErrored(FakeError(FakeErrorBody(api.fail_message)))))
            elif cid in api.expire_ids:
                out.append(FakeResponse(cid, FakeEnded("expired")))
            elif cid in api.cancel_ids:
                out.append(FakeResponse(cid, FakeEnded("canceled")))
            else:
                out.append(FakeResponse(cid, FakeSucceeded(api.answer(req["params"], purpose))))
        if batch_id in api.wrong_ids:
            out = [FakeResponse(cid, r.result) for cid, r in zip(api.wrong_ids[batch_id], out, strict=False)]
        if api.duplicate_results:
            out = out * 2
        return out


class FakeLLM(LLM):
    """The real client over a ``FakeAnthropic`` (``self.api``). ``calls`` lists (model, purpose)
    per synchronous call; ``recorded`` lists (model, purpose, discount) per cost computed."""

    def __init__(self, cfg: Config, conn: Any, scores: Any = score_for) -> None:
        super().__init__(cfg, Secrets(anthropic_api_key="sk-ant-test-key"), conn)
        self.api = FakeAnthropic(scores)
        self._client: Any = self.api
        self.calls: list[tuple[str, str]] = []
        self.recorded: list[tuple[str, str, float]] = []
        self._recording = threading.local()

    # the older fakes' knobs
    @property
    def batches(self) -> dict[str, list[dict[str, Any]]]:
        return self.api.batches

    @property
    def fail_ids(self) -> set[str]:
        return self.api.fail_ids

    def finish_all(self) -> None:
        self.api.finish_all()

    def judge(self, model: str, system_text: str, user_text: str, run_id: int | None, purpose: str) -> Any:
        self.calls.append((model, purpose))
        self.api.purpose.value = purpose
        return super().judge(model, system_text, user_text, run_id, purpose)

    def complete(
        self,
        model: str,
        system_text: str,
        user_text: str,
        run_id: int | None,
        purpose: str,
        max_tokens: int = 2000,
    ) -> str:
        self.calls.append((model, purpose))
        return super().complete(model, system_text, user_text, run_id, purpose, max_tokens)

    def record(
        self, run_id: int | None, model: str, purpose: str, usage: Any, discount: float = 1.0
    ) -> float:
        self._recording.purpose = purpose
        try:
            return super().record(run_id, model, purpose, usage, discount)
        finally:
            self._recording.purpose = None

    def cost(self, model: str, usage: Any, discount: float = 1.0) -> tuple[dict[str, int], float]:
        self.recorded.append((model, getattr(self._recording, "purpose", None) or "batch", discount))
        return super().cost(model, usage, discount)
