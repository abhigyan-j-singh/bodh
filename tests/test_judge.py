"""The teacher's synchronous paths: the pool, the deadline, the second looks."""

from __future__ import annotations

import time

import anthropic
import pytest
from fakes import FakeLLM, api_error, make_item

from bodh import store
from bodh.config import Secrets
from bodh.judge import Judge
from bodh.llm import ModelConfigError


def _pending(conn, n: int) -> None:
    for i in range(1, n + 1):
        store.upsert_article(
            conn, make_item(i, title=f"article {i}", crawled_msec=1_800_000_000_000 + i), None
        )


def test_score_pending_at_most_workers_in_flight_and_deadline_starts_no_new_call(cfg, conn):
    cfg.judge.articles_per_call = 1
    _pending(conn, 12)
    llm = FakeLLM(cfg, conn)
    llm.api.delay = 0.03
    teacher = Judge(cfg, llm, conn)
    teacher.batch_mode = False
    stats = teacher.score_pending(None, workers=3)
    assert stats["scored"] == 12 and 1 < llm.api.peak <= 3
    # a deadline already past: nothing new starts, and what was never started stays pending
    _pending(conn, 20)
    llm = FakeLLM(cfg, conn)
    teacher = Judge(cfg, llm, conn)
    teacher.batch_mode = False
    stats = teacher.score_pending(None, workers=4, stop_after=time.monotonic() - 1)
    assert stats["stopped_early"] and llm.calls == [] and stats["pending_after"] == 8


def test_score_pending_model_config_error_cancels_queue(cfg, conn):
    """A rejected key used to let the pool run through its queued calls (17 rejected calls in
    a blast); the first rejection empties the queue."""
    cfg.judge.articles_per_call = 1
    _pending(conn, 10)
    llm = FakeLLM(cfg, conn)
    llm.api.delay = 0.01
    llm.api.fail_with = api_error(anthropic.AuthenticationError, "invalid x-api-key", 401)
    teacher = Judge(cfg, llm, conn)
    teacher.batch_mode = False
    with pytest.raises(ModelConfigError):
        teacher.score_pending(None, workers=2)
    assert len(llm.calls) <= 2


def test_second_looks_record_each_result_as_it_lands(cfg, conn):
    """Sync second looks were applied only after the whole pool finished: an interrupt threw
    every paid look away and the next run paid again."""
    from conftest import seed_scored

    cfg.autocut.cutoff = 35
    ids = seed_scored(conn, cfg, [dict(n=i, score=30) for i in range(1, 4)])
    conn.execute("UPDATE articles SET text = ?", ("x" * 5000,))
    rows = store.unread_scored(conn)
    llm = FakeLLM(cfg, conn)
    llm.api.fail_calls[2] = KeyboardInterrupt()  # Ctrl-C while the second look is in flight
    teacher = Judge(cfg, llm, conn)
    with pytest.raises(KeyboardInterrupt):
        teacher.reviews(rows, None, workers=1)
    looked = [r for r in store.unread_scored(conn) if r["second_look_score"] is not None]
    assert len(looked) == 1 and looked[0]["score"] == 55.0 and looked[0]["second_look_score"] == 55.0
    assert ids and len(llm.calls) == 2
    # the cap: with a limit of one look per run the other close calls are left unexamined, unread
    teacher = Judge(cfg, FakeLLM(cfg, conn), conn)
    rows = [r for r in store.unread_scored(conn) if r["second_look_score"] is None]
    results = teacher.reviews(rows, None, max_looks=1)
    assert len(results) == 1 and list(results.values()) == [True]  # 55 clears the cut-off of 35
    assert len([r for r in store.unread_scored(conn) if r["second_look_score"] is None]) == 1


def test_secrets_shape_for_llm(cfg, conn):
    from bodh.llm import LLM, JudgeError

    with pytest.raises(JudgeError):
        LLM(cfg, Secrets(), conn)
