"""The teacher: a strong model judges every article as teaching material for one
student. One call scores a numbered batch; a second, stronger model re-reads close
calls with the full text before anything is cut. All model access lives here.

Two ways to run the first pass. Synchronous: the run waits for every call. Batch
(the default): a run submits what is pending to the Message Batches API at half
price and records the judgments on the next run, half an hour later. Second looks
travel the same way in batch mode, so a close call waits one run before it can be cut.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, ValidationError

from . import store
from .config import Config, Secrets
from .ids import resolve_ordinal

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

KINDS = ["primary_source", "original_analysis", "reporting", "commentary", "aggregation", "promo", "other"]
FLAGS = ["primary_source", "home_angle", "vendor_marketing_no_data", "funding_offfield",
         "perishable", "paywalled_stub", "duplicate"]

# the compact codes the model writes (one token each) and what they mean in the database
KIND_CODES = {"src": "primary_source", "analysis": "original_analysis", "news": "reporting",
              "opinion": "commentary", "roundup": "aggregation", "promo": "promo", "other": "other"}
FLAG_CODES = {"primary": "primary_source", "home": "home_angle", "vendor": "vendor_marketing_no_data",
              "funding": "funding_offfield", "perishable": "perishable", "stub": "paywalled_stub",
              "dupe": "duplicate",
              "india": "home_angle"}  # the code the prompt used before home_angle was configurable
# flag names written by earlier versions, read as today's
FLAG_ALIASES = {"india_angle": "home_angle", "funding_round_non_infra": "funding_offfield"}
SUBSCORES = ("quality", "learning", "fit", "breadth", "relevance")


def normalize_flags(flags: list[str] | None) -> list[str]:
    """Flag names as the code knows them today, whatever version wrote them."""
    return [FLAG_ALIASES.get(f, f) for f in (flags or [])]

BATCH_WAIT_MINUTES = 60   # a half-empty batch is still submitted once its oldest article has waited this long
BATCH_LOST_HOURS = 26     # the API expires a batch after 24h; one never seen to end by then is given up on


class Verdict(BaseModel):
    """One judgment as the rest of the system sees it (long names, bounded scores)."""

    n: int
    quality: int = Field(ge=0, le=100)
    learning: int = Field(ge=0, le=100)
    fit: int = Field(ge=0, le=100)
    breadth: int = Field(ge=0, le=100)
    relevance: int = Field(ge=0, le=100)
    lesson: str = ""
    concept: str = ""
    kind: str = "other"
    flags: list[str] = Field(default_factory=list)
    dupe_of: int | str | None = None  # an ordinal from the model, or the resolved article id
    confidence: Literal["high", "medium", "low"] = "medium"


KindCode = Literal["src", "analysis", "news", "opinion", "roundup", "promo", "other"]
FlagCode = Literal["primary", "home", "vendor", "funding", "perishable", "stub", "dupe"]


class JudgmentOut(BaseModel):
    """The shape the model writes: about half the output tokens of the long form. This is
    the schema sent with every request; nothing is parsed with it (see JudgmentIn)."""

    n: int = Field(description="the article's number in this batch")
    s: list[int] = Field(description="five integers 0-100 in this order: quality, learning, fit, breadth, relevance")
    lesson: str = Field(description="at most 12 words: what the student takes from it, or what the piece is instead")
    concept: str = Field(description="one to three lowercase words: the concept or field taught")
    kind: KindCode = Field(description="src, analysis, news, opinion, roundup, promo or other")
    flags: list[FlagCode] = Field(description="any of primary, home, vendor, funding, perishable, stub, dupe; usually empty")
    dupe: int | None = Field(description="the number of an earlier article in this batch telling the same story, else null")
    conf: Literal["high", "medium", "low"] = Field(description="low when the text is too thin to judge")


class JudgmentBatchOut(BaseModel):
    judgments: list[JudgmentOut]


class JudgmentIn(BaseModel):
    """What comes back, read leniently: the API does not promise the case of enum values,
    and structured outputs cannot enforce bounds or lengths, so everything is normalized here."""

    n: int
    s: list[int]
    lesson: str | None = ""          # None in any of these is read as "not given", never as a dropped judgment
    concept: str | None = ""
    kind: str | None = "other"
    flags: list[str | None] | None = None
    dupe: int | str | None = None
    conf: str | None = "medium"

    def to_verdict(self) -> Verdict:
        if len(self.s) != len(SUBSCORES):
            raise ValueError(f"expected {len(SUBSCORES)} sub-scores, got {len(self.s)}")
        scores = {name: max(0, min(100, int(val))) for name, val in zip(SUBSCORES, self.s, strict=True)}
        flags = [FLAG_CODES[f] for f in ((x or "").strip().lower() for x in self.flags or []) if f in FLAG_CODES]
        dupe: int | None = None
        if isinstance(self.dupe, int):
            dupe = self.dupe
        elif isinstance(self.dupe, str) and self.dupe.strip().isdecimal():
            try:
                dupe = int(self.dupe.strip())
            except ValueError:
                dupe = None
        conf = (self.conf or "").strip().lower()
        return Verdict(
            n=self.n, **scores, lesson=(self.lesson or "").strip(), concept=(self.concept or "").strip().lower(),
            kind=KIND_CODES.get((self.kind or "").strip().lower(), "other"), flags=flags, dupe_of=dupe,
            confidence=conf if conf in ("high", "medium", "low") else "medium",  # type: ignore[arg-type]
        )


PLAIN_JSON_INSTRUCTION = (
    "\n\nRespond with only a JSON object, no prose and no code fence, of the form "
    '{"judgments": [{"n": 1, "s": [0, 0, 0, 0, 0], "lesson": "", "concept": "", "kind": "other", '
    '"flags": [], "dupe": null, "conf": "medium"}]}'
)


class TeacherError(Exception):
    pass


class BudgetExceeded(TeacherError):
    pass


class ModelConfigError(TeacherError):
    """The API rejected the key, the permission or the model id. Retrying is pointless;
    the run stops at once so the problem is visible instead of 27 identical tracebacks."""


class BatchNotFound(TeacherError):
    """The API no longer knows the batch (deleted in the console, or submitted under another
    key): it is closed as lost and its articles are pending again, nothing else stops."""


class NoJudgments(TeacherError):
    """The model answered, but with nothing a judgment could be read from."""


def _batch_classify(exc: Exception) -> Exception:
    import anthropic

    if isinstance(exc, anthropic.NotFoundError):
        return BatchNotFound(f"batch not found: {exc}")
    return _classify(exc)


def _classify(exc: Exception) -> Exception:
    import anthropic

    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError)):
        return ModelConfigError(f"model call rejected: {exc}. Check ANTHROPIC_API_KEY and the model ids in config.yaml")
    return exc


_STRUCTURED_WORDS = re.compile(r"\b(output_config|output_format|structured|json_schema)\b", re.I)
_TTL_WORDS = re.compile(r"\b(ttl|cache_control)\b", re.I)


def _mentions_structured_outputs(text: str) -> bool:
    return bool(_STRUCTURED_WORDS.search(text or ""))


def _mentions_cache_ttl(text: str) -> bool:
    # whole words only: "throttled" contains "ttl" and used to disable the 1-hour cache for good
    return bool(_TTL_WORDS.search(text or ""))


def load_prompt(name: str, root: Path | None = None) -> str:
    """A prompt by name. A copy in <root>/prompts/ overrides the packaged one, so
    prompts can be edited next to config.yaml without touching the package."""
    candidates = [PROMPTS_DIR / f"{name}.md"]
    if root is not None:
        candidates.insert(0, root / "prompts" / f"{name}.md")
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    raise TeacherError(f"prompt {name!r} not found in {[str(c) for c in candidates]}")


def fill_prompt(text: str, cfg: Config) -> str:
    """The prompt's placeholders: `<<home_flag>>` names the student's home ground when one is
    configured (the model then writes the `home` flag) and disappears when none is."""
    home = cfg.scoring.home_angle.strip()
    home_flag = f"home (an angle on the student's home ground, {home}), " if home else ""
    return text.replace("<<home_flag>>", home_flag)


def parse_judgments(text: str) -> list[Verdict] | None:
    """JSON text (structured output or the plain fallback) to verdicts. Malformed
    judgments are dropped, never guessed; unparsable text returns None."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        raw = json.loads(text[start: end + 1])
    except json.JSONDecodeError:
        return None
    out: list[Verdict] = []
    for item in (raw.get("judgments") if isinstance(raw, dict) else None) or []:
        try:
            out.append(JudgmentIn(**item).to_verdict())
        except (ValidationError, ValueError, TypeError) as exc:
            log.warning("teacher: dropping malformed judgment %r (%s)", item, exc)
    return out


