"""The one document the dashboard renders from: `build(cfg, conn, mode)` returns a
`DashboardDocument` with every number the page shows and nothing it does not, the sentences
composed here (the state line, the attention reason, the leads, the empty states) so the page
never invents words. The only module of `web/` that touches SQL; one function per panel; a
sixty-second cache in front (`Cache`). `GET /api/dashboard`, the static export and the Mac
app read the same object; its JSON schema (`docs/json.md`) is generated from the model."""

from __future__ import annotations

import difflib
import logging
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from .. import __version__, locks, store
from ..config import Config
from . import tokens

log = logging.getLogger(__name__)

SCHEMA = 1
LIST_BAND = 70
LIST_CAP = 50
FEEDS_CAP = 40
RUNS_CAP = 20
CONCEPTS_CAP = 5
LINES_PER_CONCEPT = 5
CLOUD_CAP = 30
DOCS_URL = "https://github.com/abhigyan-j-singh/bodh/blob/main/RUNBOOK.md"
REPO_URL = "https://github.com/abhigyan-j-singh/bodh"
GRADES = ("great", "good", "meh", "bad")


class Strict(BaseModel):
    model_config = {"extra": "forbid"}


# -- the model -----------------------------------------------------------------------------


class Attention(Strict):
    key: str
    level: str  # warning | critical | good
    word: str
    text: str
    link_text: str | None = None
    link_url: str | None = None
    dismissable: bool = False


class State(Strict):
    running: bool
    kind: str | None = None  # run, prune, catch-up, ... while running
    phase: str = ""
    sentence: str
    progress_done: int | None = None
    progress_total: int | None = None
    rate_per_min: float | None = None
    eta_min: float | None = None
    mode: str  # scoring-only | autocut
    mode_label: str
    next_run_in_s: float | None = None
    attention: Attention | None = None
    levers: list[str] = Field(default_factory=list)  # run, stop, pause, resume


class Tile(Strict):
    key: str
    label: str
    value: float
    text: str
    delta: float | None = None
    delta_text: str = ""
    direction: str = ""  # up-good, down-good, up-bad, down-bad, or empty
    series: list[float] = Field(default_factory=list)  # seven days, oldest first, today last
    series_label: str = ""


class Today(Strict):
    unread: int
    target: int
    hero_text: str
    lead: str
    tiles: list[Tile]


class Article(Strict):
    id: str
    title: str
    url: str
    reader_url: str | None = None
    score: float
    band: int
    lesson: str
    concept: str
    feed: str
    folder: str | None = None
    age_text: str
    arrived_at: str | None = None  # when BazQux fetched it: the "newest" sort
    judged_at: str | None = None


class ReadingList(Strict):
    lead: str
    band: int
    bands: list[int]
    folders: list[str]
    articles: list[Article]
    total: int  # at the band and above, before the cap
    pending: int
    empty_text: str
    verdicts: list[str]  # the verdict menu's words, console and app only


class GridRow(Strict):
    band: int
    label: str
    counts: dict[str, int]


class Disagreement(Strict):
    id: str
    title: str
    url: str
    grade: str
    score: float
    band: int
    concept: str


class CalibrationRow(Strict):
    band: int
    label: str
    n: int
    read_share: float | None
    star_share: float | None


class NearMiss(Strict):
    id: str
    title: str
    url: str
    score: float
    band: int
    lesson: str
    concept: str
    cut_mode: str


class Verdicts(Strict):
    lead: str
    grid: list[GridRow]
    grid_sentence: str
    disagreements: list[Disagreement]
    disagreements_empty: str
    calibration: list[CalibrationRow]
    calibration_sentence: str
    calibration_empty: str
    near_misses: list[NearMiss]
    near_misses_empty: str


class Learned(Strict):
    title: str
    url: str
    lesson: str


class Concept(Strict):
    concept: str
    count: int
    items: list[Learned]
    more: int


class CloudItem(Strict):
    concept: str
    count: int
    frontier: bool


class DiffLine(Strict):
    kind: str  # add | del | ctx
    text: str


class Profile(Strict):
    version: int | None
    date_text: str
    text: str
    diff: list[DiffLine]
    diff_empty: str


class Learning(Strict):
    lead: str
    concepts: list[Concept]
    concepts_more: int
    concepts_empty: str
    cloud: list[CloudItem]
    cloud_empty: str
    profile: Profile


class FeedRow(Strict):
    feed: str
    folder: str | None
    articles: int
    judged: int
    mean_score: float | None
    mean_band: int | None
    cut_share: float
    read_share: float
    star_share: float
    proposal: str | None = None


class Feeds(Strict):
    lead: str
    rows: list[FeedRow]
    empty_text: str
    days: int


class SpendDay(Strict):
    day: str  # ISO date in the reader's zone
    label: str  # "Mon 6 Oct"
    usd: float


class Money(Strict):
    lead: str
    days: list[SpendDay]
    budget_usd: float
    chart_title: str
    per_1000: float | None
    per_1000_text: str
    cache_hit: float | None
    cache_hit_text: str
    month_usd: float
    month_text: str
    empty_text: str


class BatchRow(Strict):
    id: str
    purpose: str
    articles: int
    age_text: str


class RunRow(Strict):
    id: int
    kind: str
    started_text: str
    started_at: str | None
    ok: bool
    summary: str


class Machinery(Strict):
    lead: str
    batches: list[BatchRow]
    batches_empty: str
    runs: list[RunRow]
    runs_empty: str
    log: list[str]  # console only


