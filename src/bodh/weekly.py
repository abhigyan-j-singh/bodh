"""Sunday's job: the profiler rewrites the learned profile (kept only if the eval gate passes),
the weekly note is written, the drift check re-judges a few of the week's articles, the
dashboard is rendered, and the run row records it all."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from . import dashboard, profiler, store
from . import run as runmod

log = logging.getLogger(__name__)

DRIFT_SAMPLE = 30
DRIFT_MIN_ROWS = 10
DRIFT_MEAN_SHIFT = 8.0
DRIFT_MEAN_ABS = 15.0


def weekly(
    ctx: runmod.Context, dry_run: bool = False, say: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Returns the profiler's result with `drift` and `note` added. A failure is recorded on
    the run row and the heartbeat before it is raised."""
    out = say or (lambda _t: None)
    run_id = store.begin_run(ctx.conn, "weekly")
    try:
        result = profiler.weekly_rewrite(ctx.cfg, ctx.llm, ctx.conn, run_id, dry_run=dry_run)
        out(result["reason"])
        if result.get("diff"):
            out(result["diff"])
        drift = drift_check(ctx, run_id)
        if drift:
            out(drift)
        note = dashboard.weekly_note(ctx.cfg, ctx.conn, result)
        out(f"weekly note: {note}")
        dashboard.render(ctx.cfg, ctx.conn)
        store.finish_run(
            ctx.conn, run_id, True, summary=f"weekly: {result['reason']}" + (f"; {drift}" if drift else "")
        )
        if not result["applied"] and result.get("gate") and not result["gate"]["passed"]:
            ctx.heartbeat.fail(f"weekly profile rewrite rejected: {result['reason']}")
        return {**result, "drift": drift, "note": str(note), "run_id": run_id}
    except Exception as exc:  # the run row and the heartbeat record the failure before it is raised
        store.finish_run(ctx.conn, run_id, False, error=f"{type(exc).__name__}: {exc}")
        ctx.heartbeat.fail(f"weekly failed: {exc}")
        raise


def drift_check(ctx: runmod.Context, run_id: int) -> str:
    """Re-judge a sample of the week's articles; a large shift means the model behind the API
    changed. Empty when there is too little to re-judge."""
    conn, cfg = ctx.conn, ctx.cfg
    rows = conn.execute(
        """SELECT a.*, s.base_score FROM scores s JOIN articles a ON a.id = s.article_id
           WHERE s.scored_at >= ? AND s.model = ? AND a.text != '' ORDER BY RANDOM() LIMIT ?""",
        (store.days_ago_iso(7), cfg.models.judge, DRIFT_SAMPLE),
    ).fetchall()
    if len(rows) < DRIFT_MIN_ROWS:
        return ""
    judge = ctx.judge()
    from .judgment import compose

    diffs = []
    for i in range(0, len(rows), cfg.judge.articles_per_call):
        batch = rows[i : i + cfg.judge.articles_per_call]
        verdicts = judge.judge_batch(batch, run_id)
        for r in batch:
            v = verdicts.get(r["id"])
            if v is not None:
                diffs.append(compose(v, cfg, judge.expansion_topics) - float(r["base_score"]))
    if not diffs:
        return ""
    mean_shift = sum(diffs) / len(diffs)
    mean_abs = sum(abs(d) for d in diffs) / len(diffs)
    msg = f"drift check: {len(diffs)} re-scored, mean shift {mean_shift:+.1f}, mean absolute change {mean_abs:.1f}"
    if abs(mean_shift) > DRIFT_MEAN_SHIFT or mean_abs > DRIFT_MEAN_ABS:
        ctx.heartbeat.fail(msg + " (above threshold)")
        return msg + " ABOVE THRESHOLD"
    return msg
