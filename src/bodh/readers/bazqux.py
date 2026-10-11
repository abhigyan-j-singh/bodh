"""A small, hardened client for BazQux Reader's Google Reader-compatible API.

Everything the system does to BazQux goes through here. Rules learned the hard way
and from BazQux's own API notes (github.com/bazqux/bazqux-api):

- find unread items by paginating ``stream/items/ids`` with ``c``; never ``ot=``
- contents are a POST of ``i=`` ids with a ``T`` token, 100-250 per request
- ``edit-tag`` adds (``a=``) or removes (``r=``) one tag per call, <= 10,000 ids
- <= 2 requests a second, exponential backoff on 5xx and connection errors,
  one re-login on 401, tolerant parsing (ids that vanished are not errors)
"""

from __future__ import annotations

import html
import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..errors import ReaderError
from ..ids import long_to_short, normalize_item_id

log = logging.getLogger(__name__)

BASE_URL = "https://bazqux.com"
READING_LIST = "user/-/state/com.google/reading-list"
READ = "user/-/state/com.google/read"
STARRED = "user/-/state/com.google/starred"

MIN_REQUEST_INTERVAL = 0.5
MAX_RETRIES = 4
BACKOFF_BASE = 1.5
IDS_PAGE = 1000
MAX_PAGES = 500  # 500,000 items: more than any reading list; a continuation that loops stops here
CONTENTS_BATCH = 200
EDIT_BATCH = 500


def label(name: str) -> str:
    """A folder, tag or smart stream as a stream id."""
    return f"user/-/label/{name}"


class BazQuxError(ReaderError):
    """BazQux could not be reached, refused, or answered badly."""


class BazQuxAuthError(BazQuxError):
    pass


class _Unauthorized(Exception):
    pass


@dataclass
class ItemRef:
    id: str  # long form
    timestamp_usec: int | None
    direct_stream_ids: list[str] = field(default_factory=list)


@dataclass
class Item:
    id: str  # long form
    short_id: int
    feed_stream_id: str
    feed_title: str
    feed_html_url: str
    url: str
    title: str
    author: str
    published: int | None  # unix seconds
    crawled_msec: int | None
    html: str
    categories: list[str] = field(default_factory=list)


@dataclass
class Subscription:
    stream_id: str
    title: str
    html_url: str
    folders: list[str]