# -- composition --------------------------------------------------------------

def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2}


def matches_topic(concept: str, topics: list[str]) -> bool:
    c = (concept or "").strip().lower()
    if not c:
        return False
    cw = _words(c)
    for t in topics:
        t = t.strip().lower()
        if not t:
            continue
        if t in c or c in t:
            return True
        tw = _words(t)
        if cw and tw and len(cw & tw) / len(cw | tw) >= 0.5:
            return True
    return False


def compose(v: Verdict | dict, cfg: Config, expansion_topics: list[str] | None = None) -> float:
    """Sub-scores to one score, deterministically, so weights can change without re-prompting."""
    d = v.model_dump() if isinstance(v, Verdict) else dict(v)
    w = cfg.scoring.weights
    s = sum(w[k] * float(d.get(k, 0)) for k in w)
    flags = set(normalize_flags(d.get("flags")))
    if "home_angle" in flags and cfg.scoring.home_angle:
        s += cfg.scoring.home_bonus
    if "vendor_marketing_no_data" in flags or d.get("kind") == "promo":
        s = min(s, 30)
    if "funding_offfield" in flags:
        s = min(s, 40)
    if d.get("dupe_of") or "duplicate" in flags:
        s = min(s, 20)
    if expansion_topics and matches_topic(d.get("concept", ""), expansion_topics):
        s += cfg.scoring.expansion_bonus
    return round(max(0.0, min(100.0, s)), 1)


# -- duplicates -----------------------------------------------------------------

_TRACKING = re.compile(r"^(utm_|fbclid|gclid|ref$|source$|mc_cid|mc_eid)")


def canonical_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)])
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", host, path, query, ""))


def normalized_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (title or "").lower()).strip()


class DupIndex:
    """Earlier articles telling the same story: the same canonical URL, or the same
    normalized title in a different feed, within the window. Built once per scoring pass."""

    def __init__(self, conn: sqlite3.Connection, window_days: int = 7) -> None:
        since = store.days_ago_iso(window_days)
        self.by_url: dict[str, dict] = {}
        self.by_title: dict[str, dict] = {}
        for cand in conn.execute(
            """SELECT a.id, a.url, a.title, a.feed_title, s.concept
               FROM articles a JOIN scores s ON s.article_id = a.id
               WHERE a.fetched_at >= ? AND (s.dupe_of IS NULL OR s.dupe_of = '')""",
            (since,),
        ):
            self.add(dict(cand))

    def add(self, cand: dict) -> None:
        curl = canonical_url(cand.get("url") or "")
        if curl:
            self.by_url.setdefault(curl, cand)
        ntitle = normalized_title(cand.get("title") or "")
        if len(ntitle) > 25:
            self.by_title.setdefault(ntitle, cand)

    def find(self, row: sqlite3.Row | dict) -> dict | None:
        curl = canonical_url(row["url"] or "")
        hit = self.by_url.get(curl) if curl else None
        if hit and hit["id"] != row["id"]:
            return hit
        ntitle = normalized_title(row["title"] or "")
        hit = self.by_title.get(ntitle) if len(ntitle) > 25 else None
        if hit and hit["id"] != row["id"] and hit.get("feed_title") != row["feed_title"]:
            return hit
        return None