class Footer(Strict):
    version: str
    data_dir: str | None
    docs_url: str
    repo_url: str
    exported_text: str | None = None


class DashboardDocument(Strict):
    schema_version: int = Field(default=SCHEMA, alias="schema")
    as_of: str
    as_of_text: str
    timezone: str
    state: State
    today: Today
    list: ReadingList
    verdicts: Verdicts
    learning: Learning
    feeds: Feeds
    money: Money
    machinery: Machinery
    footer: Footer

    model_config = {"extra": "forbid", "populate_by_name": True}

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


def json_schema() -> dict[str, Any]:
    return DashboardDocument.model_json_schema(by_alias=True)


# -- words for numbers and times -----------------------------------------------------------


def plural(count: float, one: str, many: str) -> str:
    return one if count == 1 else many


def n(value: float) -> str:
    return f"{value:,.0f}"


def usd(value: float) -> str:
    return f"${value:,.2f}"


def pct(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.0f}%"


def minutes_text(seconds: float) -> str:
    mins = max(0, round(seconds / 60))
    if mins < 1:
        return "under a minute"
    if mins < 60:
        return f"{mins} min"
    hours, rest = divmod(mins, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def age_text(iso: str | None, now: datetime) -> str:
    """ "2 h", "yesterday", "Mon 6 Oct": relative under a day, a weekday and date beyond."""
    then = _parse(iso)
    if then is None:
        return ""
    delta = now - then
    secs = delta.total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min"
    if secs < 86400:
        return f"{int(secs // 3600)} h"
    local = then.astimezone(now.tzinfo)
    if (now.date() - local.date()).days == 1:
        return "yesterday"
    return local.strftime("%a %-d %b") if hasattr(local, "strftime") else ""


def when_text(iso: str | None, now: datetime) -> str:
    """ "2 h ago", "yesterday", "on Mon 6 Oct": the age as a phrase that reads in a sentence."""
    age = age_text(iso, now)
    if not age:
        return ""
    if age in ("just now", "yesterday"):
        return age
    return f"{age} ago" if age[0].isdigit() else f"on {age}"


def clock_text(iso: str | None, now: datetime) -> str:
    """ "16:56" today, "yesterday 09:10", "Mon 6 Oct 09:10" before."""
    then = _parse(iso)
    if then is None:
        return ""
    local = then.astimezone(now.tzinfo)
    days = (now.date() - local.date()).days
    if days == 0:
        return local.strftime("%H:%M")
    if days == 1:
        return "yesterday " + local.strftime("%H:%M")
    return local.strftime("%a %-d %b %H:%M")


def safe_url(url: Any) -> str:
    """An article's link as the page may carry it: http(s) or nothing. The document is the
    page's data, so what a feed put in a URL field is sanitised here, once."""
    text = str(url or "")
    return text if text.startswith(("http://", "https://")) else ""


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def band_of(cfg: Config, score: float) -> int:
    for threshold in sorted(cfg.tags.bands, reverse=True):
        if score >= threshold:
            return threshold
    return min(cfg.tags.bands)


def band_label(cfg: Config, band: int) -> str:
    above = sorted(b for b in cfg.tags.bands if b > band)
    if not above:
        return f"{band} and up"
    top = min(above) - 1
    return f"{band} to {top}" if band else f"under {top + 1}"


# -- the panels ----------------------------------------------------------------------------


RUN_INTERVAL_S = 1800  # the schedule's interval; the document does not ask launchd


def _next_run(runs: list[dict[str, Any]], now: datetime) -> tuple[float | None, str]:
    """Seconds until the schedule's next run, from the last timer run: known while the last one
    is less than two intervals old, else the schedule looks dormant and the last run's age is
    said instead."""
    last = next((r for r in runs if r["kind"] == "run"), None)
    if last is None:
        return None, "no run yet"
    started = _parse(last["started_at"])
    if started is None:
        return None, "no run yet"
    elapsed = (now - started).total_seconds()
    if elapsed > 2 * RUN_INTERVAL_S:
        return None, f"last run {when_text(last['started_at'], now)}"
    return max(0.0, RUN_INTERVAL_S - elapsed), ""


def _state(cfg: Config, conn: sqlite3.Connection, now: datetime, mode: str) -> State:
    from .. import progress

    data_dir = cfg.root / cfg.data_dir
    proc = locks.running_process(data_dir)
    act = progress.activity(conn, cfg)
    phase = store.kv_get(conn, store.PHASE_KEY) or "" if proc else ""
    done, total = _progress_numbers(phase)
    rate = float(act.get("rate_per_min") or 0.0)
    runs = [dict(r) for r in store.last_runs(conn, 3)]
    next_in, last_text = _next_run(runs, now) if not proc else (None, "")
    eta = (total - done) / rate if (proc and total and done is not None and rate > 0) else None
    sentence = _state_sentence(proc, phase, done, total, rate, eta, next_in, last_text)
    mode_name = "autocut" if cfg.autocut.enabled else "scoring-only"
    levers = [] if mode == "export" else (["stop"] if proc else ["run", "pause"])
    return State(
        running=proc is not None,
        kind=proc["kind"] if proc else None,
        phase=phase,
        sentence=sentence,
        progress_done=done if proc else None,
        progress_total=total if proc else None,
        rate_per_min=rate if proc else None,
        eta_min=(eta or 0.0) if (proc and eta is not None) else None,
        mode=mode_name,
        mode_label="Scoring only" if not cfg.autocut.enabled else f"Cutting below {cfg.autocut.cutoff:g}",
        next_run_in_s=next_in,
        attention=_attention(cfg, conn, now),
        levers=levers,
    )


def _progress_numbers(phase: str) -> tuple[int | None, int | None]:
    """ "judging 240 of 1,200" -> (240, 1200); anything else -> (None, None)."""
    import re

    m = re.search(r"(\d[\d,]*) of (\d[\d,]*)", phase)
    if not m:
        return None, None
    return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))


