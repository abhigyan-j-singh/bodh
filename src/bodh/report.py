"""The monthly feed report: per feed, volume, mean score, share cut, read and star
rates, and a proposal (unsubscribe, move to the strictest folder, protect). Proposals are
written for you to act on in BazQux; nothing is changed automatically."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .dashboard import feeds

MIN_VOLUME_FOR_PROPOSAL = 40   # articles in the window, and judged ones: a feed nobody judged proposes nothing


def strictest_folder(cfg: Config) -> str | None:
    """The folder with the highest automatic floor above the default (`News` in the example config),
    where a high-volume, low-keep feed belongs; None when the config has no such folder."""
    best: tuple[int, str] | None = None
    for name, policy in cfg.folders.items():
        floor = policy.cutoff
        if floor is not None and not policy.protected and floor > cfg.autocut.cutoff:
            if best is None or floor > best[0]:
                best = (floor, name)
    return best[1] if best else None


def proposal(row: sqlite3.Row, cfg: Config) -> str:
    n, judged = row["n"], row["judged"] or 0
    mean = row["mean_score"]
    cut = row["cut_share"] or 0
    star = row["star_share"] or 0
    read = row["read_share"] or 0
    if n < MIN_VOLUME_FOR_PROPOSAL or judged < MIN_VOLUME_FOR_PROPOSAL or mean is None:
        return ""
    if star == 0 and read < 0.02 and cut > 0.85 and mean < 35:
        return "unsubscribe: high volume, almost everything cut, nothing read or starred"
    strict = strictest_folder(cfg)
    if strict and n >= 300 and cut > 0.6 and (row["folder"] or "") != strict:
        return f"move to {strict}: high volume with a low keep rate; a stricter floor fits"
    if star >= 0.08 or (read >= 0.3 and mean >= 60):
        return "consider protecting: you read or star an unusual share of it"
    return ""


def render(cfg: Config, conn: sqlite3.Connection, days: int = 30) -> Path:
    tz = ZoneInfo(cfg.timezone)
    rows = feeds(conn, days=days, limit=200)
    lines = [f"# Feed report · {datetime.now(tz).strftime('%d %b %Y')} · last {days} days", "",
             "| Feed | Folder | Articles | Mean score | Cut | Read | Starred | Proposal |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |"]
    proposals = 0
    for r in rows:
        p = proposal(r, cfg)
        proposals += bool(p)
        mean = f"{r['mean_score']:.0f}" if r["mean_score"] is not None else "–"
        lines.append(
            f"| {r['feed_title'][:40]} | {r['folder'] or ''} | {r['n']} | {mean} | {(r['cut_share'] or 0) * 100:.0f}% | "
            f"{(r['read_share'] or 0) * 100:.0f}% | {(r['star_share'] or 0) * 100:.0f}% | {p} |"
        )
    lines += ["", f"{proposals} proposals. Act on them in BazQux (unsubscribe, or drag the feed into a folder); "
                  "this report never changes anything itself."]
    out_dir = cfg.root / cfg.dashboard_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "feed-report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