class BazQuxClient:
    """All item ids come back in long form; all writes are idempotent."""

    def __init__(self, email: str, password: str, base_url: str = BASE_URL, transport: Any = None) -> None:
        if not email or not password:
            raise BazQuxAuthError("BAZQUX_EMAIL / BAZQUX_PASSWORD not configured")
        self._email = email
        self._password = password
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(timeout=30.0, transport=transport, follow_redirects=True)
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._auth: str | None = None
        self._token: str | None = None
        self._token_at = 0.0

    # -- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self._base}{path}"
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            with self._lock:
                wait = MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
                self._last_request = time.monotonic()
            try:
                response = self._http.request(method, url, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                sleep = BACKOFF_BASE**attempt
                log.warning("bazqux: %s %s failed (%s); retry in %.1fs", method, path, exc, sleep)
                time.sleep(sleep)
                continue
            if response.status_code == 401:
                raise _Unauthorized(path)
            if response.status_code >= 500 or response.status_code == 429:
                sleep = BACKOFF_BASE**attempt
                log.warning("bazqux: %s %s -> %d; retry in %.1fs", method, path, response.status_code, sleep)
                time.sleep(sleep)
                last_exc = BazQuxError(f"HTTP {response.status_code} from {path}")
                continue
            return response
        raise BazQuxError(f"bazqux request failed after {MAX_RETRIES} attempts: {last_exc}")

    def _authed(self, method: str, path: str, with_token: bool = False, **kwargs: Any) -> httpx.Response:
        """A request with the auth header; on a 401, log in once more and retry. With
        ``with_token`` the write token goes into the form data on each attempt, so the retry
        after a re-login carries a token issued under the new session, not the old one."""
        for attempt in range(2):
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"GoogleLogin auth={self.auth}"
            if with_token:
                kwargs["data"] = {**(kwargs.get("data") or {}), "T": self._write_token()}
            try:
                response = self._request(method, path, headers=headers, **kwargs)
            except _Unauthorized:
                if attempt == 1:
                    raise BazQuxAuthError("BazQux rejected the auth token twice") from None
                log.info("bazqux: auth token rejected; logging in again")
                self._auth = None
                self._token = None
                continue
            if response.status_code != 200:
                raise BazQuxError(f"{path}: HTTP {response.status_code}: {response.text[:200]}")
            return response
        raise BazQuxAuthError("unreachable")

    # -- auth ---------------------------------------------------------------

    def login(self) -> str:
        try:
            response = self._request(
                "POST", "/accounts/ClientLogin", data={"Email": self._email, "Passwd": self._password}
            )
        except _Unauthorized:
            raise BazQuxAuthError("BazQux login failed: HTTP 401 (wrong email or password?)") from None
        if response.status_code != 200:
            reason = response.headers.get("X-BQ-LoginErrorReason", "")
            detail = f" ({reason})" if reason else ""
            raise BazQuxAuthError(f"BazQux login failed: HTTP {response.status_code}{detail}")
        for line in response.text.splitlines():
            if line.startswith("Auth="):
                self._auth = line[5:].strip()
                log.info("bazqux: logged in")  # never the account email: logs reach the dashboard
                return self._auth
        raise BazQuxAuthError("BazQux login response carried no Auth token")

    @property
    def auth(self) -> str:
        if self._auth is None:
            self.login()
        assert self._auth is not None
        return self._auth

    def _write_token(self) -> str:
        # write tokens expire after about 30 minutes; refresh at 20
        if self._token is None or time.monotonic() - self._token_at > 20 * 60:
            self._token = self._authed("GET", "/reader/api/0/token").text.strip()
            self._token_at = time.monotonic()
        return self._token

    # -- reads --------------------------------------------------------------

    def user_info(self) -> dict:
        return self._authed("GET", "/reader/api/0/user-info", params={"output": "json"}).json()

    def subscriptions(self) -> list[Subscription]:
        data = self._authed("GET", "/reader/api/0/subscription/list", params={"output": "json"}).json()
        out = []
        for sub in data.get("subscriptions", []):
            folders = []
            for cat in sub.get("categories") or []:
                name = cat.get("label") or str(cat.get("id", "")).rsplit("/", 1)[-1]
                if name:
                    folders.append(name)
            out.append(
                Subscription(
                    stream_id=str(sub.get("id", "")),
                    title=str(sub.get("title", "")),
                    html_url=str(sub.get("htmlUrl", "")),
                    folders=folders,
                )
            )
        return out

    def tags(self) -> list[str]:
        """Folders, tags and smart streams, as stream ids."""
        data = self._authed("GET", "/reader/api/0/tag/list", params={"output": "json"}).json()
        return [str(t.get("id", "")) for t in data.get("tags", [])]

    def item_refs(self, stream: str, exclude: str | None = None, limit: int | None = None) -> list[ItemRef]:
        """Every item ref in a stream, by continuation. ``exclude`` is an ``xt=`` stream."""
        out: list[ItemRef] = []
        continuation: str | None = None
        seen: set[str] = set()
        for _page in range(MAX_PAGES):
            params: dict[str, Any] = {"s": stream, "n": IDS_PAGE, "output": "json"}
            if exclude:
                params["xt"] = exclude
            if continuation:
                params["c"] = continuation
            data = self._authed("GET", "/reader/api/0/stream/items/ids", params=params).json()
            refs = data.get("itemRefs", []) or []
            for ref in refs:
                try:
                    out.append(
                        ItemRef(
                            id=normalize_item_id(ref["id"]),
                            timestamp_usec=_int_or_none(ref.get("timestampUsec")),
                            direct_stream_ids=[str(s) for s in ref.get("directStreamIds") or []],
                        )
                    )
                except Exception as exc:  # one malformed reference must not lose the page
                    log.error("bazqux: bad item ref %r (%s)", ref, exc)
            continuation = data.get("continuation")
            if not continuation or not refs or (limit and len(out) >= limit):
                return out
            if continuation in seen:
                # a shorter list than the truth would read as "everything else was read" (or
                # "nothing else is protected"): a stream that loops is an error, not an answer
                raise BazQuxError(
                    f"{stream}: the continuation {continuation!r} repeated after {len(out)} items"
                )
            seen.add(continuation)
        raise BazQuxError(f"{stream}: no end after {MAX_PAGES} pages ({len(out)} items)")

    def unread_ids(self) -> list[ItemRef]:
        return self.item_refs(READING_LIST, exclude=READ)

    def starred_ids(self) -> list[str]:
        return [r.id for r in self.item_refs(STARRED)]

    def label_ids(self, name: str, unread_only: bool = False, strict: bool = False) -> list[str]:
        """The items carrying a tag, folder or smart stream. A label BazQux cannot serve reads
        as empty (a tag nobody has used yet is the usual case), unless ``strict``: a caller
        whose decision depends on the answer (which articles are protected) gets the error."""
        try:
            return [r.id for r in self.item_refs(label(name), exclude=READ if unread_only else None)]
        except BazQuxError as exc:
            if strict:
                raise
            log.info("bazqux: label %r unavailable (%s); treating as empty", name, exc)
            return []

    def contents(self, item_ids: Iterable[str]) -> tuple[list[Item], list[str]]:
        """Batch-fetch items. Returns (items, vanished): ids BazQux no longer
        returns were hidden by a filter or evicted; they are tolerated."""
        ids = [normalize_item_id(i) for i in item_ids]
        items: list[Item] = []
        returned: set[str] = set()
        for start in range(0, len(ids), CONTENTS_BATCH):
            batch = ids[start : start + CONTENTS_BATCH]
            response = self._authed(
                "POST",
                "/reader/api/0/stream/items/contents",
                with_token=True,
                params={"output": "json"},
                data={"i": batch},
            )
            try:
                data = response.json()
            except Exception as exc:
                raise BazQuxError(f"contents JSON parse failure: {exc}") from exc
            for raw in data.get("items", []) or []:
                try:
                    item = _parse_item(raw)
                except Exception as exc:  # one malformed item must not lose the batch
                    log.error("bazqux: item parse failure (%s); raw: %.300s", exc, str(raw))
                    continue
                items.append(item)
                returned.add(item.id)
        vanished = [i for i in ids if i not in returned]
        if vanished:
            log.info("bazqux: %d requested items no longer upstream", len(vanished))
        return items, vanished

    # -- writes ---------------------------------------------------------------

    def edit_tag(self, item_ids: Iterable[str], add: str | None = None, remove: str | None = None) -> int:
        """Add or remove one tag on many items, in batches. Returns the count written."""
        ids = [normalize_item_id(i) for i in item_ids]
        if not ids or not (add or remove):
            return 0
        done = 0
        for start in range(0, len(ids), EDIT_BATCH):
            batch = ids[start : start + EDIT_BATCH]
            data: dict[str, Any] = {"i": batch}
            if add:
                data["a"] = add
            if remove:
                data["r"] = remove
            response = self._authed("POST", "/reader/api/0/edit-tag", with_token=True, data=data)
            if response.text.strip() != "OK":
                raise BazQuxError(f"edit-tag returned {response.text[:100]!r}")
            done += len(batch)
        return done

    def mark_read(self, item_ids: Iterable[str]) -> int:
        return self.edit_tag(item_ids, add=READ)

    def mark_unread(self, item_ids: Iterable[str]) -> int:
        return self.edit_tag(item_ids, remove=READ)

    def tag(self, item_ids: Iterable[str], name: str) -> int:
        return self.edit_tag(item_ids, add=label(name))

    def untag(self, item_ids: Iterable[str], name: str) -> int:
        return self.edit_tag(item_ids, remove=label(name))


def _int_or_none(value: Any) -> int | None:
    """A timestamp as BazQux writes it (an int, a digit string, now and then "1.7e9"), or
    None: a bad timestamp must not drop the item, which would be refetched every run."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return None


def _first_href(entries: object) -> str:
    if isinstance(entries, (dict, str)):
        entries = [entries]
    for entry in entries or []:  # type: ignore[union-attr]
        if isinstance(entry, dict) and entry.get("href"):
            return str(entry["href"])
        if isinstance(entry, str) and entry:
            return entry
    return ""


def _parse_item(raw: dict) -> Item:
    long_id = normalize_item_id(raw.get("id", ""))
    origin = raw.get("origin") or {}
    url = _first_href(raw.get("canonical")) or _first_href(raw.get("alternate"))
    body = ""
    summary = raw.get("summary") or {}
    if isinstance(summary, dict) and summary.get("content"):
        body = str(summary["content"])
    content = raw.get("content") or {}
    if isinstance(content, dict) and len(str(content.get("content") or "")) > len(body):
        body = str(content["content"])
    return Item(
        id=long_id,
        short_id=long_to_short(long_id),
        feed_stream_id=str(origin.get("streamId") or ""),
        feed_title=str(origin.get("title") or ""),
        feed_html_url=str(origin.get("htmlUrl") or ""),
        url=url,
        title=html.unescape(str(raw.get("title") or "")),
        author=html.unescape(str(raw.get("author") or "")),
        published=_int_or_none(raw.get("published")),
        crawled_msec=_int_or_none(raw.get("crawlTimeMsec")),
        html=body,
        categories=[str(c) for c in raw.get("categories") or []],
    )
