"""SQLite store: the system's memory of what it judged, cut and learned, next to BazQux,
which stays the only source of truth for what is read and tagged. (A rebuild of the
scored set from the bucket tags in BazQux is planned, not written: `triage rebuild`.)"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY,
  short_id INTEGER,
  feed_stream TEXT, feed_title TEXT, folder TEXT,
  title TEXT, url TEXT, author TEXT,
  published INTEGER, crawled_msec INTEGER,
  text TEXT,
  fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS scores (
  article_id TEXT PRIMARY KEY,
  prompt_version TEXT, model TEXT,
  quality INTEGER, learning INTEGER, fit INTEGER, breadth INTEGER, relevance INTEGER,
  base_score REAL, score REAL,
  lesson TEXT, concept TEXT, kind TEXT, flags TEXT, dupe_of TEXT, confidence TEXT,
  second_look_score REAL, second_look_model TEXT,
  bucket TEXT, bucket_tagged INTEGER DEFAULT 0,
  scored_at TEXT
);
CREATE TABLE IF NOT EXISTS state (
  article_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'unread',
  starred INTEGER DEFAULT 0,
  protected INTEGER DEFAULT 0,
  exploration INTEGER DEFAULT 0,
  exempt INTEGER DEFAULT 0,
  judge_failures INTEGER DEFAULT 0,
  cut_run_id INTEGER, cut_mode TEXT, cut_at TEXT,
  cut_tagged INTEGER DEFAULT 0, read_marked INTEGER DEFAULT 0,
  rescued_at TEXT, read_at TEXT,
  first_seen_at TEXT, last_seen_unread_at TEXT
);
CREATE INDEX IF NOT EXISTS state_status ON state(status);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, started_at TEXT, finished_at TEXT, ok INTEGER,
  unread_upstream INTEGER, new INTEGER, scored INTEGER, second_looks INTEGER,
  cut_floor INTEGER, cut_prune INTEGER, rescues INTEGER, reads INTEGER,
  spend_usd REAL, boundary REAL, error TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS costs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, model TEXT, purpose TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_write INTEGER, cache_read INTEGER,
  usd REAL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id TEXT, kind TEXT, at TEXT,
  UNIQUE(article_id, kind)
);
CREATE TABLE IF NOT EXISTS learner_versions (
  version INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT, reason TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  run_id INTEGER, purpose TEXT, model TEXT,
  n_requests INTEGER, n_articles INTEGER,
  status TEXT, note TEXT,
  created_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS inflight (
  article_id TEXT, purpose TEXT,
  batch_id TEXT, custom_id TEXT, ordinal INTEGER,
  PRIMARY KEY (article_id, purpose)
);
CREATE INDEX IF NOT EXISTS inflight_batch ON inflight(batch_id);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # worker threads record model costs through the same connection, serialized by a lock
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


# -- migrations ---------------------------------------------------------------
# SCHEMA above is the current shape, so a new database needs no migration. A database made
# by an older version carries kv.schema_version (missing = 1, the shape before migrations
# existed); each numbered step below brings it one version on, inside one transaction, and
# is written so that running it on a database that already has the change is harmless.

SCHEMA_VERSION = 3
SCHEMA_VERSION_KEY = "schema_version"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_2(conn: sqlite3.Connection) -> None:
    """state.exempt: an undone cut must not be repeated by the next automatic floor."""
    if "exempt" not in _columns(conn, "state"):
        conn.execute("ALTER TABLE state ADD COLUMN exempt INTEGER DEFAULT 0")


def _migrate_3(conn: sqlite3.Connection) -> None:
    """state.judge_failures: how often the model answered for this article with no judgment,
    so a broken prompt stops being resubmitted every half hour."""
    if "judge_failures" not in _columns(conn, "state"):
        conn.execute("ALTER TABLE state ADD COLUMN judge_failures INTEGER DEFAULT 0")


MIGRATIONS = {2: _migrate_2, 3: _migrate_3}


def schema_version(conn: sqlite3.Connection) -> int:
    return int(kv_get(conn, SCHEMA_VERSION_KEY) or 1)


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database to SCHEMA_VERSION. Returns the version it is at afterwards."""
    version = schema_version(conn)
    if version >= SCHEMA_VERSION:
        return version
    with transaction(conn):
        for step in range(version + 1, SCHEMA_VERSION + 1):
            MIGRATIONS[step](conn)
            kv_set(conn, SCHEMA_VERSION_KEY, str(step))
    return SCHEMA_VERSION


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """All or nothing, and one fsync instead of one per row: the connection runs in
    autocommit, so bulk writes and multi-step updates ask for a transaction explicitly."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# -- text ---------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def html_to_text(raw: str, limit: int = 20000) -> str:
    """Good-enough HTML to text: strip tags, unescape entities, collapse whitespace."""
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


# -- articles -----------------------------------------------------------------

def upsert_article(conn: sqlite3.Connection, item: Any, folder: str | None) -> None:
    conn.execute(
        """INSERT INTO articles (id, short_id, feed_stream, feed_title, folder, title, url, author,
                                 published, crawled_msec, text, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET folder=excluded.folder, feed_title=excluded.feed_title,
             feed_stream=excluded.feed_stream, title=excluded.title, url=excluded.url, author=excluded.author,
             published=excluded.published, crawled_msec=excluded.crawled_msec, text=excluded.text""",
        (
            item.id, item.short_id, item.feed_stream_id, item.feed_title, folder, item.title, item.url,
            item.author, item.published, item.crawled_msec, html_to_text(item.html), utcnow_iso(),
        ),
    )
    conn.execute(
        """INSERT INTO state (article_id, status, first_seen_at, last_seen_unread_at)
           VALUES (?, 'unread', ?, ?) ON CONFLICT(article_id) DO NOTHING""",
        (item.id, utcnow_iso(), utcnow_iso()),
    )


def unread_without_text(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT a.id FROM articles a JOIN state st ON st.article_id = a.id "
        "WHERE st.status = 'unread' AND (a.text IS NULL OR a.text = '')")]


def known_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT id FROM articles")}


IN_CHUNK = 900  # ids per `IN (...)`: SQLite caps the variables in one statement


def in_chunks(ids: Iterable[str], size: int = IN_CHUNK) -> Iterator[list[str]]:
    """``ids`` in lists small enough for one ``IN (...)`` clause."""
    batch: list[str] = []
    for i in ids:
        batch.append(i)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def fetch_in(conn: sqlite3.Connection, sql: str, ids: Iterable[str], before: tuple = (),
             after: tuple = ()) -> list[sqlite3.Row]:
    """Run ``sql`` (with ``{marks}`` where the ids go) once per chunk of ids and gather the rows,
    so a mark-all-read of a big pile never exceeds the variable limit."""
    rows: list[sqlite3.Row] = []
    for chunk in in_chunks(ids):
        marks = ",".join("?" * len(chunk))
        rows.extend(conn.execute(sql.format(marks=marks), (*before, *chunk, *after)).fetchall())
    return rows


def scored_ids(conn: sqlite3.Connection, prompt_version: str | None = None) -> set[str]:
    if prompt_version:
        rows = conn.execute("SELECT article_id FROM scores WHERE prompt_version = ?", (prompt_version,))
    else:
        rows = conn.execute("SELECT article_id FROM scores")
    return {r[0] for r in rows}


def article(conn: sqlite3.Connection, article_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT a.*, s.score, s.base_score, s.quality, s.learning, s.fit, s.breadth, s.relevance,
                  s.lesson, s.concept, s.kind, s.flags, s.confidence, s.dupe_of, s.second_look_score,
                  s.bucket, s.model, s.prompt_version, st.status, st.starred, st.protected, st.exploration, st.cut_mode, st.cut_at,
                  st.cut_run_id, st.rescued_at, st.read_at
           FROM articles a LEFT JOIN scores s ON s.article_id = a.id
                           LEFT JOIN state st ON st.article_id = a.id
           WHERE a.id = ?""",
        (article_id,),
    ).fetchone()


