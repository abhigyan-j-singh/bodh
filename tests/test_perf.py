"""Performance at 50,000 articles, on the fixture `scripts/make_fixture_db.py` builds. Run
with `BODH_PERF=1` (CI does, on Linux); skipped otherwise. Each budget is the figure the
plan names; the test fails only at three times it (a regression, not a slow runner) and
prints what it measured, so the CI log shows the trend."""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from fakes import FakeLLM, FakeReader, make_item

from bodh import dashboard, feedback, progress, recovery, run, store
from bodh.config import Config, Secrets

pytestmark = pytest.mark.skipif(
    not os.environ.get("BODH_PERF"), reason="BODH_PERF=1 runs the 50,000-article timings"
)

ARTICLES = 50_000
SLACK = 3.0  # fail at three times the budget


def _budget(name: str, seconds: float, budget: float) -> None:
    print(f"\nperf: {name}: {seconds * 1000:.0f} ms (budget {budget * 1000:.0f} ms)")
    assert seconds < budget * SLACK, (
        f"{name} took {seconds:.2f}s, over {SLACK:.0f}x the budget of {budget:.2f}s"
    )


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory) -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from make_fixture_db import build

    return build(tmp_path_factory.mktemp("perf") / "bodh-50k.db", ARTICLES)


@pytest.fixture
def world(fixture_db: Path, tmp_path: Path):
    """A fresh copy of the fixture under a project root, so a test may write to it."""
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    (root / "profile").mkdir()
    (root / "profile" / "about.md").write_text("# About the student\nStudent: a reader.\n", encoding="utf-8")
    (root / "profile" / "learned.md").write_text(
        "## What the student has been learning\nx\n", encoding="utf-8"
    )
    shutil.copy(fixture_db, root / "data" / "bodh.db")
    cfg = Config()
    cfg.root = root
    cfg.models.batch = False
    conn = store.connect(cfg.db_path)
    yield cfg, conn
    conn.close()


def test_pending_query_under_100ms_at_50k(world):
    cfg, conn = world
    t0 = time.perf_counter()
    rows = store.unscored_unread(
        conn, cfg.judge.prompt_version, cfg.limits.max_age_days, cfg.unjudged_folders(), exclude_inflight=True
    )
    n = store.unscored_unread_count(
        conn, cfg.judge.prompt_version, cfg.limits.max_age_days, cfg.unjudged_folders(), exclude_inflight=True
    )
    _budget("pending query and count", time.perf_counter() - t0, 0.1)
    assert len(rows) == n > 0


def test_dashboard_render_under_1s_at_50k(world):
    cfg, conn = world
    t0 = time.perf_counter()
    path = dashboard.render(cfg, conn)
    _budget("dashboard render", time.perf_counter() - t0, 1.0)
    assert path.exists() and (path.parent / "progress.json").exists()
    t0 = time.perf_counter()
    progress.render_text(progress.snapshot(conn, cfg))
    _budget("progress snapshot and text", time.perf_counter() - t0, 0.5)


def test_run_non_network_phases_under_5s_at_50k(world):
    """A whole run with the reader and the model faked: the feedback pass over the pile, the
    ingest of 40 new articles, their judging, the tags, the autocut's selection over every
    unread judged article, the run row and the dashboard."""
    cfg, conn = world
    cfg.autocut.new_ground_share = 0.0
    recovery.backup_daily(
        conn, cfg
    )  # today's backup exists already: the run's VACUUM INTO is not what is measured
    unread = sorted(store.status_ids(conn, "unread"))
    new = [
        make_item(ARTICLES + k, title=f"new {k}", crawled_msec=int(time.time() * 1000) - k)
        for k in range(1, 41)
    ]
    client = FakeReader(new, unread=unread + [i.id for i in new])
    ctx = run.Context(cfg, Secrets(), conn)
    ctx._client = client
    ctx._llm = FakeLLM(cfg, conn)
    t0 = time.perf_counter()
    res = run.run(ctx, enforce=True)
    _budget("run, non-network phases", time.perf_counter() - t0, 5.0)
    assert res.ok and res.new == 40 and res.scored >= 40


def test_feedback_collect_linear_at_50k(world):
    """Half the pile going read in one run (a mass flip, held for a second sighting) and
    then confirmed: the pass stays well under a second each time, 25,000 articles or not."""
    cfg, conn = world
    known = sorted(store.known_ids(conn))
    half = set(known[: len(known) // 2])
    client = FakeReader([], unread=sorted(half))
    t0 = time.perf_counter()
    feedback.collect(client, conn, cfg, half, set())
    first = time.perf_counter() - t0
    t0 = time.perf_counter()
    feedback.collect(client, conn, cfg, half, set())
    second = time.perf_counter() - t0
    _budget("feedback.collect with 25,000 unread", max(first, second), 1.0)
