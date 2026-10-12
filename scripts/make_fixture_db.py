"""Build a database of many judged articles for the performance tests and for trying the
dashboard on a full-sized store: `python scripts/make_fixture_db.py --articles 50000 --out
/tmp/bodh-50k.db`. Everything is invented (titles from word lists, texts of a few hundred
words, scores spread over the bands); nothing comes from anyone's reading list.

Shape, close to a year of daily use: 60% of the articles cut by autocut or a prune, 35% read
or dismissed, 5% unread (half of them judged); concepts from a pool of 300; feedback from
stars, reads, grades and rescues; a run row every thirty minutes for 60 days; costs per run.
Also importable: `build(path, n)`."""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bodh import store
from bodh.ids import short_to_long

WORDS = [
    "reef",
    "current",
    "carbon",
    "grid",
    "turbine",
    "lattice",
    "ledger",
    "kernel",
    "sonnet",
    "harbour",
    "glacier",
    "plankton",
    "compiler",
    "census",
    "basalt",
    "pigment",
    "orbit",
    "cadence",
    "tariff",
    "enzyme",
    "mosaic",
    "trellis",
    "quorum",
    "saffron",
    "vector",
    "dune",
    "archive",
    "cipher",
    "lantern",
    "meadow",
    "pulley",
    "sextant",
    "tundra",
    "violin",
    "walnut",
    "yarrow",
    "zenith",
]
FEEDS = [f"Feed {w.title()}" for w in WORDS[:40]]
FOLDERS = ["Always read", "News", "Essays", "Aggregators", "Science", "Comics", None]
BANDS = {90: "score-90", 70: "score-70", 50: "score-50", 30: "score-30", 0: "score-0"}
KINDS = ["primary_source", "original_analysis", "reporting", "commentary", "aggregation", "promo", "other"]


def _title(rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(4, 9))).capitalize()


def _text(rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(150, 400)))


def _band(score: float) -> str:
    for threshold in sorted(BANDS, reverse=True):
        if score >= threshold:
            return BANDS[threshold]
    return BANDS[0]


def build(path: Path, n: int = 50_000, seed: int = 7, now: datetime | None = None) -> Path:
    """Write the fixture to `path` (replaced if it exists). Returns the path. `now` fixes the
    clock everything is dated from, so the visual tests' screenshots are the same every day."""
    if path.exists():
        path.unlink()
    rng = random.Random(seed)
    conn = store.connect(path)
    now = now or datetime.now(timezone.utc)
    concepts = [f"{rng.choice(WORDS)} {rng.choice(WORDS)}" for _ in range(300)]
    runs: list[tuple] = []
    for i in reversed(range(60 * 48)):  # a run every thirty minutes for sixty days, oldest first
        at = now - timedelta(minutes=30 * i)
        runs.append(
            (
                "run",
                at.isoformat(),
                (at + timedelta(seconds=40)).isoformat(),
                1,
                400,
                30,
                30,
                2,
                20,
                0,
                0,
                5,
                0.02,
                35.0,
                "",
                "judged 30",
            )
        )
    articles, scores, state, feedback, costs = [], [], [], [], []
    for k in range(1, n + 1):
        aid = short_to_long(k)
        age = rng.random() ** 2 * 365  # more recent than old
        crawled = now - timedelta(days=age)
        feed = rng.choice(FEEDS)
        folder = rng.choice(FOLDERS)
        articles.append(
            (
                aid,
                k,
                f"feed/https://example.com/{feed.replace(' ', '')}/rss",
                feed,
                folder,
                _title(rng),
                f"https://example.com/{feed.replace(' ', '')}/{k}",
                "",
                int(crawled.timestamp()),
                int(crawled.timestamp() * 1000),
                _text(rng),
                crawled.isoformat(),
            )
        )
        fate = rng.random()
        judged = fate > 0.025  # the last slice is unread and pending
        if judged:
            score = round(min(100.0, max(0.0, rng.gauss(45, 25))), 1)
            scores.append(
                (
                    aid,
                    "teacher.v2",
                    "claude-sonnet-5-5",
                    int(score),
                    int(score),
                    int(score),
                    int(score),
                    int(score),
                    score,
                    score,
                    "a lesson in twelve words or fewer about the thing",
                    rng.choice(concepts),
                    rng.choice(KINDS),
                    json.dumps([]),
                    None,
                    rng.choice(["high", "high", "medium", "low"]),
                    None,
                    None,
                    _band(score),
                    1,
                    crawled.isoformat(),
                )
            )
        if fate < 0.05:
            status, cut_mode, cut_at = "unread", None, None
        elif fate < 0.40:
            status, cut_mode, cut_at = rng.choice(["read", "dismissed", "expired"]), None, None
        else:
            status = "cut"
            cut_mode = rng.choice(["autocut", "autocut", "prune", "clear-old"])
            cut_at = (crawled + timedelta(hours=1)).isoformat()
        state.append(
            (
                aid,
                status,
                1 if rng.random() < 0.01 else 0,
                1 if folder == "Always read" else 0,
                0,
                0,
                0,
                rng.randint(1, len(runs)) if status == "cut" else None,
                cut_mode,
                cut_at,
                1 if status == "cut" else 0,
                1 if status == "cut" else 0,
                None,
                (crawled + timedelta(hours=3)).isoformat() if status == "read" else None,
                crawled.isoformat(),
                crawled.isoformat(),
            )
        )
        r = rng.random()
        if status == "read" and r < 0.5:
            feedback.append((aid, "read", (crawled + timedelta(hours=3)).isoformat()))
        if r < 0.02:
            feedback.append(
                (
                    aid,
                    rng.choice(["star", "great", "good", "meh", "bad", "wrong"]),
                    (crawled + timedelta(hours=4)).isoformat(),
                )
            )
    for i in range(len(runs)):
        costs.append((i + 1, "claude-sonnet-5-5", "teacher", 20000, 1500, 0, 18000, 0.02, runs[i][1]))
    with store.transaction(conn):
        conn.executemany("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", articles)
        conn.executemany("INSERT INTO scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", scores)
        conn.executemany("INSERT INTO state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", state)
        conn.executemany("INSERT OR IGNORE INTO feedback (article_id, kind, at) VALUES (?,?,?)", feedback)
        conn.executemany(
            "INSERT INTO runs (kind, started_at, finished_at, ok, unread_upstream, new, scored, second_looks, cut_autocut, cut_prune, rescues, reads, spend_usd, boundary, error, summary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            runs,
        )
        conn.executemany(
            "INSERT INTO costs (run_id, model, purpose, input_tokens, output_tokens, cache_write, cache_read, usd, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            costs,
        )
        store.kv_set(conn, store.SCHEMA_VERSION_KEY, str(store.SCHEMA_VERSION))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--articles", type=int, default=50_000)
    parser.add_argument("--out", type=Path, default=Path("/tmp/bodh-50k.db"))
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    path = build(args.out, args.articles, args.seed)
    print(f"wrote {path} ({path.stat().st_size // 1_000_000} MB, {args.articles} articles)")


if __name__ == "__main__":
    main()