MAX_JUDGE_FAILURES = 3  # empty answers from the model for one article before it is left alone


def bump_judge_failures(conn: sqlite3.Connection, ids: Iterable[str]) -> None:
    """The model answered for these articles and said nothing usable; three times and they
    are left unread, unjudged, until `triage safety reset`."""
    conn.executemany("UPDATE state SET judge_failures = judge_failures + 1 WHERE article_id = ?", [(i,) for i in ids])


def given_up(conn: sqlite3.Connection) -> int:
    """Unread articles the teacher has stopped asking about."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM state st LEFT JOIN scores s ON s.article_id = st.article_id "
        "WHERE st.status = 'unread' AND st.judge_failures >= ? AND s.article_id IS NULL", (MAX_JUDGE_FAILURES,)
    ).fetchone()[0] or 0)


def reset_judge_failures(conn: sqlite3.Connection) -> int:
    return conn.execute("UPDATE state SET judge_failures = 0 WHERE judge_failures > 0").rowcount


def _unscored_where(prompt_version: str, max_age_days: int | None, skip_folders: Iterable[str],
                    exclude_inflight: bool, rejudge: tuple[str, float] | None = None) -> tuple[list[str], list[Any]]:
    """``rejudge`` = (teacher model, min score): a judgment by another model at or above the
    score counts as still pending for the teacher."""
    params: list[Any] = [prompt_version]
    if rejudge is not None:
        where = ["st.status = 'unread'",
                 "(s.article_id IS NULL OR (s.model != ? AND s.model != 'dedup' AND s.score >= ?))"]
        params += [rejudge[0], float(rejudge[1])]
    else:
        where = ["st.status = 'unread'", "s.article_id IS NULL"]
    where.append("st.judge_failures < ?")  # given up on after repeated empty answers (see given_up)
    params.append(MAX_JUDGE_FAILURES)
    if max_age_days is not None:
        cutoff_ms = int((utcnow() - timedelta(days=max_age_days)).timestamp() * 1000)
        where.append("((a.crawled_msec IS NULL AND a.published IS NULL) "
                     "OR COALESCE(a.crawled_msec, a.published * 1000) >= ?)")
        params.append(cutoff_ms)
    skip = [f for f in skip_folders if f]
    if skip:
        where.append(f"(a.folder IS NULL OR a.folder NOT IN ({','.join('?' * len(skip))}))")
        params.extend(skip)
    if exclude_inflight:
        where.append("a.id NOT IN (SELECT article_id FROM inflight WHERE purpose = 'teacher')")
    return where, params


def unscored_unread_count(conn: sqlite3.Connection, prompt_version: str, max_age_days: int | None = None,
                          skip_folders: Iterable[str] = (), exclude_inflight: bool = False,
                          rejudge: tuple[str, float] | None = None) -> int:
    where, params = _unscored_where(prompt_version, max_age_days, skip_folders, exclude_inflight, rejudge)
    row = conn.execute(
        f"""SELECT COUNT(*) FROM articles a JOIN state st ON st.article_id = a.id
            LEFT JOIN scores s ON s.article_id = a.id AND s.prompt_version = ?
            WHERE {' AND '.join(where)}""", params).fetchone()
    return int(row[0] or 0)


def unscored_unread_folders(conn: sqlite3.Connection, prompt_version: str, max_age_days: int | None = None,
                            skip_folders: Iterable[str] = (), exclude_inflight: bool = False) -> list[sqlite3.Row]:
    """Only the folder of each pending article (no text), for the progress views."""
    where, params = _unscored_where(prompt_version, max_age_days, skip_folders, exclude_inflight)
    return conn.execute(
        f"""SELECT a.folder FROM articles a JOIN state st ON st.article_id = a.id
            LEFT JOIN scores s ON s.article_id = a.id AND s.prompt_version = ?
            WHERE {' AND '.join(where)}""", params).fetchall()


def unscored_unread(conn: sqlite3.Connection, prompt_version: str, max_age_days: int | None = None,
                    skip_folders: Iterable[str] = (), exclude_inflight: bool = False,
                    limit: int | None = None, rejudge: tuple[str, float] | None = None) -> list[sqlite3.Row]:
    """Unread articles the teacher still has to judge: no score for this prompt version (or a
    promising one from another model, see ``rejudge``), not older than the window (articles of
    unknown age count as new), not in a folder the teacher never reads, and, if asked, not
    already sitting in a submitted batch."""
    where, params = _unscored_where(prompt_version, max_age_days, skip_folders, exclude_inflight, rejudge)
    tail = ""
    if limit:
        tail = " LIMIT ?"
        params.append(int(limit))
    return conn.execute(
        f"""SELECT a.* FROM articles a JOIN state st ON st.article_id = a.id
            LEFT JOIN scores s ON s.article_id = a.id AND s.prompt_version = ?
            WHERE {' AND '.join(where)}
            ORDER BY a.crawled_msec DESC{tail}""",
        params,
    ).fetchall()


def unread_scored(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unread articles with a score, joined with state; the pool every cut selects from."""
    return conn.execute(
        """SELECT a.id, a.feed_title, a.folder, a.title, a.url, a.crawled_msec, LENGTH(a.text) AS text_len,
                  s.score, s.quality, s.concept, s.confidence, s.dupe_of, s.flags,
                  s.second_look_score, s.second_look_model,
                  st.starred, st.protected, st.exploration, st.exempt, st.rescued_at
           FROM articles a JOIN state st ON st.article_id = a.id
                           JOIN scores s ON s.article_id = a.id
           WHERE st.status = 'unread'
           ORDER BY s.score ASC, a.crawled_msec ASC"""
    ).fetchall()