def _state_sentence(
    proc: dict[str, Any] | None,
    phase: str,
    done: int | None,
    total: int | None,
    rate: float,
    eta: float | None,
    next_in: float | None,
    last_text: str = "",
) -> str:
    if proc is None:
        if next_in is not None:
            return f"Idle · next run in {minutes_text(next_in)}"
        return "Idle" + (f" · {last_text}" if last_text else "")
    what = phase or f"{proc['kind']} in progress"
    parts = [what[0].upper() + what[1:]]
    if done is not None and total:
        parts.append(f"{n(rate)} a minute" if rate else "")
        parts.append(f"about {minutes_text(eta * 60)} left" if eta is not None else "")
    return " · ".join(p for p in parts if p)


def _attention(cfg: Config, conn: sqlite3.Connection, now: datetime) -> Attention | None:
    """The one banner, the most severe first; None when nothing needs the reader."""
    runs = [dict(r) for r in store.last_runs(conn, 5)]
    last = runs[0] if runs else None
    tripped = store.kv_get(conn, store.SAFETY_KEY)
    if tripped:
        return Attention(
            key="safety",
            level="critical",
            word="The safety limit tripped",
            text=f"Run {tripped} would have cut nearly everything newly judged; autocut cuts nothing until you run bodh safety reset.",
        )
    if last and not last["ok"] and last.get("error") and last["kind"] in ("run", "weekly", "catch-up"):
        error = str(last["error"])
        if "403" in error or "login" in error.lower() or "YearSubscriptionExpired" in error:
            return Attention(
                key="signin",
                level="critical",
                word="Sign-in failed",
                text=f"BazQux refused the login: {error[:140]}. Check the password in .env.",
            )
        return Attention(key="failed", level="critical", word="The last run failed", text=error[:200])
    spent = store.spent_since(conn, store.local_midnight_iso(cfg.timezone))
    if spent >= cfg.models.daily_budget_usd:
        return Attention(
            key="budget",
            level="warning",
            word="Budget reached",
            text=f"{usd(spent)} of {usd(cfg.models.daily_budget_usd)} spent today; judging pauses until midnight and nothing is cut because of it.",
            dismissable=True,
        )
    weekly = next((r for r in runs if r["kind"] == "weekly" and r["ok"]), None)
    if (
        weekly
        and _parse(weekly.get("finished_at"))
        and now - (_parse(weekly["finished_at"]) or now) < timedelta(days=1)
    ):
        applied = "rewritten" in (weekly.get("summary") or "") and "rejected" not in (
            weekly.get("summary") or ""
        )
        if applied:
            return Attention(
                key="weekly",
                level="good",
                word="Profile rewritten",
                text="Sunday's rewrite passed the gate.",
                link_text="Show the diff",
                link_url="#learning",
                dismissable=True,
            )
    return None


def _today(cfg: Config, conn: sqlite3.Connection, now: datetime, tz: ZoneInfo) -> Today:
    unread = int(conn.execute("SELECT COUNT(*) FROM state WHERE status = 'unread'").fetchone()[0])
    days = [(now.astimezone(tz).date() - timedelta(days=i)) for i in range(6, -1, -1)]
    starts = [
        datetime.combine(d, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc).isoformat() for d in days
    ]
    ends = [*starts[1:], (now + timedelta(days=1)).isoformat()]
    series: dict[str, list[float]] = {"judged": [], "cut": [], "read": [], "spend": []}
    for start, end in zip(starts, ends, strict=True):
        series["judged"].append(
            _count(
                conn,
                "SELECT COUNT(*) FROM scores WHERE scored_at >= ? AND scored_at < ? AND model != 'dedup'",
                (start, end),
            )
        )
        series["cut"].append(
            _count(conn, "SELECT COUNT(*) FROM state WHERE cut_at >= ? AND cut_at < ?", (start, end))
        )
        series["read"].append(
            _count(
                conn, "SELECT COUNT(*) FROM feedback WHERE kind = 'read' AND at >= ? AND at < ?", (start, end)
            )
        )
        series["spend"].append(
            float(
                conn.execute(
                    "SELECT COALESCE(SUM(usd), 0) FROM costs WHERE created_at >= ? AND created_at < ?",
                    (start, end),
                ).fetchone()[0]
            )
        )
    tiles = [
        _tile("judged", "Judged today", series["judged"], "up-good", "down-bad"),
        _tile("cut", "Cut today", series["cut"], "", ""),
        _tile("read", "Read today", series["read"], "up-good", "down-bad"),
        _tile("spend", "Spend today", series["spend"], "up-bad", "down-good", money=True),
    ]
    today = {t.key: t.value for t in tiles}
    lead = (
        f"{n(today['judged'])} judged, {n(today['cut'])} cut, {n(today['read'])} read, {usd(today['spend'])} spent."
        if any(today.values())
        else "Nothing yet today. The next run judges what arrived and the tiles fill in."
    )
    return Today(
        unread=unread,
        target=cfg.prune.keep,
        hero_text=f"{n(unread)} unread · target {n(cfg.prune.keep)}",
        lead=lead,
        tiles=tiles,
    )