def find_duplicate(conn: sqlite3.Connection, row: sqlite3.Row, window_days: int = 7) -> dict | None:
    return DupIndex(conn, window_days).find(row)


# -- the model boundary -------------------------------------------------------

def _output_format() -> dict:
    """The structured-outputs format for a judgment batch, built the way the SDK's
    ``messages.parse`` builds it (bounds moved into descriptions, no extra properties)."""
    schema = JudgmentBatchOut.model_json_schema()
    try:
        from anthropic.lib._parse._transform import transform_schema

        schema = transform_schema(schema)
    except Exception:  # noqa: BLE001 — an SDK without the helper still gets a usable schema
        pass
    return {"type": "json_schema", "schema": schema}


class LLM:
    """Anthropic client with cost accounting and a daily budget. Nothing else talks to the API."""

    def __init__(self, cfg: Config, secrets: Secrets, conn: sqlite3.Connection) -> None:
        if not secrets.is_set("anthropic_api_key"):
            raise TeacherError("ANTHROPIC_API_KEY not configured")
        import anthropic

        self.cfg = cfg
        # worker threads account for their calls here, under the lock, on a connection of their
        # own: sqlite3 shares prepared statements per connection, so two threads running the
        # same query on one connection can hand each other empty results
        self._lock = threading.Lock()
        self.conn = self._private_connection(cfg, conn)
        self._client = anthropic.Anthropic(api_key=secrets.value("anthropic_api_key"), max_retries=2, timeout=180.0)
        self._format = _output_format()
        # once a model has rejected structured outputs, every later call asks for plain JSON instead;
        # once the API has rejected the 1-hour cache, the default 5-minute cache is used instead
        with self._lock:
            self.plain_json = store.kv_get(self.conn, "plain_json") == "1"
            self.long_ttl_ok = store.kv_get(self.conn, "cache_ttl_fallback") != "1"

    @staticmethod
    def _private_connection(cfg: Config, shared: sqlite3.Connection) -> sqlite3.Connection:
        try:
            if cfg.db_path.exists():
                return store.connect(cfg.db_path)
        except sqlite3.Error:
            pass
        return shared

    # -- budget ---------------------------------------------------------------

    def spent_today(self) -> float:
        with self._lock:
            return store.spent_since(self.conn, store.local_midnight_iso(self.cfg.timezone))

    def budget_left(self) -> float:
        return self.cfg.models.daily_budget_usd - self.spent_today()

    def ensure_budget(self) -> None:
        if self.budget_left() <= 0:
            raise BudgetExceeded(
                f"daily model budget of ${self.cfg.models.daily_budget_usd:.2f} reached; scoring pauses until midnight"
            )

    # -- accounting -----------------------------------------------------------

    def cost(self, model: str, usage: Any, discount: float = 1.0) -> tuple[dict, float]:
        """(token counts, USD) for one response's usage, the batch discount applied if any."""
        p_in, p_out = self.cfg.models.price(model)
        creation = getattr(usage, "cache_creation", None)
        write_1h = int(getattr(creation, "ephemeral_1h_input_tokens", 0) or 0)
        write_5m = int(getattr(creation, "ephemeral_5m_input_tokens", 0) or 0)
        write_total = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        if write_1h + write_5m == 0 and write_total:
            write_5m = write_total if self.cfg.models.cache_ttl == "5m" else 0
            write_1h = write_total - write_5m
        u = {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "cache_write": write_1h + write_5m,
            "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        }
        p_read = self.cfg.models.cache_read_price(model)
        usd = (u["input_tokens"] * p_in + u["output_tokens"] * p_out
               + write_5m * p_in * 1.25 + write_1h * p_in * 2.0 + u["cache_read"] * p_read) / 1_000_000
        return u, usd * discount

    def record(self, run_id: int | None, model: str, purpose: str, usage: Any, discount: float = 1.0) -> float:
        u, usd = self.cost(model, usage, discount)
        with self._lock:
            store.record_cost(self.conn, run_id, model, purpose, u, usd)
        return usd

    def _enable_plain_json(self, model: str) -> None:
        if not self.plain_json:
            log.warning("teacher: %s rejected structured outputs; asking for plain JSON from now on", model)
            self.plain_json = True
            with self._lock:
                store.kv_set(self.conn, "plain_json", "1")

    def _disable_long_ttl(self) -> None:
        if self.long_ttl_ok:
            log.warning("teacher: the API rejected the 1-hour cache; using the 5-minute cache from now on")
            self.long_ttl_ok = False
            with self._lock:
                store.kv_set(self.conn, "cache_ttl_fallback", "1")

    def adapt_to_error(self, model: str, message: str) -> bool:
        """A rejected request shape is remembered so the next request is different.
        Returns True when something changed (so a retry makes sense)."""
        if not self.plain_json and _mentions_structured_outputs(message):
            self._enable_plain_json(model)
            return True
        if self.long_ttl_ok and _mentions_cache_ttl(message):
            self._disable_long_ttl()
            return True
        return False

    # -- request shapes -------------------------------------------------------

    def _system(self, system_text: str, ttl: str | None = None) -> list[dict]:
        cache: dict[str, Any] = {"type": "ephemeral"}
        if ttl == "1h" and self.long_ttl_ok:
            cache["ttl"] = "1h"
        return [{"type": "text", "text": system_text, "cache_control": cache}]

    def request_params(self, model: str, system_text: str, user_text: str, ttl: str | None = None) -> dict:
        """One judgment request, the same shape for a direct call and for a batch."""
        if self.plain_json:
            return {"model": model, "max_tokens": 8000, "system": self._system(system_text, ttl),
                    "messages": [{"role": "user", "content": user_text + PLAIN_JSON_INSTRUCTION}]}
        return {"model": model, "max_tokens": 8000, "system": self._system(system_text, ttl),
                "messages": [{"role": "user", "content": user_text}],
                "output_config": {"format": self._format}}

    @staticmethod
    def message_text(message: Any) -> str:
        return "".join(getattr(b, "text", "") for b in getattr(message, "content", []) or [])

    # -- synchronous ----------------------------------------------------------

    def judge(self, model: str, system_text: str, user_text: str, run_id: int | None,
              purpose: str) -> list[Verdict]:
        """One call, waited for. Returns validated verdicts; invalid ones are dropped."""
        import anthropic

        self.ensure_budget()
        try:
            response = self._client.messages.create(**self.request_params(model, system_text, user_text))
        except anthropic.BadRequestError as exc:
            if not self.adapt_to_error(model, str(exc)):
                raise _classify(exc) from exc
            try:
                response = self._client.messages.create(**self.request_params(model, system_text, user_text))
            except Exception as exc2:  # noqa: BLE001
                raise _classify(exc2) from exc2
        except Exception as exc:  # noqa: BLE001
            raise _classify(exc) from exc
        self.record(run_id, model, purpose, response.usage)
        verdicts = parse_judgments(self.message_text(response))
        if verdicts is None:
            raise NoJudgments(f"{model} returned no parsable judgments")
        return verdicts

    def complete(self, model: str, system_text: str, user_text: str, run_id: int | None,
                 purpose: str, max_tokens: int = 2000) -> str:
        """Plain text completion (the weekly learner rewrite)."""
        self.ensure_budget()
        try:
            response = self._client.messages.create(
                model=model, max_tokens=max_tokens, system=system_text,
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception as exc:  # noqa: BLE001
            raise _classify(exc) from exc
        self.record(run_id, model, purpose, response.usage)
        return self.message_text(response).strip()

    # -- batches --------------------------------------------------------------

    def submit_batch(self, requests: list[dict]) -> str:
        """requests: [{"custom_id": ..., "params": ...}]. Returns the batch id."""
        try:
            batch = self._client.messages.batches.create(requests=requests)
        except Exception as exc:  # noqa: BLE001
            raise _classify(exc) from exc
        return batch.id

    def batch_status(self, batch_id: str) -> str:
        """'in_progress', 'canceling' or 'ended'."""
        try:
            return self._client.messages.batches.retrieve(batch_id).processing_status
        except Exception as exc:  # noqa: BLE001
            raise _batch_classify(exc) from exc

    def batch_counts(self, batch_id: str) -> dict:
        """How many requests of a batch are processing, succeeded, errored, canceled, expired."""
        try:
            counts = self._client.messages.batches.retrieve(batch_id).request_counts
        except Exception as exc:  # noqa: BLE001
            raise _batch_classify(exc) from exc
        return {k: int(getattr(counts, k, 0) or 0) for k in ("processing", "succeeded", "errored", "canceled", "expired")}

    def retries(self, n: int) -> None:
        """More patience with 429s and 5xx, for a deliberate blast through a backlog."""
        self._client = self._client.with_options(max_retries=max(0, n))

    def batch_results(self, batch_id: str) -> list[Any]:
        try:
            return list(self._client.messages.batches.results(batch_id))
        except Exception as exc:  # noqa: BLE001
            raise _batch_classify(exc) from exc


# -- the teacher --------------------------------------------------------------

def _fmt_date(published: int | None) -> str:
    if not published:
        return "unknown date"
    try:
        return datetime.fromtimestamp(int(published), tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError, TypeError):
        return "unknown date"  # a feed's bad timestamp must not fail its batch every run for two weeks


def article_block(n: int, row: sqlite3.Row | dict, chars: int) -> str:
    text = (row["text"] or "").strip()
    snippet = text[:chars] + (" […]" if len(text) > chars else "")
    host = urlsplit(row["url"] or "").netloc.lower().removeprefix("www.") if row["url"] else ""
    return (
        f"[{n}]\n"
        f"feed: {row['feed_title'] or '?'} · folder: {row['folder'] or 'none'}\n"
        f"title: {row['title'] or '(untitled)'}\n"
        f"author: {row['author'] or 'unknown'} · published: {_fmt_date(row['published'])} · site: {host}\n"
        f"text: {snippet or '(no text in the feed)'}\n"
    )


def has_more_to_read(row: sqlite3.Row | dict, cfg: Config) -> bool:
    """A second look is worth paying for only when the full text holds more than the
    opening the first pass already saw."""
    length = row["text_len"] if "text_len" in row.keys() else len(row["text"] or "")  # type: ignore[union-attr]
    return int(length or 0) > cfg.scoring.opening_chars + 200


class Teacher:
    def __init__(self, cfg: Config, llm: LLM, conn: sqlite3.Connection, context: str = "",
                 expansion_topics: list[str] | None = None, learner_text: str | None = None) -> None:
        """``learner_text`` stands in for profile/learner.md (the weekly rewrite's gate judges
        with a proposed model without writing it to disk)."""
        self.cfg = cfg
        self.llm = llm
        self.conn = conn
        self.prompt_version = cfg.scoring.prompt_version
        self.expansion_topics = expansion_topics or []
        base = fill_prompt(load_prompt(self.prompt_version, cfg.root), cfg)
        intro, _, rubric = base.partition("\n\n")
        cfg.check_seed()   # the backstop; run and blast refuse earlier, before any network
        seed = cfg.read_seed() or "(no seed written yet)"
        learner = (cfg.read_learner() if learner_text is None else learner_text) or "(no learner model yet)"
        # the cached prefix changes only when the seed or the learner model does; the concept
        # lists change with every star and read, so they travel in the user message instead
        student = f"# Seed\n{seed}\n\n# Learner model\n{learner}"
        self.system_text = f"{intro}\n\n{rubric}\n\n{student}"
        self.second_look_text = f"{load_prompt('second_look.v1', cfg.root)}\n\n{rubric}\n\n{student}"
        self.context = context or "(no reading history yet)"
        self.batch_mode = cfg.models.batch  # a run may switch this off (`viveka run --sync`)
        self.rejudge = self.rejudge_rule(cfg)  # None when a stand-in model must not touch the teacher's work
        self.stale_tags: dict[str, list[str]] = {}  # bucket tag -> articles a re-score moved out of it

    def _user_text(self, rows: list, chars: int) -> str:
        blocks = "\n".join(article_block(i, r, chars) for i, r in enumerate(rows, 1))
        return f"# Concepts\n{self.context}\n\n# Articles\n{blocks}"

    # -- what is pending --------------------------------------------------------

    def pending_rows(self, limit: int | None = None, exclude_inflight: bool = False) -> tuple[list[sqlite3.Row], int]:
        """Unread articles still to judge, newest first, with duplicates of earlier stories
        recorded on the spot (no model call). Returns (rows to judge, duplicates recorded)."""
        rows = store.unscored_unread(self.conn, self.prompt_version, self.cfg.scoring.max_age_days,
                                     self.cfg.cut.unscored_folders(), exclude_inflight=exclude_inflight,
                                     limit=limit, rejudge=self.rejudge)
        todo: list[sqlite3.Row] = []
        dupes = 0
        index = DupIndex(self.conn)
        for row in rows:
            dup = index.find(row)
            if dup is not None:
                self._record_duplicate(row, dup)
                dupes += 1
            else:
                todo.append(row)
        return todo, dupes

    def pending_count(self, exclude_inflight: bool = False) -> int:
        return store.unscored_unread_count(self.conn, self.prompt_version, self.cfg.scoring.max_age_days,
                                           self.cfg.cut.unscored_folders(), exclude_inflight=exclude_inflight,
                                           rejudge=self.rejudge)

    @staticmethod
    def rejudge_rule(cfg: Config) -> tuple[str, float] | None:
        """(teacher model, min score): what another model judged at or above the score is
        judged again by the teacher, so the keepers rest on the teacher's opinion."""
        if cfg.scoring.rejudge_min_score is None:
            return None
        return cfg.models.teacher, float(cfg.scoring.rejudge_min_score)

    # -- first pass, synchronous ----------------------------------------------

    def judge_batch(self, rows: list[sqlite3.Row], run_id: int | None) -> dict[str, Verdict]:
        ordered = [r["id"] for r in rows]
        user_text = self._user_text(rows, self.cfg.scoring.opening_chars)
        verdicts = self.llm.judge(self.cfg.models.teacher, self.system_text, user_text, run_id, "teacher")
        return self._by_article(verdicts, ordered)

    @staticmethod
    def _by_article(verdicts: list[Verdict], ordered: list[str]) -> dict[str, Verdict]:
        """Verdicts keyed by article id, in batch order. A duplicate may point only at an
        earlier article of the batch that is not itself a duplicate: a forward pointer or a
        mutual pair would cap both copies of a good story at 20, and the later copy then has
        no original to be a duplicate of. Otherwise the pointer and the flag are dropped."""
        out: dict[str, Verdict] = {}
        dupes: set[str] = set()
        for v in sorted(verdicts, key=lambda v: v.n):
            aid = resolve_ordinal(v.n, ordered)
            if aid is None or aid in out:
                continue
            if v.dupe_of is not None or "duplicate" in v.flags:
                target = resolve_ordinal(v.dupe_of, ordered) if v.dupe_of is not None else None
                earlier = target is not None and target != aid and ordered.index(target) < ordered.index(aid)
                if earlier and target not in dupes:
                    v = v.model_copy(update={"dupe_of": target})
                    dupes.add(aid)
                else:
                    v = v.model_copy(update={"dupe_of": None, "flags": [f for f in v.flags if f != "duplicate"]})
            out[aid] = v
        return out

    def score_pending(self, run_id: int | None, limit: int | None = None, workers: int = 3,
                      on_progress: Any = None, stop_after: float | None = None) -> dict:
        """Score every pending article now, waiting for each call. Resumable: anything not
        scored stays pending for the next run. ``on_progress(stats)`` is called after every
        batch that lands; past ``stop_after`` (a time.monotonic() value) no new call starts.
        Returns stats."""
        stats = {"scored": 0, "dupes": 0, "failed_batches": 0, "empty_batches": 0, "budget_stop": False,
                 "pending_after": 0, "total": 0, "stopped_early": False}
        todo, stats["dupes"] = self.pending_rows(limit, exclude_inflight=True)  # a batch may hold some
        stats["total"] = len(todo)
        size = self.cfg.scoring.batch_size
        batches = [todo[i: i + size] for i in range(0, len(todo), size)]
        workers = max(1, workers)
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {}
            queue = list(enumerate(batches))
            running: set = set()

            def feed() -> None:
                # at most one call per worker in flight and nothing queued behind it, so the
                # deadline and the budget stop new calls at once, not after a backlog drains
                while queue and len(running) < workers:
                    if self.llm.budget_left() <= 0:
                        stats["budget_stop"] = True
                        queue.clear()
                        return
                    if stop_after is not None and time.monotonic() > stop_after:
                        stats["stopped_early"] = True
                        queue.clear()
                        return
                    bi, batch = queue.pop(0)
                    fut = pool.submit(self.judge_batch, batch, run_id)
                    futures[fut] = bi
                    running.add(fut)

            feed()
            while running:
                done = next(as_completed(list(running)))
                running.discard(done)
                self._absorb_future(done, batches[futures[done]], stats, on_progress, len(todo))
                feed()  # after the absorb: a rejection or an interrupt must not let one more call out
        finally:
            # a rejected key or model id, a timeout, Ctrl-C: whatever is queued is never sent
            pool.shutdown(wait=True, cancel_futures=True)
        stats["pending_after"] = self.pending_count(exclude_inflight=True)
        return stats

    def _absorb_future(self, future: Any, batch: list, stats: dict, on_progress: Any, total: int) -> None:
        try:
            verdicts = future.result()
        except BudgetExceeded:
            stats["budget_stop"] = True
            return
        except ModelConfigError:
            raise
        except NoJudgments as exc:  # the model answered with nothing: counted against the articles
            stats["failed_batches"] += 1
            stats["empty_batches"] += 1
            store.bump_judge_failures(self.conn, [r["id"] for r in batch])
            log.warning("teacher: batch of %d: %s; they stay pending", len(batch), exc)
            return
        except Exception:  # noqa: BLE001 — one bad batch, not the run
            stats["failed_batches"] += 1
            log.exception("teacher: batch of %d failed; they stay pending", len(batch))
            return
        by_id = {r["id"]: r for r in batch}
        for aid, v in verdicts.items():
            self._record(by_id[aid], v)
            stats["scored"] += 1
        if not verdicts and batch:
            stats["empty_batches"] += 1
            store.bump_judge_failures(self.conn, [r["id"] for r in batch])
            log.warning("teacher: a batch of %d came back with no judgment", len(batch))
        log.info("teacher: judged %d of %d", stats["scored"], total)
        if on_progress is not None:
            on_progress(stats)

    # -- first pass, batched --------------------------------------------------

    def submit_pending(self, run_id: int | None, limit: int | None = None) -> dict:
        """Send what is pending to the Batches API. A batch smaller than one call's worth
        waits for more articles unless its oldest has waited an hour. Returns stats."""
        stats = {"submitted": 0, "requests": 0, "dupes": 0, "budget_stop": False, "batch_id": None, "waiting": 0}
        limit = limit or self.cfg.scoring.batch_max_per_run
        todo, stats["dupes"] = self.pending_rows(limit, exclude_inflight=True)
        if not todo:
            return stats
        size = self.cfg.scoring.batch_size
        if len(todo) < size and not self._waited_long_enough(todo):
            stats["waiting"] = len(todo)
            return stats
        est = max(self.cfg.models.est_usd_per_article, 1e-6)
        allowed = int(max(0.0, self.llm.budget_left()) / est)
        if allowed < len(todo):
            stats["budget_stop"] = True
            todo = todo[:allowed]
            if not todo:
                return stats
        model = self.cfg.models.teacher
        ttl = self.cfg.models.cache_ttl
        requests, members = [], []
        for bi in range(0, len(todo), size):
            batch = todo[bi: bi + size]
            custom_id = f"t{run_id or 0}-{bi // size}"
            params = self.llm.request_params(model, self.system_text, self._user_text(batch, self.cfg.scoring.opening_chars), ttl)
            requests.append({"custom_id": custom_id, "params": params})
            members.append((custom_id, [r["id"] for r in batch]))
        batch_id = self.llm.submit_batch(requests)
        store.add_batch(self.conn, batch_id, run_id, "teacher", model, members)
        stats.update(submitted=len(todo), requests=len(requests), batch_id=batch_id)
        log.info("teacher: submitted batch %s: %d articles in %d requests", batch_id, len(todo), len(requests))
        return stats

    @staticmethod
    def _waited_long_enough(rows: list[sqlite3.Row]) -> bool:
        oldest = min((r["fetched_at"] or "") for r in rows)
        if not oldest:
            return True
        try:
            seen = datetime.fromisoformat(oldest)
        except ValueError:
            return True
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        return store.utcnow() - seen >= timedelta(minutes=BATCH_WAIT_MINUTES)

    def submit_second_looks(self, run_id: int | None, rows: list[sqlite3.Row]) -> int:
        """Close calls go to the stronger model with the full text, one request each."""
        rows = [r for r in rows if r["id"] not in store.inflight_ids(self.conn, "second_look")]
        if not rows:
            return 0
        model = self.cfg.models.second_look
        requests, members = [], []
        for n, row in enumerate(rows):
            full = store.article(self.conn, row["id"])
            if full is None:
                continue
            custom_id = f"s{run_id or 0}-{n}"
            user_text = self._user_text([dict(full)], self.cfg.scoring.second_look_chars)
            params = self.llm.request_params(model, self.second_look_text, user_text, self.cfg.models.cache_ttl)
            requests.append({"custom_id": custom_id, "params": params})
            members.append((custom_id, [row["id"]]))
        if not requests:
            return 0
        batch_id = self.llm.submit_batch(requests)
        store.add_batch(self.conn, batch_id, run_id, "second_look", model, members)
        log.info("teacher: submitted %d second looks as batch %s", len(requests), batch_id)
        return len(requests)

    def collect(self, run_id: int | None, client: Any = None, on_progress: Any = None) -> dict:
        """Record the judgments of every finished batch. Articles whose request failed go
        back to pending (first pass) or count as examined and stay unread (second look)."""
        stats = {"scored": 0, "second_looks": 0, "saved": 0, "failed": 0, "empty": 0, "open": 0, "notes": []}
        for b in store.open_batches(self.conn):
            try:
                status = self.llm.batch_status(b["id"])
            except BatchNotFound as exc:
                self._give_up(b, f"vanished from the API ({exc})", stats)
                continue
            except ModelConfigError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("teacher: could not poll batch %s", b["id"])
                stats["open"] += 1
                continue
            if status != "ended":
                if self._lost(b):
                    self._give_up(b, f"still {status} after {BATCH_LOST_HOURS}h", stats)
                else:
                    stats["open"] += 1
                continue
            try:
                results = self.llm.batch_results(b["id"])
            except BatchNotFound as exc:
                self._give_up(b, f"vanished from the API ({exc})", stats)
                continue
            except ModelConfigError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("teacher: could not fetch results of batch %s", b["id"])
                if self._lost(b):  # ended, but its results will not come: the same deadline applies
                    self._give_up(b, f"ended but its results could not be fetched for {BATCH_LOST_HOURS}h", stats)
                else:
                    stats["open"] += 1
                continue
            self._absorb(b, results, run_id, client, stats)
            if on_progress is not None:
                on_progress(stats)
        return stats

    def _give_up(self, b: sqlite3.Row, why: str, stats: dict) -> None:
        """Close a batch whose results will never come; its articles are pending again (first
        pass) or count as examined and stay unread (second looks)."""
        if b["purpose"] == "second_look":
            for ids in store.batch_members(self.conn, b["id"]).values():
                store.record_second_look_failure(self.conn, ids[0])
                stats["failed"] += 1
        store.close_batch(self.conn, b["id"], "lost", why)
        log.warning("teacher: batch %s given up on: %s", b["id"], why)
        stats["notes"].append(f"batch {b['id'][:12]} given up on ({why}); its articles are pending again")

    @staticmethod
    def _lost(b: sqlite3.Row) -> bool:
        try:
            created = datetime.fromisoformat(b["created_at"])
        except (TypeError, ValueError):
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return store.utcnow() - created > timedelta(hours=BATCH_LOST_HOURS)

    def _absorb(self, b: sqlite3.Row, results: list[Any], run_id: int | None, client: Any, stats: dict) -> None:
        """Record one finished batch: judgments, second looks, costs and the batch's closing in
        one transaction, so an interrupt leaves it open and untouched for the next run to
        absorb again rather than half-recorded with its costs counted twice."""
        with store.transaction(self.conn):
            model, first_error = self._absorb_results(b, results, run_id, client, stats)
        if first_error:
            # after the commit: the LLM remembers a rejected request shape on its own connection,
            # and that write would wait on this one's open transaction until it timed out
            self.llm.adapt_to_error(model, first_error)

    def _absorb_results(self, b: sqlite3.Row, results: list[Any], run_id: int | None, client: Any,
                        stats: dict) -> tuple[str, str]:
        """Returns (model, first error message) for the caller to act on once the transaction is over."""
        members = store.batch_members(self.conn, b["id"])
        purpose, model = b["purpose"], b["model"]
        errored, empty, first_error, answered = 0, 0, "", set()
        for res in results:
            ids = members.get(res.custom_id)
            if not ids or res.custom_id in answered:  # unknown, or listed twice: once is enough
                continue
            answered.add(res.custom_id)
            result = res.result
            if getattr(result, "type", "") != "succeeded":
                errored += 1
                err = getattr(getattr(result, "error", None), "error", None)
                first_error = first_error or f"{getattr(result, 'type', '?')}: {getattr(err, 'message', '') or ''}"
                if purpose == "second_look":
                    store.record_second_look_failure(self.conn, ids[0])
                    stats["failed"] += 1
                continue
            message = result.message
            usage, usd = self.llm.cost(model, message.usage, self.cfg.models.batch_discount)
            store.record_cost(self.conn, run_id, model, purpose, usage, usd)
            verdicts = parse_judgments(self.llm.message_text(message))
            if purpose == "teacher":
                done = self._absorb_teacher(ids, verdicts or [])
                stats["scored"] += done
                if not done:
                    empty += 1
                    store.bump_judge_failures(self.conn, ids)
            else:
                self._absorb_second_look(ids[0], verdicts, client, stats)
        for custom_id, ids in members.items():
            if custom_id not in answered and purpose == "second_look":
                store.record_second_look_failure(self.conn, ids[0])
                stats["failed"] += 1
        notes = []
        if errored:
            notes.append(f"{errored} of {b['n_requests']} requests failed: {first_error}")
        if empty:
            stats["empty"] += empty
            notes.append(f"{empty} of {b['n_requests']} requests came back with no judgment "
                         f"(an article is left alone after {store.MAX_JUDGE_FAILURES}; `viveka safety` shows how many)")
        note = "; ".join(notes)
        store.close_batch(self.conn, b["id"], "partial" if errored or empty else "done", note)
        if note:
            log.warning("teacher: batch %s: %s", b["id"], note)
            stats["notes"].append(f"batch {b['id'][:12]}: {note}")
        return model, first_error if errored else ""

    def _absorb_teacher(self, ids: list[str], verdicts: list[Verdict]) -> int:
        by_id = {r["id"]: r for r in self.conn.execute(
            f"SELECT * FROM articles WHERE id IN ({','.join('?' * len(ids))})", ids)}
        done = 0
        for aid, v in self._by_article(verdicts, ids).items():
            row = by_id.get(aid)
            if row is None:
                continue
            self._record(row, v)
            done += 1
        return done

    def _absorb_second_look(self, aid: str, verdicts: list[Verdict] | None, client: Any, stats: dict) -> None:
        current = self.conn.execute(
            "SELECT s.score, a.folder FROM scores s JOIN articles a ON a.id = s.article_id WHERE s.article_id = ?",
            (aid,)).fetchone()
        if current is None or not verdicts:
            store.record_second_look_failure(self.conn, aid)
            stats["failed"] += 1
            return
        second = compose(verdicts[0].model_copy(update={"dupe_of": None}), self.cfg, self.expansion_topics)
        stats["second_looks"] += 1
        if self.apply_second_look(aid, float(current["score"]), second, current["folder"], client):
            stats["saved"] += 1

    def apply_second_look(self, aid: str, first: float, second: float, folder: str | None, client: Any = None) -> bool:
        """Keep the higher score, retag the bucket if it moved. True when the article now
        clears its floor."""
        new_score = max(first, second)
        new_bucket = self.cfg.tags.bucket_for(new_score)
        old_bucket = self.cfg.tags.bucket_for(first)
        store.record_second_look(self.conn, aid, second, self.cfg.models.second_look, new_score, new_bucket)
        if new_bucket != old_bucket and client is not None:
            try:
                client.untag([aid], old_bucket)
            except Exception:  # noqa: BLE001 — a stale bucket tag is cosmetic
                log.warning("cut: could not remove old bucket tag %s from %s", old_bucket, aid)
        floor = self.cfg.cut.floor_for(folder)
        return floor is None or new_score >= floor

    # -- recording --------------------------------------------------------------

    def _record(self, row: sqlite3.Row, v: Verdict) -> None:
        d = v.model_dump()
        d["concept"] = (d.get("concept") or "").strip().lower()[:60]
        d["lesson"] = (d.get("lesson") or "").strip()[:200]
        d["dupe_of"] = v.dupe_of if isinstance(v.dupe_of, str) else None
        score = compose(d, self.cfg, self.expansion_topics)
        stale = store.record_score(self.conn, row["id"], d, base_score=score, score=score,
                                   bucket=self.cfg.tags.bucket_for(score), prompt_version=self.prompt_version,
                                   model=self.cfg.models.teacher)
        if stale:
            self.stale_tags.setdefault(stale, []).append(row["id"])

    def _record_duplicate(self, row: sqlite3.Row, original: dict) -> None:
        d = {"quality": 20, "learning": 10, "fit": 20, "breadth": 10, "relevance": 20,
             "lesson": f"Same story as: {original['title'][:80]}", "concept": original["concept"] or "duplicate",
             "kind": "other", "flags": ["duplicate"], "dupe_of": original["id"], "confidence": "high"}
        stale = store.record_score(self.conn, row["id"], d, base_score=20.0, score=20.0,
                                   bucket=self.cfg.tags.bucket_for(20.0), prompt_version=self.prompt_version,
                                   model="dedup")
        if stale:
            self.stale_tags.setdefault(stale, []).append(row["id"])

    # -- second look, synchronous -------------------------------------------------

    def second_looks(self, rows: list[sqlite3.Row], run_id: int | None, client: Any = None,
                     workers: int | None = None, stop_after: float | None = None,
                     max_looks: int | None = None) -> dict[str, bool | None]:
        """Several second looks at once, each recorded the moment it lands (a look that was
        paid for is never lost to a timeout or a Ctrl-C later in the run). ``rows`` are
        floor candidates with their score and folder. Returns {article id: True when the
        article now clears its floor, False when the cut stands, None when the look failed
        (recorded as failed; the article stays unread)}. An article missing from the result
        was never examined (the budget, the deadline, or the per-run cap): it stays unread."""
        results: dict[str, bool | None] = {}
        cap = self.cfg.scoring.second_look_max_per_run if max_looks is None else max_looks
        todo = [r for r in rows[:cap] if store.article(self.conn, r["id"]) is not None]
        if len(rows) > cap:
            log.info("teacher: %d close calls; looking at %d this run", len(rows), cap)
        workers = max(1, workers or self.cfg.scoring.workers)
        pool = ThreadPoolExecutor(max_workers=workers)
        budget_hit = False
        try:
            futures: dict[Any, sqlite3.Row] = {}
            queue = list(todo)
            running: set = set()

            def feed() -> None:
                nonlocal budget_hit
                while queue and len(running) < workers:
                    if self.llm.budget_left() <= 0:
                        budget_hit = True
                        queue.clear()
                        return
                    if stop_after is not None and time.monotonic() > stop_after:
                        queue.clear()
                        return
                    row = queue.pop(0)
                    full = store.article(self.conn, row["id"])
                    if full is None:
                        continue
                    fut = pool.submit(self.second_look, full, run_id)
                    futures[fut] = row
                    running.add(fut)

            feed()
            while running:
                done = next(as_completed(list(running)))
                running.discard(done)
                row = futures[done]
                aid = row["id"]
                try:
                    second = done.result()
                except BudgetExceeded:
                    budget_hit = True
                    second = None
                    skip = True
                except ModelConfigError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("teacher: second look failed for %s", aid)
                    second, skip = None, False
                else:
                    skip = False
                if skip:
                    pass  # unexamined: the budget ran out while it waited
                elif second is None:
                    store.record_second_look_failure(self.conn, aid)
                    results[aid] = None
                else:
                    results[aid] = self.apply_second_look(aid, float(row["score"]), float(second), row["folder"], client)
                feed()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        if budget_hit:
            log.warning("teacher: budget reached during second looks; unexamined close calls stay unread")
        return results

    def second_look(self, row: sqlite3.Row, run_id: int | None) -> float | None:
        """Re-read one article with the full text on the stronger model, now. Returns the
        composed score of that judgment, or None if it failed. The caller keeps the max."""
        full = dict(row)
        user_text = self._user_text([full], self.cfg.scoring.second_look_chars)
        try:
            verdicts = self.llm.judge(self.cfg.models.second_look, self.second_look_text, user_text, run_id,
                                      "second_look")
        except (BudgetExceeded, ModelConfigError):
            raise
        except Exception:  # noqa: BLE001
            log.exception("teacher: second look failed for %s", row["id"])
            return None
        if not verdicts:
            return None
        v = verdicts[0].model_copy(update={"dupe_of": None})
        return compose(v, self.cfg, self.expansion_topics)


def concept_context(conn: sqlite3.Connection, cfg: Config) -> str:
    """The lists for the teacher: what the student loved, read, skipped, and where the level
    missed, by concept, from stars, reads and the student's verdict tags."""
    days = cfg.feedback.concept_window_days
    since = store.days_ago_iso(days)
    loved = store.concept_counts(conn, store.LOVED_KINDS, since)
    read = store.concept_counts(conn, ["good", "read", "rescue"], since)
    neg = store.concept_counts(conn, store.NEGATIVE_KINDS, since)
    basic = store.concept_counts(conn, ["basic"], since, limit=20)
    heavy = store.concept_counts(conn, ["heavy"], since, limit=20)
    fmt = lambda pairs: ", ".join(f"{c}: {n}" for c, n in pairs)  # noqa: E731
    lines = []
    if loved:
        lines.append(f"Loved (starred or marked great) in the last {days} days (concept: count): {fmt(loved)}")
    if read:
        lines.append(f"Read, marked good or rescued in the last {days} days: {fmt(read)}")
    if not loved and not read:
        lines.append(f"Read in the last {days} days: nothing recorded yet.")
    if neg:
        lines.append(f"Marked bad or meh, or dismissed: {fmt(neg)}")
    if basic:
        lines.append(f"Too basic for the student (score fit low for introductions to these): {fmt(basic)}")
    if heavy:
        lines.append(f"Too dense for the student (prefer gentler entries to these): {fmt(heavy)}")
    return "\n".join(lines)