# -- scores -------------------------------------------------------------------

def record_score(conn: sqlite3.Connection, article_id: str, verdict: dict, base_score: float,
                 score: float, bucket: str, prompt_version: str, model: str) -> str | None:
    """Insert or replace a judgment. Returns the bucket tag the article carried in BazQux
    before, when a re-score moved it to another bucket, so the caller can remove it."""
    old = conn.execute("SELECT bucket, bucket_tagged FROM scores WHERE article_id = ?", (article_id,)).fetchone()
    stale = old["bucket"] if old is not None and old["bucket_tagged"] and old["bucket"] != bucket else None
    conn.execute(
        """INSERT INTO scores (article_id, prompt_version, model, quality, learning, fit, breadth, relevance,
                               base_score, score, lesson, concept, kind, flags, dupe_of, confidence,
                               bucket, bucket_tagged, scored_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
           ON CONFLICT(article_id) DO UPDATE SET
             prompt_version=excluded.prompt_version, model=excluded.model,
             quality=excluded.quality, learning=excluded.learning, fit=excluded.fit,
             breadth=excluded.breadth, relevance=excluded.relevance,
             base_score=excluded.base_score, score=excluded.score, lesson=excluded.lesson,
             concept=excluded.concept, kind=excluded.kind, flags=excluded.flags, dupe_of=excluded.dupe_of,
             confidence=excluded.confidence, bucket=excluded.bucket,
             bucket_tagged=CASE WHEN scores.bucket = excluded.bucket THEN scores.bucket_tagged ELSE 0 END,
             second_look_score=NULL, second_look_model=NULL, scored_at=excluded.scored_at""",
        (
            article_id, prompt_version, model,
            verdict["quality"], verdict["learning"], verdict["fit"], verdict["breadth"], verdict["relevance"],
            base_score, score, verdict.get("lesson", ""), verdict.get("concept", ""), verdict.get("kind", ""),
            json.dumps(verdict.get("flags", [])), verdict.get("dupe_of"), verdict.get("confidence", "medium"),
            bucket, utcnow_iso(),
        ),
    )
    conn.execute("UPDATE state SET judge_failures = 0 WHERE article_id = ? AND judge_failures > 0", (article_id,))
    return stale