def _tile(key: str, label: str, series: list[float], up: str, down: str, money: bool = False) -> Tile:
    value, before = series[-1], series[-2]
    delta = value - before
    sign = "+" if delta >= 0 else "−"
    text, dtext = (
        (usd(value), f"{sign}{usd(abs(delta))} vs yesterday")
        if money
        else (n(value), f"{sign}{n(abs(delta))} vs yesterday")
    )
    direction = (up if delta > 0 else down if delta < 0 else "") if (up or down) else ""
    quiet = not value and not before  # two empty days say nothing against each other
    return Tile(
        key=key,
        label=label,
        value=value,
        text=text,
        delta=None if quiet else delta,
        delta_text="" if quiet else dtext,
        direction=direction,
        series=series,
        series_label=f"{label.split(maxsplit=1)[0].lower()}, last seven days",
    )


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def list_rows(
    cfg: Config,
    conn: sqlite3.Connection,
    now: datetime,
    band: int = LIST_BAND,
    offset: int = 0,
    limit: int = LIST_CAP,
    folder: str | None = None,
) -> tuple[list[Article], int]:
    """The unread articles at `band` and above, best first, one page of them and the count of
    all of them: the list panel, and the console's `/list` for the page's band filter and
    its "Show 50 more"."""
    where = "st.status = 'unread' AND s.score >= ? AND s.model != 'dedup'"
    params: list[Any] = [band]
    if folder:
        where += " AND a.folder = ?"
        params.append(folder)
    rows = conn.execute(
        f"""SELECT a.id, a.title, a.url, a.feed_title, a.folder, a.crawled_msec, s.score, s.lesson, s.concept, s.scored_at
           FROM state st JOIN scores s ON s.article_id = st.article_id JOIN articles a ON a.id = st.article_id
           WHERE {where} ORDER BY s.score DESC, a.crawled_msec DESC LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    ).fetchall()
    total = _count(
        conn,
        f"SELECT COUNT(*) FROM state st JOIN scores s ON s.article_id = st.article_id JOIN articles a ON a.id = st.article_id WHERE {where}",
        tuple(params),
    )
    return [_article(cfg, r, now) for r in rows], total


def _list(cfg: Config, conn: sqlite3.Connection, now: datetime, state: State, mode: str) -> ReadingList:
    articles, total = list_rows(cfg, conn, now, LIST_BAND, 0, LIST_CAP)
    pending = store.unscored_unread_count(
        conn, cfg.judge.prompt_version, cfg.limits.max_age_days, cfg.unjudged_folders(), exclude_inflight=True
    )
    folders = sorted({*cfg.folders, *(a.folder for a in articles if a.folder)})
    parts = []
    if pending:
        parts.append(f"{n(pending)} {plural(pending, 'article is', 'articles are')} waiting for the judge")
    if state.next_run_in_s is not None:
        parts.append(f"the next run is in {minutes_text(state.next_run_in_s)}")
    empty_text = f"Nothing at {LIST_BAND} or above right now. " + (
        "; ".join(parts) + "."
        if parts
        else "The next run submits what arrives to the judge; scores arrive within the hour."
    )
    lead = (
        f"{n(total)} unread at {LIST_BAND} and above, best first."
        if total
        else f"Nothing to read at {LIST_BAND} or above yet."
    )
    # the export has no server to ask: only the bands its rows already cover
    lowest = LIST_BAND if mode == "export" else 50
    return ReadingList(
        lead=lead,
        band=LIST_BAND,
        bands=[b for b in sorted(cfg.tags.bands, reverse=True) if b >= lowest],
        folders=folders,
        articles=articles,
        total=total,
        pending=pending,
        empty_text=empty_text,
        verdicts=[*list(GRADES), "basic", "heavy", "more", "less"] if mode != "export" else [],
    )


def _article(cfg: Config, r: sqlite3.Row, now: datetime) -> Article:
    crawled = (
        datetime.fromtimestamp((r["crawled_msec"] or 0) / 1000, tz=timezone.utc).isoformat()
        if r["crawled_msec"]
        else None
    )
    return Article(
        id=str(r["id"]),
        title=str(r["title"] or "(untitled)"),
        url=safe_url(r["url"]),
        reader_url=_reader_url(str(r["id"])),
        score=float(r["score"]),
        band=band_of(cfg, float(r["score"])),
        lesson=str(r["lesson"] or ""),
        concept=str(r["concept"] or ""),
        feed=str(r["feed_title"] or ""),
        folder=r["folder"],
        age_text=age_text(crawled, now),
        arrived_at=crawled,
        judged_at=r["scored_at"],
    )


def _reader_url(article_id: str) -> str | None:
    """The article in BazQux: its item id is the tail of the long id."""
    tail = article_id.rsplit("/", 1)[-1]
    return f"https://bazqux.com/i/{tail}" if tail and tail != article_id else None


def _grid(cfg: Config, conn: sqlite3.Connection, since: str) -> tuple[list[GridRow], str, int]:
    """The grades-by-band grid, its sentence, and the number of verdicts in the window."""
    marks = ",".join("?" * len(GRADES))
    counts: dict[int, dict[str, int]] = {b: dict.fromkeys(GRADES, 0) for b in cfg.tags.bands}
    for r in conn.execute(
        f"""SELECT {_band_case(cfg, "s.score")} AS band, f.kind, COUNT(*) AS k FROM feedback f
            JOIN scores s ON s.article_id = f.article_id WHERE f.kind IN ({marks}) AND f.at >= ?
            GROUP BY band, f.kind""",
        (*GRADES, since),
    ):
        counts[int(r["band"])][str(r["kind"])] += int(r["k"])
    grid = [
        GridRow(band=b, label=band_label(cfg, b), counts=counts[b])
        for b in sorted(cfg.tags.bands, reverse=True)
    ]
    liked_high = sum(counts[b]["great"] + counts[b]["good"] for b in counts if b >= 70)
    liked = sum(counts[b]["great"] + counts[b]["good"] for b in counts)
    total = sum(sum(c.values()) for c in counts.values())
    if not total:
        sentence = "No verdicts yet. Tag an article great, good, meh or bad in BazQux; it shows here after the next run, within 30 minutes."
    elif liked and liked_high / liked >= 0.7:
        sentence = f"Your great and good fall mostly in the 70+ bands ({liked_high} of {liked}): the judge is calibrated to you."
    elif liked:
        count = "None" if not liked_high else f"Only {liked_high}"
        sentence = f"{count} of your {liked} great and good {'is' if liked == 1 else 'are'} in the 70+ bands: the judge rates what you like lower than you do. Keep tagging; Sunday's rewrite reads these."
    else:
        sentence = f"{total} verdicts, none of them great or good yet."
    return grid, sentence, total


def _disagreements(cfg: Config, conn: sqlite3.Connection, since: str) -> list[Disagreement]:
    return [
        Disagreement(
            id=str(r["id"]),
            title=str(r["title"] or ""),
            url=safe_url(r["url"]),
            grade=str(r["kind"]),
            score=float(r["score"]),
            band=band_of(cfg, float(r["score"])),
            concept=str(r["concept"] or ""),
        )
        for r in conn.execute(
            """SELECT a.id, a.title, a.url, s.score, s.concept, f.kind FROM feedback f
               JOIN scores s ON s.article_id = f.article_id JOIN articles a ON a.id = f.article_id
               WHERE f.at >= ? AND ((f.kind IN ('great', 'good', 'rescue') AND s.score < 50) OR (f.kind IN ('bad', 'meh') AND s.score >= 70))
               ORDER BY ABS(s.score - 50) DESC, f.at DESC LIMIT 10""",
            (since,),
        )
    ]


def _near_misses(cfg: Config, conn: sqlite3.Connection) -> list[NearMiss]:
    return [
        NearMiss(
            id=str(r["id"]),
            title=str(r["title"] or ""),
            url=safe_url(r["url"]),
            score=float(r["score"]),
            band=band_of(cfg, float(r["score"])),
            lesson=str(r["lesson"] or ""),
            concept=str(r["concept"] or ""),
            cut_mode=store.cut_mode_name(r["cut_mode"]),
        )
        for r in conn.execute(
            """SELECT a.id, a.title, a.url, s.score, s.lesson, s.concept, st.cut_mode FROM state st
               JOIN articles a ON a.id = st.article_id JOIN scores s ON s.article_id = a.id
               WHERE st.status = 'cut' AND st.cut_at >= ? ORDER BY s.score DESC LIMIT 10""",
            (store.days_ago_iso(7),),
        )
    ]


def _verdicts(cfg: Config, conn: sqlite3.Connection, now: datetime) -> Verdicts:
    since = store.days_ago_iso(30)
    grid, grid_sentence, total = _grid(cfg, conn, since)
    dis = _disagreements(cfg, conn, since)
    calibration = _calibration(cfg, conn)
    near = _near_misses(cfg, conn)
    lead = (
        f"{total} {plural(total, 'verdict', 'verdicts')} in the last 30 days; {len(dis)} {plural(len(dis), 'disagreement', 'disagreements')}; "
        f"{len(near)} {plural(len(near), 'near miss', 'near misses')} this week."
        if total
        else "Your grades, where you and the judge disagree, how often each band gets read, and the best of what was cut."
    )
    return Verdicts(
        lead=lead,
        grid=grid,
        grid_sentence=grid_sentence,
        disagreements=dis,
        disagreements_empty="No disagreements in the last 30 days: nothing you graded great or good scored under 50, nothing you graded bad or meh scored 70 or more.",
        calibration=calibration,
        calibration_sentence=_calibration_sentence(calibration),
        calibration_empty="Nothing has waited a day yet. Once judged articles sit unread for a day, how often each band gets read and starred shows here; a judge that fits you reads high and ignores low.",
        near_misses=near,
        near_misses_empty="Nothing was cut in the last seven days."
        if cfg.autocut.enabled
        else "Nothing was cut in the last seven days: scoring-only mode judges and tags, and cuts nothing.",
    )


def _band_case(cfg: Config, column: str) -> str:
    """A CASE expression that maps a score column to its band threshold, from the configuration."""
    cases = " ".join(f"WHEN {column} >= {b} THEN {b}" for b in sorted(cfg.tags.bands, reverse=True))
    return f"CASE {cases} ELSE {min(cfg.tags.bands)} END"


def _calibration(cfg: Config, conn: sqlite3.Connection) -> list[CalibrationRow]:
    """Per band, among articles that waited at least a day and were not cut: how many, and the
    shares read and starred. Two aggregates: the counts walk `state` by status (the small
    side), the marks walk `feedback` (smaller still); nothing walks every score."""
    since = store.days_ago_iso(1)
    band = _band_case(cfg, "s.score")
    counts = {
        int(r[0]): int(r[1])
        for r in conn.execute(
            f"""SELECT {band} AS band, COUNT(*) FROM state st JOIN scores s ON s.article_id = st.article_id
                WHERE st.status IN ('unread', 'read', 'dismissed', 'expired') AND s.scored_at <= ? AND s.model != 'dedup'
                GROUP BY band""",
            (since,),
        )
    }
    marks = {
        int(r[0]): (int(r[1] or 0), int(r[2] or 0))
        for r in conn.execute(
            f"""SELECT {band} AS band, SUM(f.kind = 'read'), SUM(f.kind = 'star')
                FROM feedback f JOIN scores s ON s.article_id = f.article_id JOIN state st ON st.article_id = f.article_id
                WHERE f.kind IN ('read', 'star') AND s.scored_at <= ? AND s.model != 'dedup'
                  AND st.status IN ('unread', 'read', 'dismissed', 'expired')
                GROUP BY band""",
            (since,),
        )
    }
    out = []
    for b in sorted(cfg.tags.bands, reverse=True):
        total = counts.get(b, 0)
        read, starred = marks.get(b, (0, 0))
        out.append(
            CalibrationRow(
                band=b,
                label=band_label(cfg, b),
                n=total,
                read_share=(read / total) if total else None,
                star_share=(starred / total) if total else None,
            )
        )
    return out


def _calibration_sentence(rows: list[CalibrationRow]) -> str:
    with_data = [r for r in rows if r.n >= 10 and r.read_share is not None]
    if len(with_data) < 2:
        return "Higher bands should be read more often. The shares appear once each band has ten articles that waited a day."
    top, low = with_data[0], with_data[-1]
    if (top.read_share or 0) > (low.read_share or 0):
        return f"Higher bands are read more often ({pct(top.read_share)} of {top.label} against {pct(low.read_share)} of {low.label}): the shape is right."
    return f"The {low.label} band is read as often as {top.label} ({pct(low.read_share)} against {pct(top.read_share)}): the mix of sub-scores in config.yaml may be off."


def _learning(cfg: Config, conn: sqlite3.Connection, now: datetime) -> Learning:
    rows = store.feedback_since(conn, ["read", "star", "great", "good"], store.days_ago_iso(7))
    seen: set[str] = set()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        if r["id"] in seen or not r["lesson"]:
            continue
        seen.add(r["id"])
        grouped[(r["concept"] or "other").strip().lower()].append(r)
    ordered = sorted(grouped.items(), key=lambda kv: -len(kv[1]))
    concepts = [
        Concept(
            concept=c,
            count=len(items),
            items=[
                Learned(title=str(r["title"] or ""), url=safe_url(r["url"]), lesson=str(r["lesson"]))
                for r in items[:LINES_PER_CONCEPT]
            ],
            more=max(0, len(items) - LINES_PER_CONCEPT),
        )
        for c, items in ordered[:CONCEPTS_CAP]
    ]
    since = store.days_ago_iso(cfg.profile.concept_window_days)
    covered = store.concept_counts(conn, ["star", "read", "rescue"], since, limit=CLOUD_CAP)
    known = {c for c, _ in store.concept_counts(conn, ["star", "read", "rescue"], since, limit=500)}
    frontier = _frontier(conn, known)
    cloud = [CloudItem(concept=c, count=k, frontier=False) for c, k in covered] + [
        CloudItem(concept=c, count=k, frontier=True) for c, k in frontier
    ]
    lead = (
        f"{len(seen)} {plural(len(seen), 'article', 'articles')} read or liked this week across {len(grouped)} {plural(len(grouped), 'concept', 'concepts')}."
        if seen
        else "Nothing read yet this week. What you read and star in BazQux appears here by concept, with its lesson."
    )
    return Learning(
        lead=lead,
        concepts=concepts,
        concepts_more=max(0, len(ordered) - CONCEPTS_CAP),
        concepts_empty="Nothing read yet this week. Read or star an article in BazQux; its lesson appears here after the next run.",
        cloud=cloud,
        cloud_empty="The concept map fills as you read: every concept you read or starred in the last 90 days, sized by count, with the frontier (seen at 70 and above this fortnight, not yet read) marked.",
        profile=_profile(cfg, conn, now),
    )


def _frontier(conn: sqlite3.Connection, known: set[str]) -> list[tuple[str, int]]:
    rows = conn.execute(
        """SELECT LOWER(TRIM(s.concept)) c, COUNT(*) k FROM scores s
           WHERE s.scored_at >= ? AND s.score >= 70 AND s.concept IS NOT NULL AND s.concept != ''
           GROUP BY c ORDER BY k DESC LIMIT 200""",
        (store.days_ago_iso(14),),
    ).fetchall()
    out = [(str(r[0]), int(r[1])) for r in rows if r[0] not in known]
    return out[:15]


def _profile(cfg: Config, conn: sqlite3.Connection, now: datetime) -> Profile:
    versions = store.learned_versions(conn, 2)
    text = cfg.read_learned()
    diff: list[DiffLine] = []
    if len(versions) == 2:
        for line in difflib.unified_diff(
            str(versions[1]["text"]).splitlines(),
            str(versions[0]["text"]).splitlines(),
            "last week",
            "this week",
            lineterm="",
            n=1,
        ):
            if line.startswith(("---", "+++", "@@")):
                continue
            kind = "add" if line.startswith("+") else "del" if line.startswith("-") else "ctx"
            diff.append(DiffLine(kind=kind, text=line[1:] if kind != "ctx" else line.lstrip(" ")))
    newest = versions[0] if versions else None
    return Profile(
        version=int(newest["version"]) if newest else None,
        date_text=clock_text(newest["created_at"], now) if newest else "not yet rewritten",
        text=text,
        diff=diff,
        diff_empty="No Sunday diff yet: the profiler rewrites the learned profile once a week from what you read; the diff appears after the first rewrite.",
    )


def _feeds(cfg: Config, conn: sqlite3.Connection) -> Feeds:
    days = 30
    since = store.days_ago_iso(days)
    rows = conn.execute(
        """SELECT a.feed_title, a.folder, COUNT(*) n, COUNT(s.score) judged, AVG(s.score) mean_score,
                  AVG(CASE WHEN st.status = 'cut' AND st.cut_mode NOT IN ('clear-old', 'bankruptcy') THEN 1.0 ELSE 0.0 END) cut_share
           FROM articles a JOIN state st ON st.article_id = a.id LEFT JOIN scores s ON s.article_id = a.id
           WHERE a.fetched_at >= ? AND a.feed_title IS NOT NULL AND a.feed_title != ''
           GROUP BY a.feed_title ORDER BY n DESC LIMIT ?""",
        (since, FEEDS_CAP),
    ).fetchall()
    marks = {
        str(r[0]): (int(r[1] or 0), int(r[2] or 0))
        for r in conn.execute(
            """SELECT a.feed_title, SUM(f.kind = 'read'), SUM(f.kind = 'star') FROM feedback f
               JOIN articles a ON a.id = f.article_id WHERE f.kind IN ('read', 'star') AND a.fetched_at >= ?
               GROUP BY a.feed_title""",
            (since,),
        )
    }
    proposals = _proposals(cfg)
    out = [
        FeedRow(
            feed=str(r["feed_title"]),
            folder=r["folder"],
            articles=int(r["n"]),
            judged=int(r["judged"]),
            mean_score=round(float(r["mean_score"]), 1) if r["mean_score"] is not None else None,
            mean_band=band_of(cfg, float(r["mean_score"])) if r["mean_score"] is not None else None,
            cut_share=float(r["cut_share"] or 0.0),
            read_share=marks.get(str(r["feed_title"]), (0, 0))[0] / int(r["n"]),
            star_share=marks.get(str(r["feed_title"]), (0, 0))[1] / int(r["n"]),
            proposal=proposals.get(str(r["feed_title"])),
        )
        for r in rows
    ]
    lead = (
        f"{len(out)} {plural(len(out), 'feed', 'feeds')} with articles in the last {days} days, most active first."
        if out
        else "No articles in the last 30 days yet."
    )
    return Feeds(
        lead=lead,
        rows=out,
        empty_text="No feeds yet. The first run fetches your unread articles and the feeds appear here with their numbers.",
        days=days,
    )


def _proposals(cfg: Config) -> dict[str, str]:
    """The monthly report's proposals, by feed, when the report has run."""
    path = cfg.root / cfg.dashboard_dir / "report.md"
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("- ") and ":" in line:
                feed, _, proposal = line[2:].partition(":")
                out[feed.strip().strip("*`")] = proposal.strip()
    except OSError:
        return {}
    return out


def _money(cfg: Config, conn: sqlite3.Connection, now: datetime, tz: ZoneInfo) -> Money:
    days: list[SpendDay] = []
    local_today = now.astimezone(tz).date()
    per: dict[str, float] = defaultdict(float)
    since = (
        datetime.combine(local_today - timedelta(days=29), datetime.min.time(), tzinfo=tz)
        .astimezone(timezone.utc)
        .isoformat()
    )
    for created, usd_value in conn.execute(
        "SELECT created_at, usd FROM costs WHERE created_at >= ?", (since,)
    ):
        t = _parse(created)
        if t is not None:
            per[t.astimezone(tz).strftime("%Y-%m-%d")] += float(usd_value or 0.0)
    for i in range(29, -1, -1):
        d = local_today - timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        days.append(SpendDay(day=key, label=d.strftime("%a %-d %b"), usd=round(per.get(key, 0.0), 4)))
    week_since = store.days_ago_iso(7)
    judged_week = _count(
        conn, "SELECT COUNT(*) FROM scores WHERE scored_at >= ? AND model != 'dedup'", (week_since,)
    )
    spend_week = store.spent_since(conn, week_since)
    per_1000 = (spend_week / judged_week * 1000) if judged_week else None
    tok = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read),0), COALESCE(SUM(cache_write),0) FROM costs WHERE created_at >= ? AND purpose = 'teacher'",
        (week_since,),
    ).fetchone()
    prompt_total = tok[0] + tok[1] + tok[2]
    cache_hit = (tok[1] / prompt_total) if prompt_total else None
    month_usd = sum(d.usd for d in days)
    over = [d for d in days if d.usd > cfg.models.daily_budget_usd]
    peak = max(days, key=lambda d: d.usd)
    if not any(d.usd for d in days):
        chart_title = "No spend yet this month."
    elif not over:
        chart_title = f"Spend is under the {usd(cfg.models.daily_budget_usd)} budget every day this month; the dearest day was {peak.label} at {usd(peak.usd)}."
    else:
        chart_title = f"Spend passed the {usd(cfg.models.daily_budget_usd)} budget on {len(over)} day{'s' if len(over) > 1 else ''} this month; the dearest was {peak.label} at {usd(peak.usd)}."
    return Money(
        lead=f"{usd(month_usd)} in the last 30 days"
        + (f", {usd(per_1000)} per 1,000 judged this week." if per_1000 is not None else "."),
        days=days,
        budget_usd=cfg.models.daily_budget_usd,
        chart_title=chart_title,
        per_1000=round(per_1000, 2) if per_1000 is not None else None,
        per_1000_text=usd(per_1000) if per_1000 is not None else "no judgments this week",
        cache_hit=round(cache_hit, 3) if cache_hit is not None else None,
        cache_hit_text=pct(cache_hit) if cache_hit is not None else "no cache data yet",
        month_usd=round(month_usd, 2),
        month_text=usd(month_usd),
        empty_text="No spend yet. The first run's judgments cost a few cents; the chart fills day by day.",
    )


