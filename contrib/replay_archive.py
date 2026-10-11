"""Replay the judge over an archive from an earlier filtering tool, offline, before anything
touches the live account. Not part of the package: it depends on another project's database.

The archive expected: a SQLite file with a table `articles` holding `id`, `feed`, `title`,
`url`, `summary` (HTML), `fetched_at`, `decision` ("keep" or "junk") and `quality`; optionally
a `golden.jsonl` with lines `{"article_id": ..., "judgment": "star"}` naming the articles
you starred. A stratified sample by default (random rows are proportional to feed volume),
plus every starred article, so the eval gate can be checked: no starred article may fall
below the cut-off.

    cd <your bodh folder>
    .venv/bin/python contrib/replay_archive.py --archive /path/to/archive.db [--golden golden.jsonl] [--sample 500]

Costs about a dollar for 500 articles. Touches nothing in BazQux.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from bodh import store
from bodh.config import Config, load_config, load_secrets
from bodh.judge import LLM, Judge, compose, concept_context

ARCHIVE_QUERY = """
SELECT id, feed, title, url, summary, fetched_at, decision, quality AS old_quality
FROM articles WHERE title IS NOT NULL AND title != '' ORDER BY RANDOM() LIMIT ?
"""


def _row(raw: sqlite3.Row) -> dict:
    return {
        "id": raw["id"],
        "feed_title": raw["feed"],
        "folder": None,
        "title": raw["title"],
        "url": raw["url"],
        "author": "",
        "published": None,
        "text": store.html_to_text(raw["summary"] or ""),
        "old_decision": raw["decision"],
        "old_quality": raw["old_quality"],
    }


def starred_from_golden(golden_path: Path) -> set[str]:
    ids: set[str] = set()
    if not golden_path.exists():
        return ids
    for line in golden_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("judgment") == "star" and rec.get("article_id"):
            ids.add(rec["article_id"])
    return ids


def replay(
    cfg: Config,
    llm: LLM,
    conn: sqlite3.Connection,
    archive_db: Path,
    golden: Path | None,
    sample: int = 500,
    workers: int = 3,
) -> dict:
    src = sqlite3.connect(f"file:{archive_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = [_row(r) for r in src.execute(ARCHIVE_QUERY, (sample,))]
    starred_ids = starred_from_golden(golden) if golden else set()
    if starred_ids:
        marks = ",".join("?" * len(starred_ids))
        extra = src.execute(
            f"SELECT id, feed, title, url, summary, fetched_at, decision, quality AS old_quality FROM articles WHERE id IN ({marks})",
            tuple(starred_ids),
        ).fetchall()
        have = {r["id"] for r in rows}
        rows += [_row(r) for r in extra if r["id"] not in have]
    run_id = store.begin_run(conn, "replay")
    judge = Judge(cfg, llm, conn, context=concept_context(conn, cfg))
    results: list[dict] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed

    batches = [
        rows[i : i + cfg.judge.articles_per_call] for i in range(0, len(rows), cfg.judge.articles_per_call)
    ]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(judge.judge_batch, b, run_id): b for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            try:
                verdicts = fut.result()
            except Exception as exc:
                results.append({"error": str(exc), "n": len(batch)})
                continue
            for r in batch:
                v = verdicts.get(r["id"])
                if v is None:
                    continue
                results.append(
                    {
                        "id": r["id"],
                        "feed": r["feed_title"],
                        "title": r["title"],
                        "score": compose(v, cfg),
                        "confidence": v.confidence,
                        "concept": v.concept,
                        "lesson": v.lesson,
                        "old_decision": r["old_decision"],
                        "starred": r["id"] in starred_ids,
                    }
                )
    scored = [r for r in results if "score" in r]
    cutoff = cfg.autocut.cutoff
    below_cutoff = [r for r in scored if r["score"] < cutoff and r["confidence"] != "low"]
    starred_scored = [r for r in scored if r["starred"]]
    starred_below = [r for r in starred_scored if r["score"] < cutoff]
    by_feed: dict[str, list[float]] = defaultdict(list)
    for r in scored:
        by_feed[r["feed"]].append(r["score"])
    bands = defaultdict(int)
    for r in scored:
        bands[
            next(
                name
                for lo, name in [(90, "90+"), (70, "70-89"), (50, "50-69"), (30, "30-49"), (0, "0-29")]
                if r["score"] >= lo
            )
        ] += 1
    report = {
        "sampled": len(rows),
        "scored": len(scored),
        "failed": sum(r.get("n", 0) for r in results if "error" in r),
        "cutoff": cutoff,
        "would_cut_share": (len(below_cutoff) / len(scored)) if scored else None,
        "bands": dict(bands),
        "starred_checked": len(starred_scored),
        "starred_below_cutoff": [r["title"] for r in starred_below],
        "gate_passed": not starred_below,
        "feeds": sorted(
            ({"feed": f, "n": len(s), "mean": sum(s) / len(s)} for f, s in by_feed.items()),
            key=lambda x: -x["n"],
        )[:25],
        "old_keep_now_below_cutoff": sum(1 for r in below_cutoff if r["old_decision"] == "keep"),
        "old_junk_now_above_70": sum(1 for r in scored if r["old_decision"] == "junk" and r["score"] >= 70),
        "spend_usd": store.spent_since(conn, store.local_midnight_iso(cfg.timezone)),
        "sample": sorted(scored, key=lambda r: -r["score"])[:15]
        + sorted(scored, key=lambda r: r["score"])[:15],
    }
    store.finish_run(
        conn,
        run_id,
        True,
        scored=len(scored),
        summary=f"replay: {len(scored)} scored, gate {'passed' if report['gate_passed'] else 'FAILED'}",
    )
    out = cfg.root / cfg.data_dir / "replay.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    report["path"] = str(out)
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="judge a sample of an archive offline and check the eval gate")
    ap.add_argument("--archive", required=True, type=Path)
    ap.add_argument("--golden", type=Path, default=None)
    ap.add_argument("--sample", type=int, default=500)
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args()
    if not args.archive.exists():
        sys.exit(f"archive not found: {args.archive}")
    cfg = load_config()
    secrets = load_secrets(cfg.root)
    conn = store.connect(cfg.db_path)
    llm = LLM(cfg, secrets, conn)
    rep = replay(
        cfg,
        llm,
        conn,
        args.archive,
        args.golden,
        sample=args.sample,
        workers=args.workers or cfg.limits.parallel_calls,
    )
    if rep["would_cut_share"] is None:
        print("nothing scored")
    else:
        print(
            f"scored {rep['scored']} of {rep['sampled']} (failed {rep['failed']}); cut-off {rep['cutoff']}; "
            f"would cut {rep['would_cut_share']:.0%}"
        )
    print(f"bands: {rep['bands']}")
    print(
        f"starred checked {rep['starred_checked']}; below the cut-off: {len(rep['starred_below_cutoff'])} "
        f"-> gate {'PASSED' if rep['gate_passed'] else 'FAILED'}"
    )
    for t in rep["starred_below_cutoff"][:10]:
        print(f"   below the cut-off: {t[:80]}")
    print(
        f"archive kept, now below the cut-off: {rep['old_keep_now_below_cutoff']}; archive junked, now 70+: {rep['old_junk_now_above_70']}"
    )
    print(f"full report: {rep['path']}")


if __name__ == "__main__":
    main()