def record_second_look(conn: sqlite3.Connection, article_id: str, second: float, model: str,
                       new_score: float, bucket: str) -> None:
    conn.execute(
        """UPDATE scores SET second_look_score = ?, second_look_model = ?, score = ?,
                  bucket_tagged = CASE WHEN bucket = ? THEN bucket_tagged ELSE 0 END, bucket = ?
           WHERE article_id = ?""",
        (second, model, new_score, bucket, bucket, article_id),
    )


def record_second_look_failure(conn: sqlite3.Connection, article_id: str) -> None:
    """The second look could not be had (a failed request, text that will not parse). The
    article counts as examined so it is not retried every run; the floor leaves it unread."""
    conn.execute("UPDATE scores SET second_look_model = 'failed' WHERE article_id = ? AND second_look_score IS NULL",
                 (article_id,))


def untagged_buckets(conn: sqlite3.Connection) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for row in conn.execute("SELECT article_id, bucket FROM scores WHERE bucket_tagged = 0 AND bucket IS NOT NULL"):
        out.setdefault(row["bucket"], []).append(row["article_id"])
    return out


def mark_bucket_tagged(conn: sqlite3.Connection, ids: Iterable[str]) -> None:
    conn.executemany("UPDATE scores SET bucket_tagged = 1 WHERE article_id = ?", [(i,) for i in ids])