def _machinery(cfg: Config, conn: sqlite3.Connection, now: datetime, mode: str) -> Machinery:
    batches = [
        BatchRow(
            id=str(b["id"]),
            purpose=str(b["purpose"] or ""),
            articles=int(b["n_articles"] or 0),
            age_text=age_text(b["created_at"], now),
        )
        for b in store.recent_batches(conn, 8)
        if (b["status"] or "") not in ("ended", "collected", "given_up", "expired")
    ]
    runs = [
        RunRow(
            id=int(r["id"]),
            kind=str(r["kind"]),
            started_text=clock_text(r["started_at"], now),
            started_at=r["started_at"],
            ok=bool(r["ok"]),
            summary=str(r["summary"] or r["error"] or ""),
        )
        for r in store.last_runs(conn, RUNS_CAP)
    ]
    log_lines: list[str] = []
    if mode != "export":
        from .. import logs, progress

        log_lines = progress.log_tail(logs.log_file(cfg.root / cfg.data_dir), 10)
    lead = (
        f"{len(batches)} {plural(len(batches), 'batch', 'batches')} with the model, {len(runs)} recent {plural(len(runs), 'run', 'runs')}."
        if batches
        else f"No batch with the model; {len(runs)} recent runs."
        if runs
        else "No runs yet."
    )
    return Machinery(
        lead=lead,
        batches=batches,
        batches_empty="No batch with the model right now.",
        runs=runs,
        runs_empty="No runs yet. The first `bodh run` appears here with its summary.",
        log=log_lines,
    )


