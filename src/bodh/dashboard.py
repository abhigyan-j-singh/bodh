"""The dashboard's export (the page as a static file, regenerated after every run, with its
document beside it), the weekly Markdown note for Obsidian, and the aggregates the note and
the feed report read. For looking at the system, never for reading. It reads only the SQLite
file; if it breaks, nothing else does."""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import store
from .config import Config

log = logging.getLogger(__name__)

BANDS = [(90, "90 to 100"), (70, "70 to 89"), (50, "50 to 69"), (30, "30 to 49"), (0, "0 to 29")]


def band_of(score: float) -> str:
    for lo, name in BANDS:
        if score >= lo:
            return name
    return BANDS[-1][1]


def _local_day(iso: str | None, tz: ZoneInfo) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone(tz).strftime("%Y-%m-%d")
    except ValueError:
        return None


# -- data ---------------------------------------------------------------------


def flow(conn: sqlite3.Connection, tz: ZoneInfo, days: int = 14) -> list[dict[str, Any]]:
    since = store.days_ago_iso(days)
    per: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in conn.execute("SELECT fetched_at FROM articles WHERE fetched_at >= ?", (since,)):
        d = _local_day(r[0], tz)
        if d:
            per[d]["arrived"] += 1
    for r in conn.execute("SELECT cut_at, cut_mode FROM state WHERE cut_at >= ?", (since,)):
        d = _local_day(r[0], tz)
        if d:
            per[d]["cut by autocut" if r[1] in ("autocut", "floor") else "cut by prune"] += 1
    for r in conn.execute(
        "SELECT at, kind FROM feedback WHERE at >= ? AND kind IN ('read','star')", (since,)
    ):
        d = _local_day(r[0], tz)
        if d:
            per[d]["read" if r[1] == "read" else "starred"] += 1
    today = datetime.now(tz).date()
    out = []
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        row: dict[str, Any] = {"day": day}
        for k in ("arrived", "cut by autocut", "cut by prune", "read", "starred"):
            row[k] = per[day][k]
        out.append(row)
    return out