# -- state --------------------------------------------------------------------

def set_flags(conn: sqlite3.Connection, ids: Iterable[str], **cols: Any) -> None:
    if not cols:
        return
    assignments = ", ".join(f"{k} = ?" for k in cols)
    rows = [(*cols.values(), i) for i in ids]
    if not rows:
        return
    with _bulk(conn):
        conn.executemany(f"UPDATE state SET {assignments} WHERE article_id = ?", rows)


def mark_cut(conn: sqlite3.Connection, ids: Iterable[str], run_id: int, mode: str) -> None:
    now = utcnow_iso()
    rows = [(run_id, mode, now, i) for i in ids]
    if not rows:
        return
    with _bulk(conn):
        conn.executemany(
            """UPDATE state SET status = 'cut', cut_run_id = ?, cut_mode = ?, cut_at = ?,
                                cut_tagged = 0, read_marked = 0 WHERE article_id = ?""",
            rows,
        )


@contextmanager
def _bulk(conn: sqlite3.Connection) -> Iterator[None]:
    """One transaction around a bulk write, unless the caller already holds one (then the
    rows join it): autocommit would fsync once per row, 3.5 s for 20,000 flags."""
    if conn.in_transaction:
        yield
        return
    with transaction(conn):
        yield


def cut_pending_now_exempt(conn: sqlite3.Connection) -> list[str]:
    """Cuts not yet written to BazQux whose article is starred or protected by now."""
    return [r[0] for r in conn.execute(
        "SELECT article_id FROM state WHERE status = 'cut' AND (cut_tagged = 0 OR read_marked = 0) "
        "AND (starred = 1 OR protected = 1)")]


def cut_pending(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """Cut articles still missing their tag, and those still not marked read."""
    untagged = [r[0] for r in conn.execute("SELECT article_id FROM state WHERE status='cut' AND cut_tagged=0")]
    unmarked = [r[0] for r in conn.execute("SELECT article_id FROM state WHERE status='cut' AND read_marked=0")]
    return untagged, unmarked


def run_cut_ids(conn: sqlite3.Connection, run_id: int) -> list[str]:
    return [r[0] for r in conn.execute("SELECT article_id FROM state WHERE cut_run_id = ?", (run_id,))]


def restore_unread(conn: sqlite3.Connection, ids: Iterable[str], rescued: bool, exempt: bool = False) -> None:
    """Back to unread. ``rescued`` stamps the student's rescue (the article is never cut again,
    by any path); ``exempt`` (an undo) keeps it clear of the automatic floor, so the next run
    does not repeat the cut that was just reversed; a prune may still take it."""
    now = utcnow_iso()
    conn.executemany(
        """UPDATE state SET status = 'unread', cut_run_id = NULL, cut_mode = NULL, cut_at = NULL,
                            cut_tagged = 0, read_marked = 0, rescued_at = COALESCE(?, rescued_at),
                            exempt = MAX(exempt, ?)
           WHERE article_id = ?""",
        [(now if rescued else None, 1 if exempt else 0, i) for i in ids],
    )


def status_ids(conn: sqlite3.Connection, status: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT article_id FROM state WHERE status = ?", (status,))}


def rescued_ids(conn: sqlite3.Connection) -> set[str]:
    """Articles the student rescued after a cut; no path cuts them again."""
    return {r[0] for r in conn.execute("SELECT article_id FROM state WHERE rescued_at IS NOT NULL")}


# -- runs and costs -----------------------------------------------------------

def begin_run(conn: sqlite3.Connection, kind: str) -> int:
    cur = conn.execute("INSERT INTO runs (kind, started_at, ok) VALUES (?, ?, 0)", (kind, utcnow_iso()))
    return int(cur.lastrowid or 0)


def finish_run(conn: sqlite3.Connection, run_id: int, ok: bool, **cols: Any) -> None:
    cols["finished_at"] = utcnow_iso()
    cols["ok"] = 1 if ok else 0
    assignments = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*cols.values(), run_id))