# -- the document ---------------------------------------------------------------------------


def local_now(cfg: Config, now: datetime | None = None) -> datetime:
    """The instant the page is dated from, in the reader's zone, so every time printed from it
    (the stamp, "yesterday 09:10", "Mon 6 Oct") reads in that zone."""
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(cfg.timezone))


def build(
    cfg: Config, conn: sqlite3.Connection, mode: str = "console", now: datetime | None = None
) -> DashboardDocument:
    """Every panel from the store, in page order. `mode` only decides what the document
    carries (the levers, the verdict menu and the log are console and app matters)."""
    tz = ZoneInfo(cfg.timezone)
    now = local_now(cfg, now)
    state = _state(cfg, conn, now, mode)
    return DashboardDocument(
        schema=SCHEMA,
        as_of=now.isoformat(),
        as_of_text=now.astimezone(tz).strftime("%H:%M"),
        timezone=cfg.timezone,
        state=state,
        today=_today(cfg, conn, now, tz),
        list=_list(cfg, conn, now, state, mode),
        verdicts=_verdicts(cfg, conn, now),
        learning=_learning(cfg, conn, now),
        feeds=_feeds(cfg, conn),
        money=_money(cfg, conn, now, tz),
        machinery=_machinery(cfg, conn, now, mode),
        footer=Footer(
            version=__version__,
            data_dir=str(cfg.root / cfg.data_dir) if mode != "export" else None,
            docs_url=DOCS_URL,
            repo_url=REPO_URL,
            exported_text=f"Exported {now.astimezone(tz).strftime('%H:%M')}; open the console for live data"
            if mode == "export"
            else None,
        ),
    )