def calibration(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Among articles the system left unread for at least a day, how often each band was read or starred."""
    cutoff = store.days_ago_iso(1)
    rows = conn.execute(
        """SELECT s.score, st.status,
                  EXISTS(SELECT 1 FROM feedback f WHERE f.article_id = s.article_id AND f.kind = 'read') AS was_read,
                  EXISTS(SELECT 1 FROM feedback f WHERE f.article_id = s.article_id AND f.kind = 'star') AS was_starred
           FROM scores s JOIN state st ON st.article_id = s.article_id
           WHERE s.scored_at <= ? AND (st.cut_mode IS NULL OR st.status IN ('read','dismissed','unread','expired'))""",
        (cutoff,),
    ).fetchall()
    agg: dict[str, dict[str, int]] = {name: {"n": 0, "read": 0, "starred": 0} for _, name in BANDS}
    for r in rows:
        b = band_of(float(r["score"]))
        agg[b]["n"] += 1
        agg[b]["read"] += int(r["was_read"])
        agg[b]["starred"] += int(r["was_starred"])
    out = []
    for _, name in BANDS:
        a = agg[name]
        out.append(
            {
                "band": name,
                "n": a["n"],
                "read_share": (a["read"] / a["n"]) if a["n"] else None,
                "star_share": (a["starred"] / a["n"]) if a["n"] else None,
            }
        )
    return out


BY_MODEL_MIN_N = 20  # a model with fewer judgments in the window has no row: too few to read anything into


def by_model(conn: sqlite3.Connection, days: int = 30, min_n: int = BY_MODEL_MIN_N) -> list[dict[str, Any]]:
    """Per model that judged at least `min_n` articles in the window: how many, the mean score,
    the share of its unread-for-a-day articles that were read or starred, and the student's
    grades for and against. Empty when one model judged everything: the main tables say it all."""
    since = store.days_ago_iso(days)
    rows = conn.execute(
        """SELECT s.model, COUNT(*) n, AVG(s.score) mean_score,
                  SUM(EXISTS(SELECT 1 FROM feedback f WHERE f.article_id = s.article_id AND f.kind = 'read')) was_read,
                  SUM(EXISTS(SELECT 1 FROM feedback f WHERE f.article_id = s.article_id AND f.kind = 'star')) was_starred,
                  SUM(EXISTS(SELECT 1 FROM feedback f WHERE f.article_id = s.article_id AND f.kind IN ('great', 'good'))) liked,
                  SUM(EXISTS(SELECT 1 FROM feedback f WHERE f.article_id = s.article_id AND f.kind IN ('meh', 'bad'))) disliked
           FROM scores s WHERE s.scored_at >= ? AND s.model NOT IN ('dedup', 'rebuilt')
           GROUP BY s.model HAVING COUNT(*) >= ? ORDER BY n DESC""",
        (since, min_n),
    ).fetchall()
    if len(rows) < 2:
        return []
    return [
        {
            "model": r["model"],
            "n": int(r["n"]),
            "mean_score": round(float(r["mean_score"] or 0), 1),
            "read_share": int(r["was_read"] or 0) / int(r["n"]),
            "star_share": int(r["was_starred"] or 0) / int(r["n"]),
            "liked": int(r["liked"] or 0),
            "disliked": int(r["disliked"] or 0),
        }
        for r in rows
    ]


def verdicts(conn: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    """The student's grades against the judge's bands: for each band, how many articles the
    student marked great, good, meh or bad in the window. Calibration from the student's own hand."""
    since = store.days_ago_iso(days)
    marks = ",".join("?" * len(store.GRADES))
    rows = conn.execute(
        f"""SELECT f.kind, s.score FROM feedback f JOIN scores s ON s.article_id = f.article_id
            WHERE f.kind IN ({marks}) AND f.at >= ?""",
        (*store.GRADES, since),
    ).fetchall()
    agg: dict[str, dict[str, int]] = {name: {k: 0 for k in store.GRADES} for _, name in BANDS}
    for r in rows:
        agg[band_of(float(r["score"]))][r["kind"]] += 1
    return [{"band": name, **agg[name], "n": sum(agg[name].values())} for _, name in BANDS]


def disagreements(conn: sqlite3.Connection, days: int = 30, limit: int = 15) -> list[sqlite3.Row]:
    """Where the student and the judge disagree most: great, good or wrong under 50; bad or meh at 70 or more."""
    since = store.days_ago_iso(days)
    return conn.execute(
        """SELECT a.id, a.title, a.url, a.feed_title, s.score, s.concept, s.lesson, f.kind, f.at
           FROM feedback f JOIN scores s ON s.article_id = f.article_id JOIN articles a ON a.id = f.article_id
           WHERE f.at >= ? AND ((f.kind IN ('great', 'good', 'wrong') AND s.score < 50)
                                OR (f.kind IN ('bad', 'meh') AND s.score >= 70))
           ORDER BY ABS(s.score - 50) DESC, f.at DESC LIMIT ?""",
        (since, limit),
    ).fetchall()


def near_misses(conn: sqlite3.Connection, days: int = 7, limit: int = 20) -> list[sqlite3.Row]:
    since = store.days_ago_iso(days)
    return conn.execute(
        """SELECT a.id, a.title, a.url, a.feed_title, s.score, s.lesson, s.concept, st.cut_mode
           FROM state st JOIN articles a ON a.id = st.article_id JOIN scores s ON s.article_id = a.id
           WHERE st.status = 'cut' AND st.cut_at >= ? ORDER BY s.score DESC LIMIT ?""",
        (since, limit),
    ).fetchall()


def feeds(conn: sqlite3.Connection, days: int = 30, limit: int = 40) -> list[sqlite3.Row]:
    since = store.days_ago_iso(days)
    return conn.execute(
        """SELECT a.feed_title, a.folder, COUNT(*) n, COUNT(s.score) judged, AVG(s.score) mean_score,
                  AVG(CASE WHEN st.status = 'cut' AND st.cut_mode NOT IN ('clear-old', 'bankruptcy') THEN 1.0 ELSE 0.0 END) cut_share,
                  AVG(CASE WHEN EXISTS(SELECT 1 FROM feedback f WHERE f.article_id = a.id AND f.kind='read') THEN 1.0 ELSE 0.0 END) read_share,
                  AVG(CASE WHEN EXISTS(SELECT 1 FROM feedback f WHERE f.article_id = a.id AND f.kind='star') THEN 1.0 ELSE 0.0 END) star_share,
                  AVG(CASE WHEN EXISTS(SELECT 1 FROM feedback f WHERE f.article_id = a.id AND f.kind='rescue') THEN 1.0 ELSE 0.0 END) rescue_share
           FROM articles a JOIN state st ON st.article_id = a.id LEFT JOIN scores s ON s.article_id = a.id
           WHERE a.fetched_at >= ? AND a.feed_title IS NOT NULL AND a.feed_title != ''
           GROUP BY a.feed_title ORDER BY n DESC LIMIT ?""",
        (since, limit),
    ).fetchall()


def learnings(conn: sqlite3.Connection, days: int = 7) -> dict[str, list[sqlite3.Row]]:
    rows = store.feedback_since(conn, ["read", "star", "great", "good"], store.days_ago_iso(days))
    seen: set[str] = set()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        if r["id"] in seen or not r["lesson"]:
            continue
        seen.add(r["id"])
        grouped[(r["concept"] or "other").lower()].append(r)
    return dict(sorted(grouped.items(), key=lambda kv: -len(kv[1])))


def concept_map(conn: sqlite3.Connection, cfg: Config) -> tuple[list[tuple[str, int]], list[sqlite3.Row]]:
    since = store.days_ago_iso(cfg.profile.concept_window_days)
    covered = store.concept_counts(conn, ["star", "read", "rescue"], since, limit=30)
    known = {c for c, _ in store.concept_counts(conn, ["star", "read", "rescue"], since, limit=500)}
    recent = store.days_ago_iso(14)
    rows = conn.execute(
        """SELECT a.id, a.title, a.url, s.score, s.concept, st.status FROM scores s
           JOIN articles a ON a.id = s.article_id JOIN state st ON st.article_id = a.id
           WHERE s.scored_at >= ? AND s.score >= 70 AND s.concept IS NOT NULL AND s.concept != ''
           ORDER BY s.score DESC""",
        (recent,),
    ).fetchall()
    frontier: list[sqlite3.Row] = []
    seen: set[str] = set()
    for r in rows:
        c = (r["concept"] or "").strip().lower()
        if c in known or c in seen:
            continue
        seen.add(c)
        frontier.append(r)
        if len(frontier) >= 15:
            break
    return covered, frontier


def health(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    runs = store.last_runs(conn, 20)
    today = store.spent_since(conn, store.local_midnight_iso(cfg.timezone))
    week = store.spent_since(conn, store.days_ago_iso(7))
    last_ok = next((r for r in runs if r["ok"] and r["kind"] == "run"), None)
    last_err = next((r for r in runs if not r["ok"] and r["error"]), None)
    unread = conn.execute("SELECT COUNT(*) FROM state WHERE status = 'unread'").fetchone()[0]
    by_band = conn.execute(
        "SELECT s.bucket, COUNT(*) FROM scores s JOIN state st ON st.article_id = s.article_id "
        "WHERE st.status = 'unread' GROUP BY s.bucket"
    ).fetchall()
    return {
        "runs": runs,
        "spend_today": today,
        "spend_week": week,
        "last_ok": last_ok,
        "last_error": last_err,
        "unread": unread,
        "by_band": {r[0]: r[1] for r in by_band},
    }


# -- the export ------------------------------------------------------------------


def render(cfg: Config, conn: sqlite3.Connection) -> Path:
    """dashboard/index.html, the page the console serves without its levers and with the
    document embedded; data.json, the same document, for a script or a shelf; and the
    progress.json of docs/json.md, fresh beside them."""
    from . import progress
    from .web import document, panels

    doc = document.build(cfg, conn, "export")
    out_dir = cfg.root / cfg.dashboard_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    path.write_text(panels.render(doc, "export"), encoding="utf-8")
    (out_dir / "data.json").write_text(doc.to_json(), encoding="utf-8")
    progress.write_live(cfg, conn)
    return path


def weekly_note(
    cfg: Config, conn: sqlite3.Connection, learned_result: dict[str, Any] | None = None
) -> Path | None:
    """The weekly digest as Markdown: learnings, map, near misses, the learned profile's diff."""
    target = cfg.obsidian_path
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    lessons = learnings(conn)
    covered, frontier = concept_map(conn, cfg)
    misses = near_misses(conn)
    hl = health(conn, cfg)
    lines = [
        f"# Bodh weekly · {now.strftime('%d %b %Y')}",
        "",
        f"Unread now: {hl['unread']} (target {cfg.prune.keep}). Spend last 7 days: ${hl['spend_week']:.2f}.",
        "",
    ]
    lines.append("## Learnings this week")
    if not lessons:
        lines.append("No reads recorded this week.")
    for concept, rows in lessons.items():
        lines.append(f"### {concept}")
        lines += [f"- [{r['title'][:80]}]({r['url']}): {r['lesson']}" for r in rows]
    lines += ["", "## Map, last 90 days", ", ".join(f"{c} ({n})" for c, n in covered) or "nothing yet", ""]
    lines.append("## Frontier: fields you have not been reading")
    lines += [f"- {r['score']:.0f} · {r['concept']} · [{r['title'][:80]}]({r['url']})" for r in frontier] or [
        "nothing yet"
    ]
    lines += ["", "## Near misses: the best of what was cut"]
    lines += [f"- {r['score']:.0f} · [{r['title'][:80]}]({r['url']}) · {r['lesson']}" for r in misses] or [
        "nothing cut this week"
    ]
    if learned_result:
        lines += ["", "## Learned profile", learned_result.get("reason", "")]
        if learned_result.get("diff"):
            lines += ["```diff", learned_result["diff"], "```"]
    text = "\n".join(lines) + "\n"
    out_dir = cfg.root / cfg.dashboard_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "weekly.md").write_text(text, encoding="utf-8")
    if target is None:
        return out_dir / "weekly.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
