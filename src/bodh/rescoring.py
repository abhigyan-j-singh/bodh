"""`bodh rescore`: recompose stored judgments with the current weights and bonuses.

The judge records five sub-scores per article and composes them into one score with
`judge.weights`, the home and expansion bonuses and the caps (judgment.compose). Changing the
weights changes only new judgments; everything already judged keeps the score the old weights
gave it, and its band tag with it. This recomposes the stored sub-scores, so the queue is ranked
the way the judge ranks today, without a single model call. A review's second look is kept: the
score stays the higher of the recomposed base and the second look, as apply_review decided.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from . import cutting
from .config import Config
from .judgment import compose
from .readers.bazqux import BazQuxClient


@dataclass
class Change:
    id: str
    title: str
    old: float
    new: float
    base: float  # the recomposed sub-scores alone; `new` also keeps a review's second look
    old_band: str
    new_band: str
    unread: bool


@dataclass
class Plan:
    examined: int
    changes: list[Change]
    moves: dict[str, int] = field(default_factory=dict)  # "score-50 → score-70": n
    now_below: int = 0  # unread articles the next autocut will now cut
    now_above: int = 0  # unread articles the next autocut would have cut and now keeps


def plan(conn: sqlite3.Connection, cfg: Config, expansion: list[str], include_cut: bool) -> Plan:
    """Every change the current weights would make; nothing is written."""
    where = "" if include_cut else "WHERE st.status = 'unread'"
    rows = conn.execute(
        f"""SELECT s.article_id, a.title, a.folder, s.quality, s.learning, s.fit, s.breadth, s.relevance,
                   s.concept, s.kind, s.flags, s.dupe_of, s.score, s.bucket, s.second_look_score, st.status
            FROM scores s JOIN articles a ON a.id = s.article_id JOIN state st ON st.article_id = s.article_id
            {where}"""
    ).fetchall()
    out = Plan(examined=len(rows), changes=[])
    for r in rows:
        d = dict(r)
        try:
            d["flags"] = json.loads(r["flags"] or "[]")
        except ValueError:
            d["flags"] = []
        base = new = compose(d, cfg, expansion)
        if r["second_look_score"] is not None:
            new = max(new, float(r["second_look_score"]))
        old = float(r["score"])
        new_band = cfg.tags.band_for(new)
        if new == old and new_band == (r["bucket"] or new_band):
            continue
        unread = r["status"] == "unread"
        change = Change(
            r["article_id"], r["title"] or "", old, new, base, r["bucket"] or "", new_band, unread
        )
        out.changes.append(change)
        if change.old_band != new_band:
            key = f"{change.old_band or '(none)'} → {new_band}"
            out.moves[key] = out.moves.get(key, 0) + 1
        if unread:
            cutoff = cfg.cutoff_for(r["folder"]) or cfg.autocut.cutoff
            if old >= cutoff > new:
                out.now_below += 1
            elif new >= cutoff > old:
                out.now_above += 1
    return out


def apply(conn: sqlite3.Connection, client: BazQuxClient, changes: list[Change]) -> int:
    """Write the new scores and bands; move the band tags of unread articles in BazQux (the
    old band off, the new one on). Returns the number of articles changed."""
    stale: dict[str, list[str]] = {}
    for c in changes:
        row = conn.execute("SELECT bucket_tagged FROM scores WHERE article_id = ?", (c.id,)).fetchone()
        if c.unread and c.old_band and c.old_band != c.new_band and row and row["bucket_tagged"]:
            stale.setdefault(c.old_band, []).append(c.id)
        conn.execute(
            """UPDATE scores SET base_score = ?, score = ?, bucket = ?,
                      bucket_tagged = CASE WHEN bucket = ? THEN bucket_tagged ELSE 0 END
               WHERE article_id = ?""",
            (c.base, c.new, c.new_band, c.new_band, c.id),
        )
    conn.commit()
    cutting.untag_stale(client, stale)
    cutting.tag_bands(client, conn)
    conn.commit()
    return len(changes)