def last_runs(conn: sqlite3.Connection, n: int = 10, kind: str | None = None) -> list[sqlite3.Row]:
    if kind:
        return conn.execute("SELECT * FROM runs WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, n)).fetchall()
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def record_cost(conn: sqlite3.Connection, run_id: int | None, model: str, purpose: str,
                usage: dict, usd: float) -> None:
    conn.execute(
        """INSERT INTO costs (run_id, model, purpose, input_tokens, output_tokens, cache_write, cache_read,
                              usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, model, purpose, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
         usage.get("cache_write", 0), usage.get("cache_read", 0), usd, utcnow_iso()),
    )


def spent_since(conn: sqlite3.Connection, since_iso: str) -> float:
    row = conn.execute("SELECT COALESCE(SUM(usd), 0) FROM costs WHERE created_at >= ?", (since_iso,)).fetchone()
    return float(row[0] or 0.0) if row else 0.0


def local_midnight_iso(tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    local_now = datetime.now(tz)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).isoformat()


# -- feedback and concepts ----------------------------------------------------

# The kinds of feedback, and what they mean to the teacher and the weekly rewrite.
# Stars and reads come from BazQux state; the rest are the student's tags.
GRADES = ("great", "good", "meh", "bad")      # one per article; the harsher wins when two are present
LEVELS = ("basic", "heavy")                   # the level missed: too elementary, too dense
STEERS = ("more", "less")                     # steer the teacher by this article's concept, now
LOVED_KINDS = ("star", "great", "wrong")      # the strongest positives: the eval gate checks against these
POSITIVE_KINDS = ("star", "great", "good", "read", "rescue", "wrong")
NEGATIVE_KINDS = ("bad", "meh", "dismiss", "prune")


def add_feedback(conn: sqlite3.Connection, article_id: str, kind: str) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO feedback (article_id, kind, at) VALUES (?, ?, ?)", (article_id, kind, utcnow_iso())
    )
    return cur.rowcount > 0


def set_verdict(conn: sqlite3.Connection, article_id: str, kind: str, group: Iterable[str]) -> bool:
    """Record one kind out of an exclusive group (a grade, a level, a steer), replacing any other
    member recorded for the article. True when something changed."""
    others = [k for k in group if k != kind]
    if others:
        conn.execute(
            f"DELETE FROM feedback WHERE article_id = ? AND kind IN ({','.join('?' * len(others))})",
            (article_id, *others),
        )
    return add_feedback(conn, article_id, kind)


def verdicts(conn: sqlite3.Connection, kinds: Iterable[str]) -> dict[str, str]:
    """article id -> the recorded kind, for one exclusive group."""
    kinds = list(kinds)
    marks = ",".join("?" * len(kinds))
    return {r[0]: r[1] for r in conn.execute(f"SELECT article_id, kind FROM feedback WHERE kind IN ({marks})", kinds)}


def feedback_for(conn: sqlite3.Connection, article_id: str) -> list[tuple[str, str]]:
    """(kind, at) for one article, oldest first."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT kind, at FROM feedback WHERE article_id = ? ORDER BY at, id", (article_id,))]