def live(
    cfg: Config, conn: sqlite3.Connection, now: datetime | None = None, with_today: bool = False
) -> dict[str, Any]:
    """The slices a progress tick carries: the state (cheap, every tick) and, while a run is
    in progress, today's tiles, so the page moves without rebuilding the document."""
    now = local_now(cfg, now)
    state = _state(cfg, conn, now, "console")
    out: dict[str, Any] = {"as_of": now.isoformat(), "state": state.model_dump()}
    if with_today or state.running:
        out["today"] = _today(cfg, conn, now, ZoneInfo(cfg.timezone)).model_dump()
    return out


class Cache:
    """The document computed once per change and served from memory between: `get` builds
    when nothing is cached, the entry is older than `ttl` seconds, or `invalidate` was called
    (a run finishing, a verdict recorded, a settings change)."""

    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        self._entries: dict[str, tuple[float, DashboardDocument]] = {}

    def get(self, cfg: Config, conn: sqlite3.Connection, mode: str = "console") -> DashboardDocument:
        now = time.monotonic()
        hit = self._entries.get(mode)
        if hit is not None and now - hit[0] < self.ttl:
            return hit[1]
        doc = build(cfg, conn, mode)
        self._entries[mode] = (now, doc)
        return doc

    def invalidate(self) -> None:
        self._entries.clear()


_band_tokens = tokens.BANDS  # the page orders bands as the tokens do
