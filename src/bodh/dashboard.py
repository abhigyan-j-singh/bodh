"""The dashboard: one static HTML file, regenerated after every run, plus a weekly
Markdown note for Obsidian. For looking at the system, never for reading.
It reads only the SQLite file; if it breaks, nothing else does."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import html as ui
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


# -- rendering ------------------------------------------------------------------

_e = ui.esc
_href = ui.href


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:.0f}%"


def _bars_svg(rows: list[dict[str, Any]], keys: list[str], colors: list[str]) -> str:
    """Grouped bars per day, hand-rolled SVG, no scripts."""
    if not rows:
        return ""
    w, h, pad = 900, 220, 36
    maxv = max((r[k] for r in rows for k in keys), default=1) or 1
    gw = (w - 2 * pad) / len(rows)
    bw = gw / (len(keys) + 1)
    parts = [f"<svg viewBox='0 0 {w} {h}' width='100%' role='img' aria-label='Daily flow'>"]
    for i, r in enumerate(rows):
        x0 = pad + i * gw
        for j, k in enumerate(keys):
            v = r[k]
            bh = (h - 2 * pad) * v / maxv
            parts.append(
                f"<rect x='{x0 + j * bw:.1f}' y='{h - pad - bh:.1f}' width='{bw * 0.9:.1f}' height='{bh:.1f}' "
                f"fill='{colors[j]}'><title>{_e(r['day'])} {k}: {v}</title></rect>"
            )
        parts.append(
            f"<text x='{x0 + gw / 2:.1f}' y='{h - pad + 14}' font-size='10' text-anchor='middle' fill='var(--muted)'>"
            f"{_e(r['day'][5:])}</text>"
        )
    parts.append(f"<text x='{pad}' y='{pad - 10}' font-size='11' fill='var(--muted)'>max {maxv}</text>")
    legend_x = pad
    for j, k in enumerate(keys):
        parts.append(
            f"<rect x='{legend_x}' y='{h - 12}' width='10' height='10' fill='{colors[j]}'/>"
            f"<text x='{legend_x + 14}' y='{h - 3}' font-size='11' fill='var(--fg)'>{_e(k)}</text>"
        )
        legend_x += 14 + 7 * len(k) + 16
    parts.append("</svg>")
    return "".join(parts)


FLOW_KEYS = ["arrived", "cut by autocut", "cut by prune", "read", "starred"]
FLOW_COLORS = ["var(--pending)", "var(--flight)", "var(--prune)", "var(--read)", "var(--star)"]


def data(cfg: Config, conn: sqlite3.Connection) -> dict[str, Any]:
    """Every number the page shows, in one dict: the sections render from it and `data.json`
    carries a part of it."""
    tz = ZoneInfo(cfg.timezone)
    covered, frontier = concept_map(conn, cfg)
    versions = store.learned_versions(conn, 2)
    learned_diff = ""
    if len(versions) == 2:
        from .profiler import diff

        learned_diff = diff(versions[1]["text"], versions[0]["text"])
    return {
        "now": datetime.now(tz),
        "flow": flow(conn, tz),
        "calibration": calibration(conn),
        "verdicts": verdicts(conn),
        "disagreements": disagreements(conn),
        "near_misses": near_misses(conn),
        "feeds": feeds(conn),
        "learnings": learnings(conn),
        "covered": covered,
        "frontier": frontier,
        "health": health(conn, cfg),
        "learned_text": cfg.read_learned(),
        "learned_diff": learned_diff,
    }


def _section_head(cfg: Config, d: dict[str, Any]) -> str:
    hl = d["health"]
    last_ok = hl["last_ok"]
    status = (
        f"<span class='ok'>alive</span> last good run {_e(last_ok['finished_at'][:16])}"
        if last_ok
        else "<span class='bad'>no successful run yet</span>"
    )
    tiles = [ui.kpi("unread now", str(hl["unread"]))]
    tiles += [ui.kpi(str(b), str(n)) for b, n in sorted(hl["by_band"].items(), reverse=True)]
    tiles += [
        ui.kpi("spend today", f"${hl['spend_today']:.2f}"),
        ui.kpi("spend 7 days", f"${hl['spend_week']:.2f}"),
        f"<div class='kpi'><span class='muted'>runner</span><div>{status}</div></div>",
    ]
    return (
        f"<h1>bodh</h1><div class='muted'>as of {d['now'].strftime('%a %d %b %Y %H:%M')} {cfg.timezone} · "
        f"unread {hl['unread']} · prune keeps {cfg.prune.keep} · cut-off {cfg.autocut.cutoff}</div>"
        f"<div class='kpis'>{''.join(tiles)}</div>"
    )


def _section_progress(cfg: Config, conn: sqlite3.Connection) -> str:
    from . import progress

    try:
        return progress.render_html(progress.snapshot(conn, cfg))
    except Exception:  # the progress section must never break the page the rest of the run depends on
        log.exception("dashboard: the progress section failed to render")
        return "<p class='bad'>the progress section failed to render</p>"


def _section_flow(cfg: Config, d: dict[str, Any]) -> str:
    note = (
        "Read and starred are inferred from BazQux read state; a burst of more than "
        f"{cfg.safety.mark_all_read_threshold} articles going read at once is counted as a dismissal, not reading."
    )
    return ui.section("Flow, last 14 days", _bars_svg(d["flow"], FLOW_KEYS, FLOW_COLORS) + ui.muted(note))


def _section_calibration(d: dict[str, Any]) -> str:
    rows = [
        [_e(c["band"]), str(c["n"]), _pct(c["read_share"]), _pct(c["star_share"])] for c in d["calibration"]
    ]
    note = (
        "Higher bands should be read more. If 50 to 69 is read as often as 70 to 89, "
        "the mix of sub-scores in config.yaml is wrong."
    )
    return ui.section(
        "Calibration: how often each band gets read",
        ui.table(["Band", ("Left unread", "num"), ("Read", "num"), ("Starred", "num")], rows)
        + ui.muted(note),
    )


def _section_verdicts(d: dict[str, Any]) -> str:
    verd = d["verdicts"]
    if not any(r["n"] for r in verd):
        return ui.section(
            "Your verdicts, last 30 days",
            ui.muted(
                "No verdict tags yet. In BazQux, tag an article great, good, meh or bad; basic or heavy "
                "when the level missed; more or less to steer the judge by its concept."
            ),
        )
    rows = [[_e(r["band"]), str(r["great"]), str(r["good"]), str(r["meh"]), str(r["bad"])] for r in verd]
    inner = ui.table(
        ["The judge's band", ("great", "num"), ("good", "num"), ("meh", "num"), ("bad", "num")], rows
    )
    if d["disagreements"]:
        drows = [
            [
                _e(r["kind"]),
                f"{r['score']:.0f}",
                f"<a href='{_href(r['url'])}'>{_e((r['title'] or '')[:80])}</a> "
                f"<span class='muted'>{_e((r['feed_title'] or '')[:30])}</span>",
                _e(r["concept"] or ""),
            ]
            for r in d["disagreements"]
        ]
        inner += "<p>Where you and the judge disagree most:</p>" + ui.table(
            ["You", ("Score", "num"), "Article", "Concept"], drows
        )
    inner += ui.muted(
        "Tags in BazQux: great, good, meh, bad (one grade per article); basic or heavy when the level "
        "missed; more or less to steer the judge by this article's concept. The Sunday rewrite reads them all; "
        "disagreements are what it learns from most."
    )
    return ui.section("Your verdicts, last 30 days", inner)


def _section_near_misses(d: dict[str, Any]) -> str:
    rows = [
        [
            f"{r['score']:.0f}",
            f"<a href='{_href(r['url'])}'>{_e(r['title'][:90])}</a>"
            f"<div class='muted'>{_e(r['feed_title'])} · {_e(r['cut_mode'])}</div>",
            _e(r["lesson"]),
            _e(r["concept"]),
        ]
        for r in d["near_misses"]
    ]
    return ui.section(
        "Near misses: the best of what was cut this week",
        ui.table([("Score", "num"), "Article", "Lesson", "Concept"], rows)
        + ui.muted(
            "To rescue one, mark it unread in BazQux or your reader; the next run records the rescue."
        ),
    )


def _section_feeds(d: dict[str, Any]) -> str:
    rows = [
        [
            _e(r["feed_title"][:40]),
            _e(r["folder"] or ""),
            str(r["n"]),
            f"{r['mean_score']:.0f}" if r["mean_score"] is not None else "–",
            _pct(r["cut_share"]),
            _pct(r["read_share"]),
            _pct(r["star_share"]),
            _pct(r["rescue_share"]),
        ]
        for r in d["feeds"]
    ]
    columns: list[str | tuple[str, str]] = [
        "Feed",
        "Folder",
        ("Articles", "num"),
        ("Mean score", "num"),
        ("Cut", "num"),
        ("Read", "num"),
        ("Starred", "num"),
        ("Rescued", "num"),
    ]
    return ui.section("Feeds, last 30 days", ui.table(columns, rows))


def _section_learnings(d: dict[str, Any]) -> str:
    lessons = d["learnings"]
    if not lessons:
        return ui.section("Learnings this week", ui.muted("No reads recorded yet this week."))
    parts = []
    for concept, rows in lessons.items():
        items = "".join(
            f"<li><a href='{_href(r['url'])}'>{_e(r['title'][:80])}</a>: {_e(r['lesson'])}</li>" for r in rows
        )
        parts.append(f"<h3>{_e(concept)}</h3><ul>{items}</ul>")
    return ui.section("Learnings this week", "".join(parts))


def _section_map(d: dict[str, Any]) -> str:
    covered = d["covered"]
    inner = (
        "<p>" + " · ".join(f"<b>{_e(c)}</b> {n}" for c, n in covered) + "</p>"
        if covered
        else ui.muted("Nothing recorded yet.")
    )
    rows = [
        [
            f"{r['score']:.0f}",
            _e(r["concept"]),
            f"<a href='{_href(r['url'])}'>{_e(r['title'][:80])}</a> <span class='muted'>{_e(r['status'])}</span>",
        ]
        for r in d["frontier"]
    ]
    inner += "<h3>Frontier: high-scoring arrivals in fields you have not been reading</h3>" + ui.table(
        [("Score", "num"), "Concept", "Article"], rows
    )
    return ui.section("Map: concepts read in the last 90 days", inner)


def _section_profile(d: dict[str, Any]) -> str:
    inner = f"<pre>{_e(d['learned_text'])}</pre>"
    if d["learned_diff"]:
        inner += f"<h3>Change from the previous version</h3><pre>{_e(d['learned_diff'])}</pre>"
    return ui.section("Learned profile", inner)


def _section_health(d: dict[str, Any]) -> str:
    rows = [
        [
            _e((r["started_at"] or "")[:16]),
            _e(r["kind"]),
            "<span class='ok'>yes</span>" if r["ok"] else "<span class='bad'>no</span>",
            _e(r["summary"] or r["error"] or ""),
        ]
        for r in d["health"]["runs"]
    ]
    return ui.section("Health: last 20 runs", ui.table(["Started", "Kind", "OK", "Summary"], rows))


def render(cfg: Config, conn: sqlite3.Connection) -> Path:
    """dashboard/index.html and data.json, from one pass over the database."""
    from . import progress

    d = data(cfg, conn)
    body = "\n".join(
        [
            _section_head(cfg, d),
            _section_progress(cfg, conn),
            _section_flow(cfg, d),
            _section_calibration(d),
            _section_verdicts(d),
            _section_near_misses(d),
            _section_feeds(d),
            _section_learnings(d),
            _section_map(d),
            _section_profile(d),
            _section_health(d),
        ]
    )
    out_dir = cfg.root / cfg.dashboard_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    path.write_text(ui.page("bodh", body, css_extra=progress.PROGRESS_CSS), encoding="utf-8")
    hl = d["health"]
    (out_dir / "data.json").write_text(
        json.dumps(
            {
                "flow": d["flow"],
                "calibration": d["calibration"],
                "verdicts": d["verdicts"],
                "unread": hl["unread"],
                "spend_today": hl["spend_today"],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
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