def feedback_ids(conn: sqlite3.Connection, kind: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT article_id FROM feedback WHERE kind = ?", (kind,))}


def feedback_since(conn: sqlite3.Connection, kinds: Iterable[str], since_iso: str) -> list[sqlite3.Row]:
    kinds = list(kinds)
    marks = ",".join("?" * len(kinds))
    return conn.execute(
        f"""SELECT f.kind, f.at, a.id, a.title, a.feed_title, a.url, s.score, s.lesson, s.concept
            FROM feedback f JOIN articles a ON a.id = f.article_id LEFT JOIN scores s ON s.article_id = a.id
            WHERE f.kind IN ({marks}) AND f.at >= ? ORDER BY f.at DESC""",
        (*kinds, since_iso),
    ).fetchall()


def concept_counts(conn: sqlite3.Connection, kinds: Iterable[str], since_iso: str, limit: int = 40) -> list[tuple[str, int]]:
    kinds = list(kinds)
    marks = ",".join("?" * len(kinds))
    rows = conn.execute(
        f"""SELECT LOWER(TRIM(s.concept)) c, COUNT(*) n
            FROM feedback f JOIN scores s ON s.article_id = f.article_id
            WHERE f.kind IN ({marks}) AND f.at >= ? AND s.concept IS NOT NULL AND s.concept != ''
            GROUP BY c ORDER BY n DESC LIMIT ?""",
        (*kinds, since_iso, limit),
    ).fetchall()
    return [(r[0], int(r[1])) for r in rows]


def days_ago_iso(days: int) -> str:
    return (utcnow() - timedelta(days=days)).isoformat()


# -- batches ------------------------------------------------------------------

def add_batch(conn: sqlite3.Connection, batch_id: str, run_id: int | None, purpose: str, model: str,
              requests: list[tuple[str, list[str]]]) -> None:
    """Remember a submitted batch: (custom_id, ordered article ids) per request."""
    n_articles = sum(len(ids) for _, ids in requests)
    conn.execute(
        """INSERT INTO batches (id, run_id, purpose, model, n_requests, n_articles, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'submitted', ?)""",
        (batch_id, run_id, purpose, model, len(requests), n_articles, utcnow_iso()),
    )
    conn.executemany(
        """INSERT INTO inflight (article_id, purpose, batch_id, custom_id, ordinal) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(article_id, purpose) DO UPDATE SET batch_id=excluded.batch_id,
             custom_id=excluded.custom_id, ordinal=excluded.ordinal""",
        [(aid, purpose, batch_id, custom_id, n) for custom_id, ids in requests for n, aid in enumerate(ids, 1)],
    )


def open_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM batches WHERE status = 'submitted' ORDER BY created_at").fetchall()


def batch_members(conn: sqlite3.Connection, batch_id: str) -> dict[str, list[str]]:
    """custom_id -> article ids in request order."""
    out: dict[str, list[str]] = {}
    for row in conn.execute("SELECT custom_id, article_id FROM inflight WHERE batch_id = ? ORDER BY custom_id, ordinal",
                            (batch_id,)):
        out.setdefault(row["custom_id"], []).append(row["article_id"])
    return out


def close_batch(conn: sqlite3.Connection, batch_id: str, status: str, note: str = "") -> None:
    conn.execute("UPDATE batches SET status = ?, note = ?, ended_at = ? WHERE id = ?",
                 (status, note[:500], utcnow_iso(), batch_id))
    conn.execute("DELETE FROM inflight WHERE batch_id = ?", (batch_id,))


def inflight_ids(conn: sqlite3.Connection, purpose: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT article_id FROM inflight WHERE purpose = ?", (purpose,))}


def recent_batches(conn: sqlite3.Connection, n: int = 10) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (n,)).fetchall()


# -- learner versions and kv --------------------------------------------------

def save_learner_version(conn: sqlite3.Connection, text: str, reason: str) -> int:
    cur = conn.execute("INSERT INTO learner_versions (text, reason, created_at) VALUES (?, ?, ?)",
                       (text, reason, utcnow_iso()))
    return int(cur.lastrowid or 0)


def learner_versions(conn: sqlite3.Connection, n: int = 5) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM learner_versions ORDER BY version DESC LIMIT ?", (n,)).fetchall()


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def kv_delete(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("DELETE FROM kv WHERE key = ?", (key,)).rowcount > 0
