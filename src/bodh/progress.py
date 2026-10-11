"""Where the work stands: how many articles the judge has judged, what waits, what
is with the model, how fast it is moving, what it costs. One set of queries, rendered for
the terminal (`bodh progress`, with --watch) and for the dashboard, whose progress
page polls `progress.json` every few seconds while a run writes it."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import html as ui
from . import locks, store
from .config import Config

log = logging.getLogger(__name__)

BAR = "█"
PAD = "░"
LIVE_FILE = "progress.json"
SERIES_BIN_MINUTES = 5
SERIES_HOURS = 3


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _age(iso: str | None) -> str:
    t = _parse(iso)
    if t is None:
        return "?"
    mins = int((store.utcnow() - t).total_seconds() // 60)
    if mins < 60:
        return f"{mins} min"
    if mins < 48 * 60:
        return f"{mins // 60} h {mins % 60:02d}"
    return f"{mins // 1440} d"


# -- activity: how fast is it moving ------------------------------------------


def activity(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """Judged per minute (last 2 minutes, else last 10), a 3-hour series in 5-minute
    bins, the phase a run is in, and whether a run holds the lock right now."""
    now = store.utcnow()
    two = _count(
        conn,
        "SELECT COUNT(*) FROM scores WHERE scored_at >= ? AND model != 'dedup'",
        ((now - timedelta(minutes=2)).isoformat(),),
    )
    ten = _count(
        conn,
        "SELECT COUNT(*) FROM scores WHERE scored_at >= ? AND model != 'dedup'",
        ((now - timedelta(minutes=10)).isoformat(),),
    )
    rate = two / 2.0 if two else ten / 10.0
    bins = SERIES_HOURS * 60 // SERIES_BIN_MINUTES
    start = now - timedelta(minutes=bins * SERIES_BIN_MINUTES)
    counts = [0] * bins
    for (iso,) in conn.execute(
        "SELECT scored_at FROM scores WHERE scored_at >= ? AND model != 'dedup'", (start.isoformat(),)
    ):
        t = _parse(iso)
        if t is None:
            continue
        i = int((t - start).total_seconds() // (SERIES_BIN_MINUTES * 60))
        if 0 <= i < bins:
            counts[i] += 1
    data_dir = cfg.root / cfg.data_dir
    proc = locks.running_process(data_dir)  # the pid file is the one source of "since when"
    running = proc is not None
    run_started = datetime.fromtimestamp(proc["started"], tz=timezone.utc).isoformat() if proc else None
    phase = store.kv_get(conn, store.PHASE_KEY) if running else None
    # no log tail here: this dict is written to progress.json and the dashboard, which may be
    # published; the terminal and the console fetch the log on their own (log_tail)
    return {
        "rate_per_min": rate,
        "judged_2min": two,
        "judged_10min": ten,
        "series": counts,
        "series_bin_minutes": SERIES_BIN_MINUTES,
        "series_start": start.isoformat(),
        "running": running,
        "run_started": run_started,
        "phase": phase or "",
    }


def set_phase(conn: sqlite3.Connection, phase: str) -> None:
    """What a run is doing now, for the live views ('judging 240 of 1,200', say)."""
    store.kv_set(conn, store.PHASE_KEY, phase)


def log_tail(path: Path, n: int = 10) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 16000))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line[:200] for line in lines[-n:]]


# -- the snapshot -------------------------------------------------------------


def snapshot(conn: sqlite3.Connection, cfg: Config, live: bool = True) -> dict[str, Any]:
    """Everything the progress views show, as plain numbers: the counts, the money, the
    folders, the batches, the last runs, and (``live``) the run in progress."""
    counts = _counts(conn, cfg)
    snap: dict[str, Any] = {
        "at": store.utcnow_iso(),
        "now": datetime.now(ZoneInfo(cfg.timezone)),
        "batch_mode": cfg.models.batch,
        "max_age_days": cfg.limits.max_age_days,
        "skip_folders": cfg.unjudged_folders(),
        "teacher": cfg.models.judge,
        "budget": cfg.models.daily_budget_usd,
        **counts,
        **_money(conn, cfg),
        "folders": _folders(conn, cfg, counts["_pending_rows"], counts["_inflight_judge"]),
        "batches": _batches(conn),
        "runs": [dict(r) for r in store.last_runs(conn, 3)],
    }
    del snap["_pending_rows"], snap["_inflight_judge"]
    if live:
        snap.update(activity(conn, cfg))
        rate = snap["rate_per_min"]
        snap["eta_min"] = (snap["pending"] / rate) if rate > 0 and snap["pending"] else None
    return snap


def _counts(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """What is unread, judged, pending, with the model, too old, skipped, held, given up."""
    version = cfg.judge.prompt_version
    skip = cfg.unjudged_folders()
    unread = _count(conn, "SELECT COUNT(*) FROM state WHERE status = 'unread'")
    judged_unread = _count(
        conn,
        "SELECT COUNT(*) FROM state st JOIN scores s ON s.article_id = st.article_id "
        "WHERE st.status = 'unread' AND s.prompt_version = ?",
        (version,),
    )
    pending_rows = store.unscored_unread_folders(
        conn, version, cfg.limits.max_age_days, skip, exclude_inflight=True
    )
    pending = len(pending_rows)
    unscored_all = store.unscored_unread_count(conn, version)  # leaves out what the judge gave up on
    inflight_judge = store.inflight_ids(conn, "teacher")
    skipped = (
        _count(
            conn,
            f"SELECT COUNT(*) FROM articles a JOIN state st ON st.article_id = a.id "
            f"WHERE st.status = 'unread' AND a.folder IN ({','.join('?' * len(skip))})",
            tuple(skip),
        )
        if skip
        else 0
    )
    unread_scores = "SELECT COUNT(*) FROM scores s JOIN state st ON st.article_id = s.article_id WHERE st.status = 'unread'"
    stand_in = _count(conn, unread_scores + " AND s.model != ? AND s.model != 'dedup'", (cfg.models.judge,))
    rejudge_rule = (
        None if cfg.catch_up.rejudge_at is None else (cfg.models.judge, float(cfg.catch_up.rejudge_at))
    )
    rejudge = 0
    if rejudge_rule and stand_in:
        rejudge = (
            store.unscored_unread_count(
                conn, version, cfg.limits.max_age_days, skip, exclude_inflight=True, rejudge=rejudge_rule
            )
            - pending
        )
    return {
        "unread": unread,
        "judged_unread": judged_unread,
        "pending": pending,
        "inflight": len(inflight_judge),
        "inflight_second": len(store.inflight_ids(conn, "second_look")),
        "too_old": max(0, unscored_all - pending - len(inflight_judge) - skipped),
        "skipped": skipped,
        "low_conf": _count(conn, unread_scores + " AND s.confidence = 'low'"),
        "held": _count(conn, unread_scores + " AND s.second_look_model = 'failed'"),
        "given_up": store.given_up(conn),
        "stand_in": stand_in,
        "rejudge": max(0, rejudge),
        "_pending_rows": pending_rows,
        "_inflight_judge": inflight_judge,
    }


def _period(conn: sqlite3.Connection, since: str) -> dict[str, Any]:
    return {
        "arrived": _count(conn, "SELECT COUNT(*) FROM articles WHERE fetched_at >= ?", (since,)),
        "judged": _count(
            conn, "SELECT COUNT(*) FROM scores WHERE scored_at >= ? AND model != 'dedup'", (since,)
        ),
        "dupes": _count(
            conn, "SELECT COUNT(*) FROM scores WHERE scored_at >= ? AND model = 'dedup'", (since,)
        ),
        "second_looks": _count(
            conn, "SELECT COUNT(*) FROM costs WHERE created_at >= ? AND purpose = 'second_look'", (since,)
        ),
        "cut": _count(conn, "SELECT COUNT(*) FROM state WHERE cut_at >= ?", (since,)),
        "spend": store.spent_since(conn, since),
    }


def _money(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """Today, seven days, thirty days and all time: arrivals, judgments, cuts and spend; the
    cache hit share and the cost per 1,000 judged over the week."""
    week = store.days_ago_iso(7)
    seven = _period(conn, week)
    tokens = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read),0), COALESCE(SUM(cache_write),0), "
        "COALESCE(SUM(output_tokens),0) FROM costs WHERE created_at >= ? AND purpose = 'teacher'",
        (week,),
    ).fetchone()
    prompt_total = tokens[0] + tokens[1] + tokens[2]
    return {
        "today": _period(conn, store.local_midnight_iso(cfg.timezone)),
        "week": seven,
        "month": _period(conn, store.days_ago_iso(30)),
        "all_time": {
            "judged": _count(conn, "SELECT COUNT(*) FROM scores WHERE model != 'dedup'"),
            "dupes": _count(conn, "SELECT COUNT(*) FROM scores WHERE model = 'dedup'"),
            "cut": _count(conn, "SELECT COUNT(*) FROM state WHERE status = 'cut'"),
            "spend": store.spent_since(conn, "0000"),
        },
        "cache_hit": (tokens[1] / prompt_total) if prompt_total else None,
        "per_1000": (seven["spend"] / seven["judged"] * 1000) if seven["judged"] else None,
        "output_tokens_week": tokens[3],
    }


def _folders(
    conn: sqlite3.Connection, cfg: Config, pending_rows: list[sqlite3.Row], inflight_judge: list[str]
) -> list[dict[str, Any]]:
    """Per folder: unread, judged, 70 and above, cut, pending, with the model, the cut-off."""
    skip = cfg.unjudged_folders()
    # the cuts first (every folder, a year of them, counted through the status index), then
    # the unread pile, which is small: one pass over the whole table per phase was 130 ms at
    # 50,000 articles
    cuts = {
        r["folder"]: int(r["n"] or 0)
        for r in conn.execute(
            "SELECT a.folder, COUNT(*) n FROM state st JOIN articles a ON a.id = st.article_id "
            "WHERE st.status = 'cut' GROUP BY a.folder"
        )
    }
    folders = []
    seen: set[str | None] = set()
    for r in conn.execute(
        """SELECT a.folder,
                  COUNT(*) unread,
                  SUM(s.prompt_version = ?) judged,
                  SUM(s.score >= 70) good
           FROM state st JOIN articles a ON a.id = st.article_id LEFT JOIN scores s ON s.article_id = a.id
           WHERE st.status = 'unread'
           GROUP BY a.folder ORDER BY unread DESC""",
        (cfg.judge.prompt_version,),
    ):
        seen.add(r["folder"])
        folders.append(
            _folder_row(cfg, skip, r["folder"], r["unread"], r["judged"], r["good"], cuts.get(r["folder"], 0))
        )
    for folder, n in cuts.items():
        if folder not in seen:
            folders.append(_folder_row(cfg, skip, folder, 0, 0, 0, n))
    by_name: dict[str, dict[str, Any]] = {str(f["folder"]): f for f in folders}
    for row in pending_rows:
        f = by_name.get(row["folder"] or "(no folder)")
        if f:
            f["pending"] += 1
    if inflight_judge:
        for r in store.fetch_in(
            conn,
            "SELECT folder, COUNT(*) n FROM articles WHERE id IN ({marks}) GROUP BY folder",
            inflight_judge,
        ):
            f = by_name.get(r["folder"] or "(no folder)")
            if f:
                f["inflight"] += int(r["n"])
    return folders


def _folder_row(
    cfg: Config, skip: Iterable[str], folder: str | None, unread: Any, judged: Any, good: Any, cut: int
) -> dict[str, Any]:
    return {
        "folder": folder or "(no folder)",
        "unread": int(unread or 0),
        "judged": int(judged or 0),
        "good": int(good or 0),
        "cut": cut,
        "pending": 0,
        "inflight": 0,
        "floor": cfg.cutoff_for(folder),
        "scored": (folder or "") not in skip,
    }


def _batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    batches = [dict(b) for b in store.recent_batches(conn, 8)]
    for b in batches:
        b["age"] = _age(b["created_at"])
        b["took"] = _age(b["ended_at"]) if b.get("ended_at") else ""
    return batches


# -- the live file ------------------------------------------------------------

_last_live_write = [0.0]  # when the live file was last written (monotonic), for the throttle


def write_live(
    cfg: Config, conn: sqlite3.Connection, min_interval: float = 0.0, snap: dict[str, Any] | None = None
) -> Path | None:
    """progress.json next to the dashboard, for its progress section to poll. Never raises.
    A caller that just took a snapshot passes it, so the database is not walked twice."""
    now = time.monotonic()
    if min_interval and now - _last_live_write[0] < min_interval:
        return None
    try:
        snap = dict(snap) if snap is not None else snapshot(conn, cfg)
        snap["now"] = snap["now"].isoformat() if not isinstance(snap["now"], str) else snap["now"]
        out_dir = cfg.root / cfg.dashboard_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / LIVE_FILE
        tmp = (
            out_dir / f".{LIVE_FILE}.{os.getpid()}.tmp"
        )  # a console child beside a run: two writers, two files
        tmp.write_text(json.dumps(snap, default=str), encoding="utf-8")
        os.replace(tmp, path)
        _last_live_write[0] = now
        return path
    except Exception:  # the live file is a convenience for the page: a run must never fail over it
        log.warning("progress: could not write %s", LIVE_FILE, exc_info=True)
        return None


def live_writer(
    cfg: Config, conn: sqlite3.Connection, min_interval: float = 2.0
) -> Callable[[dict[str, Any]], None]:
    """A callback for the judge: write the live file at most every few seconds."""

    def _cb(_stats: dict[str, Any] | None = None) -> None:
        write_live(cfg, conn, min_interval=min_interval)

    return _cb


# -- terminal -----------------------------------------------------------------


def _bar(share: float, width: int = 30) -> str:
    share = max(0.0, min(1.0, share))
    full = round(share * width)
    return BAR * full + PAD * (width - full)


def _n(x: int | float | None) -> str:
    return "–" if x is None else f"{x:,}"


def _eta(minutes: float | None) -> str:
    if minutes is None:
        return "–"
    if minutes < 1:
        return "under a minute"
    if minutes < 90:
        return f"{minutes:.0f} min"
    return f"{minutes / 60:.1f} h"


def render_text(snap: dict[str, Any], width: int | None = None) -> str:  # width: reserved; terminals wrap
    """The terminal view: the state, the counts, the money, the folders, the batches, the log,
    the last runs."""
    lines = [
        *_text_state(snap),
        "",
        *_text_counts(snap),
        "",
        *_text_money(snap),
        "",
        *_text_folders(snap),
        "",
    ]
    if snap["batches"]:
        lines += [*_text_batches(snap), ""]
    if snap.get("log"):
        lines += ["Log", *[f"  {line}" for line in snap["log"][-6:]], ""]
    if snap["runs"]:
        lines.append("Last runs")
        for r in snap["runs"]:
            ok = "ok " if r["ok"] else "ERR"
            lines.append(
                f"  {(r['started_at'] or '')[:16]} {ok} {r['kind']:<10} {(r['summary'] or r['error'] or '')}"
            )
    return "\n".join(lines)


def _text_state(snap: dict[str, Any]) -> list[str]:
    mode = "batch mode: judgments arrive on the next run" if snap["batch_mode"] else "synchronous mode"
    now = snap["now"] if isinstance(snap["now"], datetime) else datetime.fromisoformat(snap["now"])
    lines = [f"bodh progress · {now.strftime('%a %d %b %H:%M:%S')} · {mode}"]
    if "running" not in snap:
        return lines
    if snap["running"]:
        since = f" for {_age(snap['run_started'])}" if snap.get("run_started") else ""
        lines.append(
            f"● RUN IN PROGRESS{since} · {snap.get('phase') or 'working'} · "
            f"{snap['rate_per_min']:.0f} judged/min · ETA for pending {_eta(snap.get('eta_min'))}"
        )
    else:
        lines.append(f"○ idle · last 10 min: {_n(snap['judged_10min'])} judged")
    return lines


def _text_counts(snap: dict[str, Any]) -> list[str]:
    unread = snap["unread"] or 1
    judged, pending, inflight = snap["judged_unread"], snap["pending"], snap["inflight"]
    in_window = judged + pending + inflight
    lines = [
        f"Unread known to bodh {_n(snap['unread']):>10}",
        f"  judged               {_n(judged):>10}  {_bar(judged / unread)} {judged / unread:4.0%}",
    ]
    if inflight:
        lines.append(
            f"  with the model       {_n(inflight):>10}  {_bar(inflight / unread)} waiting for the batch to finish"
        )
    window = f"(≤{snap['max_age_days']} days)" if snap["max_age_days"] else "(any age)"
    lines.append(f"  pending {window:<13}{_n(pending):>10}  {_bar(pending / unread)}")
    if snap["too_old"]:
        lines.append(
            f"  older than {snap['max_age_days']} days  {_n(snap['too_old']):>10}  never judged; "
            f"`bodh clear-old --days {snap['max_age_days']}` clears them"
        )
    if snap["skipped"]:
        lines.append(f"  not judged (folders) {_n(snap['skipped']):>10}  {', '.join(snap['skip_folders'])}")
    if in_window:
        lines.append(f"  of the current window: {judged / in_window:.0%} judged")
    notes = _text_notes(snap)
    if notes:
        lines.append("  " + " · ".join(notes))
    return lines


def _text_notes(snap: dict[str, Any]) -> list[str]:
    notes = []
    if snap.get("stand_in"):
        notes.append(
            f"{_n(snap['stand_in'])} judged by the quick judge; the judge re-judges {_n(snap['rejudge'])} of them (50+)"
        )
    if snap["low_conf"]:
        notes.append(f"{_n(snap['low_conf'])} judged with low confidence (left alone)")
    if snap["inflight_second"]:
        notes.append(f"{_n(snap['inflight_second'])} reviews with the model")
    if snap["held"]:
        notes.append(f"{_n(snap['held'])} kept after a failed review")
    if snap.get("given_up"):
        notes.append(f"{_n(snap['given_up'])} given up on after answers with no judgment (`bodh safety`)")
    return notes


def _text_money(snap: dict[str, Any]) -> list[str]:
    t, w, a = snap["today"], snap["week"], snap["all_time"]
    per = f"${snap['per_1000']:.2f} per 1,000 judged" if snap["per_1000"] is not None else "no spend yet"
    cache = (
        f"cache hits {snap['cache_hit']:.0%} of prompt tokens"
        if snap["cache_hit"] is not None
        else "no cache data yet"
    )
    lines = [
        f"Today     arrived {_n(t['arrived'])} · judged {_n(t['judged'])} (+{_n(t['dupes'])} dupes) · "
        f"reviews {_n(t['second_looks'])} · cut {_n(t['cut'])}",
        f"7 days    arrived {_n(w['arrived'])} · judged {_n(w['judged'])} · reviews {_n(w['second_looks'])}"
        f" · cut {_n(w['cut'])}",
        f"All time  judged {_n(a['judged'])} (+{_n(a['dupes'])} dupes) · cut {_n(a['cut'])}",
        f"Spend     today ${t['spend']:.2f} of ${snap['budget']:.2f} · 7 days ${w['spend']:.2f} · "
        f"all time ${a['spend']:.2f} · {per} · {cache}",
    ]
    if snap.get("series"):
        lines.append(
            "Last 3 h  "
            + _sparkline(snap["series"])
            + f"  (judged per {snap['series_bin_minutes']} min, max "
            f"{max(snap['series'])})"
        )
    return lines


def _text_folders(snap: dict[str, Any]) -> list[str]:
    lines = [
        f"{'By folder':<32}{'unread':>8}{'judged':>8}{'pending':>9}{'model':>8}{'≥70':>6}{'cut':>7}  cut-off"
    ]
    for f in snap["folders"][:30]:
        if not f["unread"] and not f["cut"]:
            continue
        cutoff = "never" if f["floor"] is None else (f"{f['floor']}" if f["scored"] else "not judged")
        lines.append(
            f"  {f['folder'][:29]:<30}{_n(f['unread']):>8}{_n(f['judged']):>8}{_n(f['pending']):>9}"
            f"{_n(f['inflight']):>8}{_n(f['good']):>6}{_n(f['cut']):>7}  {cutoff}"
        )
    return lines


def _text_batches(snap: dict[str, Any]) -> list[str]:
    lines = ["Batches"]
    for b in snap["batches"]:
        state = b["status"]
        if state == "submitted":
            state = f"in progress, {b['age']} ago"
        elif b.get("took"):
            state = f"{state}, finished {b['took']} ago"
        lines.append(
            f"  {b['id'][:22]:<24}{b['purpose']:<12}{_n(b['n_articles']):>6} articles in "
            f"{_n(b['n_requests']):>3} requests  {state}"
        )
        if b.get("note"):
            lines.append(f"{'':<26}{b['note']}")
    return lines


def _sparkline(series: list[int]) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    top = max(series) or 1
    return "".join(blocks[min(7, int(v / top * 7.999))] if v else "▁" for v in series)


# -- dashboard ----------------------------------------------------------------


def render_html(snap: dict[str, Any]) -> str:
    """The progress section of the dashboard: numbers now, and a script that keeps them moving by polling
    progress.json while a run writes it (bars, rate, ETA, phase, sparkline, batches, log)."""
    e = ui.esc
    parts = [
        "<h2>Progress: what the judge has processed</h2>",
        _html_state_and_bars(snap),
        _html_kpis(snap),
        "<div class='spark'><span class='muted'>judged per 5 minutes, last 3 hours</span>"
        f"<svg id='p-spark' viewBox='0 0 360 48' width='360' height='48' preserveAspectRatio='none'>{_spark_svg(snap.get('series') or [])}</svg></div>",
        "<table><tr><th>Folder</th><th class='num'>Unread</th><th class='num'>Judged</th><th class='num'>Pending</th>"
        "<th class='num'>With the model</th><th class='num'>70+</th><th class='num'>Cut</th><th>Cut-off</th></tr><tbody id='p-folders'>",
        _folder_rows(snap["folders"], e),
        "</tbody></table>",
        "<h3>Batches</h3><table><tr><th>Batch</th><th>Purpose</th><th class='num'>Articles</th><th>State</th></tr>"
        f"<tbody id='p-batches'>{_batch_rows(snap['batches'], e)}</tbody></table>",
        LIVE_SCRIPT,
    ]
    return "\n".join(parts)


def _html_state_and_bars(snap: dict[str, Any]) -> str:
    e = ui.esc
    unread = snap["unread"] or 1
    judged, pending, inflight = snap["judged_unread"], snap["pending"], snap["inflight"]
    running = bool(snap.get("running"))
    rate = snap.get("rate_per_min", 0.0)
    old_note = (
        f" · {snap['too_old']:,} older than {snap['max_age_days']} days are never judged"
        if snap["too_old"]
        else ""
    )
    phase = "run in progress · " + e(snap.get("phase") or "working") if running else "idle"
    return (
        "<div class='live'>"
        f"<span id='p-dot' class='dot {'on' if running else ''}'></span>"
        f"<span id='p-phase'>{phase}</span>"
        f" · <span id='p-rate'>{rate:.0f}</span> judged/min · ETA for pending <span id='p-eta'>{e(_eta(snap.get('eta_min')))}</span>"
        " · updated <span id='p-updated'>just now</span></div>"
        "<div class='prog'>"
        f"<span id='p-bar-judged' style='width:{judged / unread * 100:.2f}%' class='p-judged' title='judged'></span>"
        f"<span id='p-bar-flight' style='width:{inflight / unread * 100:.2f}%' class='p-flight' title='with the model'></span>"
        f"<span id='p-bar-pending' style='width:{pending / unread * 100:.2f}%' class='p-pending' title='pending'></span>"
        "</div>"
        "<p class='muted'>"
        f"<b class='ok'>judged <span id='p-judged'>{judged:,}</span></b> · "
        f"<b class='c-flight'>with the model <span id='p-inflight'>{inflight:,}</span></b> · "
        f"<b class='muted'>pending <span id='p-pending'>{pending:,}</span></b> of <span id='p-unread'>{snap['unread']:,}</span> unread"
        f"<span id='p-old'>{e(old_note)}</span>"
        + (f" · {snap['skipped']:,} in folders that are not judged" if snap["skipped"] else "")
        + (
            f" · {snap['stand_in']:,} judged by the quick judge, {snap['rejudge']:,} of them await the judge"
            if snap.get("stand_in")
            else ""
        )
        + "</p>"
    )


def _html_kpis(snap: dict[str, Any]) -> str:
    t, w = snap["today"], snap["week"]
    per = f"${snap['per_1000']:.2f}" if snap["per_1000"] is not None else "–"
    cache = f"{snap['cache_hit']:.0%}" if snap["cache_hit"] is not None else "–"
    tiles = [
        ("judged today", "k-judged-today", f"{t['judged']:,}"),
        ("arrived today", "k-arrived-today", f"{t['arrived']:,}"),
        ("cut today", "k-cut-today", f"{t['cut']:,}"),
        ("judged, 7 days", "k-judged-week", f"{w['judged']:,}"),
        ("spend today", "k-spend-today", f"${t['spend']:.2f}"),
        ("per 1,000 judged", "k-per", per),
        ("cache hits", "k-cache", cache),
    ]
    inner = "".join(
        f"<div class='kpi'><span class='muted'>{label}</span><b id='{tid}'>{value}</b></div>"
        for label, tid, value in tiles
    )
    return f"<div class='kpis'>{inner}</div>"


def _folder_rows(folders: list[dict[str, Any]], e: Callable[[Any], str]) -> str:
    rows = []
    for f in folders:
        if not f["unread"] and not f["cut"]:
            continue
        cutoff = "never" if f["floor"] is None else (str(f["floor"]) if f["scored"] else "not judged")
        rows.append(
            f"<tr><td>{e(f['folder'])}</td><td class='num'>{f['unread']:,}</td><td class='num'>{f['judged']:,}</td>"
            f"<td class='num'>{f['pending']:,}</td><td class='num'>{f['inflight']:,}</td><td class='num'>{f['good']:,}</td>"
            f"<td class='num'>{f['cut']:,}</td><td>{e(cutoff)}</td></tr>"
        )
    return "".join(rows)


def _batch_rows(batches: list[dict[str, Any]], e: Callable[[Any], str]) -> str:
    if not batches:
        return "<tr><td colspan='4' class='muted'>none yet</td></tr>"
    rows = []
    for b in batches:
        state = f"in progress, submitted {b['age']} ago" if b["status"] == "submitted" else b["status"]
        if b.get("note"):
            state += f" · {b['note']}"
        rows.append(
            f"<tr><td>{e(b['id'][:22])}</td><td>{e(b['purpose'])}</td><td class='num'>{b['n_articles']:,}</td>"
            f"<td>{e(state)}</td></tr>"
        )
    return "".join(rows)


def _spark_svg(series: list[int]) -> str:
    if not series:
        return ""
    top = max(series) or 1
    n = len(series)
    pts = [(i * 360 / max(1, n - 1), 46 - v / top * 44) for i, v in enumerate(series)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"0,48 {line} 360,48"
    return (
        f"<polygon points='{area}' fill='var(--ok)' fill-opacity='0.15'/>"
        f"<polyline points='{line}' fill='none' stroke='var(--ok)' stroke-width='1.5'/>"
    )


PROGRESS_CSS = """
.prog{display:flex;height:18px;background:var(--line);border-radius:9px;overflow:hidden;margin:8px 0}
.prog span{display:block;height:100%;transition:width .8s ease}.p-judged{background:var(--ok)}.p-flight{background:var(--flight)}.p-pending{background:var(--pending)}
.live{font-size:13.5px;margin:4px 0}.c-flight{color:var(--flight)}.spark{margin:6px 0 10px}.spark svg{display:block;margin-top:2px}
"""

LIVE_SCRIPT = """<script>
(function(){
  var lastAt = null, lastRev = null, fails = 0;
  function n(x){ return (x==null) ? '–' : Number(x).toLocaleString('en-US'); }
  function set(id, v){ var el = document.getElementById(id); if(!el) return; var s = String(v);
    if(el.textContent !== s){ el.textContent = s; el.classList.add('flash'); setTimeout(function(){ el.classList.remove('flash'); }, 1500); } }
  function eta(m){ if(m==null) return '–'; if(m<1) return 'under a minute'; if(m<90) return Math.round(m)+' min'; return (m/60).toFixed(1)+' h'; }
  function esc(s){ return String(s==null?'':s).replace(/[&<>]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]; }); }
  function ago(iso){ if(!iso) return '?'; var m = Math.floor((Date.now()-Date.parse(iso))/60000); if(m<1) return 'under a minute'; if(m<60) return m+' min'; return Math.floor(m/60)+' h '+(m%60); }
  function spark(series){ var svg = document.getElementById('p-spark'); if(!svg || !series || !series.length) return;
    var top = Math.max.apply(null, series) || 1, n = series.length, pts = [];
    for(var i=0;i<n;i++){ pts.push((i*360/Math.max(1,n-1)).toFixed(1)+','+(46-series[i]/top*44).toFixed(1)); }
    var line = pts.join(' ');
    svg.innerHTML = "<polygon points='0,48 "+line+" 360,48' fill='var(--ok)' fill-opacity='0.15'/><polyline points='"+line+"' fill='none' stroke='var(--ok)' stroke-width='1.5'/>"; }
  function apply(d){
    var unread = d.unread || 1;
    document.getElementById('p-bar-judged').style.width = (d.judged_unread/unread*100).toFixed(2)+'%';
    document.getElementById('p-bar-flight').style.width = (d.inflight/unread*100).toFixed(2)+'%';
    document.getElementById('p-bar-pending').style.width = (d.pending/unread*100).toFixed(2)+'%';
    set('p-judged', n(d.judged_unread)); set('p-inflight', n(d.inflight)); set('p-pending', n(d.pending)); set('p-unread', n(d.unread));
    set('p-rate', Math.round(d.rate_per_min||0)); set('p-eta', eta(d.eta_min));
    set('p-old', d.too_old ? ' · '+n(d.too_old)+' older than '+d.max_age_days+' days are never judged' : '');
    var dot = document.getElementById('p-dot'); if(dot) dot.className = 'dot' + (d.running ? ' on' : '');
    set('p-phase', d.running ? ('run in progress' + (d.run_started ? ' for '+ago(d.run_started) : '') + ' · ' + (d.phase||'working')) : 'idle');
    set('k-judged-today', n(d.today.judged)); set('k-arrived-today', n(d.today.arrived)); set('k-cut-today', n(d.today.cut));
    set('k-judged-week', n(d.week.judged)); set('k-spend-today', '$'+Number(d.today.spend||0).toFixed(2));
    set('k-per', d.per_1000==null ? '–' : '$'+Number(d.per_1000).toFixed(2));
    set('k-cache', d.cache_hit==null ? '–' : Math.round(d.cache_hit*100)+'%');
    spark(d.series);
    var fb = document.getElementById('p-folders'); if(fb){ var h=''; (d.folders||[]).forEach(function(f){ if(!f.unread && !f.cut) return;
      var cutoff = f.floor==null ? 'never' : (f.scored ? String(f.floor) : 'not judged');
      h += '<tr><td>'+esc(f.folder)+'</td><td class="num">'+n(f.unread)+'</td><td class="num">'+n(f.judged)+'</td><td class="num">'+n(f.pending)+'</td><td class="num">'+n(f.inflight)+'</td><td class="num">'+n(f.good)+'</td><td class="num">'+n(f.cut)+'</td><td>'+esc(cutoff)+'</td></tr>'; });
      if(h && fb.innerHTML !== h) fb.innerHTML = h; }
    var bb = document.getElementById('p-batches'); if(bb){ var hb=''; (d.batches||[]).forEach(function(b){
      var state = b.status==='submitted' ? 'in progress, submitted '+ago(b.created_at)+' ago' : b.status; if(b.note) state += ' · '+b.note;
      hb += '<tr><td>'+esc(String(b.id).slice(0,22))+'</td><td>'+esc(b.purpose)+'</td><td class="num">'+n(b.n_articles)+'</td><td>'+esc(state)+'</td></tr>'; });
      bb.innerHTML = hb || '<tr><td colspan="4" class="muted">none yet</td></tr>'; }
    lastAt = d.at;
  }
  function tick(){
    fetch('progress.json?t='+Date.now(), {cache:'no-store'}).then(function(r){ if(!r.ok) throw 0; return r.json(); })
      .then(function(d){ fails = 0; apply(d); }).catch(function(){ fails++; });
  }
  setInterval(function(){ var el = document.getElementById('p-updated'); if(!el) return;
    if(!lastAt){ el.textContent = fails > 2 ? 'live view needs the page served over http (tailscale serve)' : 'just now'; return; }
    var s = Math.max(0, Math.round((Date.now()-Date.parse(lastAt))/1000)); el.textContent = s < 5 ? 'just now' : s+' s ago'; }, 1000);
  tick(); setInterval(tick, 4000);
})();
</script>"""
