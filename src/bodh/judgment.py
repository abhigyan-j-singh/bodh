"""The judgment: the shape the judge writes for one article, read leniently and composed
into one score. The codes the model writes and their long names, the parser, `compose`
(sub-scores, caps and bonuses to a number from 0 to 100) and the duplicate index."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, ValidationError

from . import store
from .config import Config

log = logging.getLogger(__name__)

# the compact codes the model writes (one token each) and what they mean in the database
KIND_CODES = {
    "src": "primary_source",
    "analysis": "original_analysis",
    "news": "reporting",
    "opinion": "commentary",
    "roundup": "aggregation",
    "promo": "promo",
    "other": "other",
}
FLAG_CODES = {
    "primary": "primary_source",
    "home": "home_angle",
    "vendor": "vendor_marketing_no_data",
    "funding": "funding_offfield",
    "perishable": "perishable",
    "stub": "paywalled_stub",
    "dupe": "duplicate",
    "india": "home_angle",
}  # the code the prompt used before home_angle was configurable
KINDS = list(dict.fromkeys(KIND_CODES.values()))  # the long names, in the codes' order
FLAGS = [name for code, name in FLAG_CODES.items() if code != "india"]
# flag names written by earlier versions, read as today's
FLAG_ALIASES = {"india_angle": "home_angle", "funding_round_non_infra": "funding_offfield"}
SUBSCORES = ("quality", "learning", "fit", "breadth", "relevance")


def normalize_flags(flags: list[str] | None) -> list[str]:
    """Flag names as the code knows them today, whatever version wrote them."""
    return [FLAG_ALIASES.get(f, f) for f in (flags or [])]


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
    s: list[int] = Field(
        description="five integers 0-100 in this order: quality, learning, fit, breadth, relevance"
    )
    lesson: str = Field(
        description="at most 12 words: what the student takes from it, or what the piece is instead"
    )
    concept: str = Field(description="one to three lowercase words: the concept or field taught")
    kind: KindCode = Field(description="src, analysis, news, opinion, roundup, promo or other")
    flags: list[FlagCode] = Field(
        description="any of primary, home, vendor, funding, perishable, stub, dupe; usually empty"
    )
    dupe: int | None = Field(
        description="the number of an earlier article in this batch telling the same story, else null"
    )
    conf: Literal["high", "medium", "low"] = Field(description="low when the text is too thin to judge")


class JudgmentBatchOut(BaseModel):
    judgments: list[JudgmentOut]


class JudgmentIn(BaseModel):
    """What comes back, read leniently: the API does not promise the case of enum values,
    and structured outputs cannot enforce bounds or lengths, so everything is normalised here."""

    n: int
    s: list[int]
    lesson: str | None = ""  # None in any of these is read as "not given", never as a dropped judgment
    concept: str | None = ""
    kind: str | None = "other"
    flags: list[str | None] | None = None
    dupe: int | str | None = None
    conf: str | None = "medium"

    def to_verdict(self) -> Verdict:
        if len(self.s) != len(SUBSCORES):
            raise ValueError(f"expected {len(SUBSCORES)} sub-scores, got {len(self.s)}")
        scores = {name: max(0, min(100, int(val))) for name, val in zip(SUBSCORES, self.s, strict=True)}
        flags = [
            FLAG_CODES[f] for f in ((x or "").strip().lower() for x in self.flags or []) if f in FLAG_CODES
        ]
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
            n=self.n,
            **scores,
            lesson=(self.lesson or "").strip(),
            concept=(self.concept or "").strip().lower(),
            kind=KIND_CODES.get((self.kind or "").strip().lower(), "other"),
            flags=flags,
            dupe_of=dupe,
            confidence=conf if conf in ("high", "medium", "low") else "medium",  # type: ignore[arg-type]
        )


PLAIN_JSON_INSTRUCTION = (
    "\n\nRespond with only a JSON object, no prose and no code fence, of the form "
    '{"judgments": [{"n": 1, "s": [0, 0, 0, 0, 0], "lesson": "", "concept": "", "kind": "other", '
    '"flags": [], "dupe": null, "conf": "medium"}]}'
)


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
        raw = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    out: list[Verdict] = []
    for item in (raw.get("judgments") if isinstance(raw, dict) else None) or []:
        try:
            out.append(JudgmentIn(**item).to_verdict())
        except (ValidationError, ValueError, TypeError) as exc:
            log.warning("judge: dropping malformed judgment %r (%s)", item, exc)
    return out


# -- composition --------------------------------------------------------------


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2}


def matches_topic(concept: str, topics: list[str]) -> bool:
    c = (concept or "").strip().lower()
    if not c:
        return False
    cw = _words(c)
    for raw in topics:
        topic = raw.strip().lower()
        if not topic:
            continue
        if topic in c or c in topic:
            return True
        tw = _words(topic)
        if cw and tw and len(cw & tw) / len(cw | tw) >= 0.5:
            return True
    return False


PROMO_CAP = 30  # vendor marketing without data, or a promo: never above this
FUNDING_CAP = 40  # a funding round outside the student's own field
DUPLICATE_CAP = 20  # a second telling of a story already in the list


def compose(v: Verdict | dict[str, Any], cfg: Config, expansion_topics: list[str] | None = None) -> float:
    """Sub-scores to one score, deterministically, so weights can change without re-prompting:
    the weighted sum, then the bonuses (the home angle, this week's expansion topics), then the
    caps. The caps come last on purpose (decided October 2026): a bonus may lift a good article,
    never a promo, an off-field funding round or a duplicate over the cut-off."""
    d = v.model_dump() if isinstance(v, Verdict) else dict(v)
    w = cfg.judge.weights
    s = sum(w[k] * float(d.get(k, 0)) for k in w)
    flags = set(normalize_flags(d.get("flags")))
    if "home_angle" in flags and cfg.judge.home_angle:
        s += cfg.judge.home_bonus
    if expansion_topics and matches_topic(d.get("concept", ""), expansion_topics):
        s += cfg.judge.expansion_bonus
    if "vendor_marketing_no_data" in flags or d.get("kind") == "promo":
        s = min(s, PROMO_CAP)
    if "funding_offfield" in flags:
        s = min(s, FUNDING_CAP)
    if d.get("dupe_of") or "duplicate" in flags:
        s = min(s, DUPLICATE_CAP)
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
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    )
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", host, path, query, ""))


def normalized_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (title or "").lower()).strip()


class DupIndex:
    """Earlier articles telling the same story: the same canonical URL, or the same
    normalised title in a different feed, within the window. Built once per scoring pass."""

    def __init__(self, conn: sqlite3.Connection, window_days: int = 7) -> None:
        since = store.days_ago_iso(window_days)
        self.by_url: dict[str, dict[str, Any]] = {}
        self.by_title: dict[str, dict[str, Any]] = {}
        for cand in conn.execute(
            """SELECT a.id, a.url, a.title, a.feed_title, s.concept
               FROM articles a JOIN scores s ON s.article_id = a.id
               WHERE a.fetched_at >= ? AND (s.dupe_of IS NULL OR s.dupe_of = '')""",
            (since,),
        ):
            self.add(dict(cand))

    def add(self, cand: dict[str, Any]) -> None:
        curl = canonical_url(cand.get("url") or "")
        if curl:
            self.by_url.setdefault(curl, cand)
        ntitle = normalized_title(cand.get("title") or "")
        if len(ntitle) > 25:
            self.by_title.setdefault(ntitle, cand)

    def find(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any] | None:
        curl = canonical_url(row["url"] or "")
        hit = self.by_url.get(curl) if curl else None
        if hit and hit["id"] != row["id"]:
            return hit
        ntitle = normalized_title(row["title"] or "")
        hit = self.by_title.get(ntitle) if len(ntitle) > 25 else None
        if hit and hit["id"] != row["id"] and hit.get("feed_title") != row["feed_title"]:
            return hit
        return None
