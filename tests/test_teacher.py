"""The teacher's synchronous paths: the pool, the deadline, the second looks."""

from __future__ import annotations

import threading
import time

import pytest
from conftest import make_item

from bodh import store
from bodh.config import Secrets
from bodh.teacher import ModelConfigError, Teacher, Verdict


class CountingLLM:
    """Judges after a short sleep, counting how many calls run at once."""

    def __init__(self, delay: float = 0.05, fail_with: Exception | None = None):
        self.delay, self.fail_with = delay, fail_with
        self.calls, self.in_flight, self.peak = 0, 0, 0
        self.lock = threading.Lock()
        self.plain_json = False

    def budget_left(self):
        return 100.0

    def judge(self, model, system_text, user_text, run_id, purpose):
        import re

        with self.lock:
            self.calls += 1
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            time.sleep(self.delay)
            if self.fail_with is not None:
                raise self.fail_with
            ns = [int(m) for m in re.findall(r"^\[(\d+)\]$", user_text, re.M)]
            return [Verdict(n=n, quality=60, learning=60, fit=60, breadth=60, relevance=60) for n in ns]
        finally:
            with self.lock:
                self.in_flight -= 1

    def request_params(self, *a, **k):
        return {}


def _pending(conn, n: int) -> None:
    for i in range(1, n + 1):
        store.upsert_article(conn, make_item(i, title=f"article {i}", crawled_msec=1_800_000_000_000 + i), None)


def test_score_pending_at_most_workers_in_flight_and_deadline_starts_no_new_call(cfg, conn):
    cfg.judge.articles_per_call = 1
    _pending(conn, 12)
    llm = CountingLLM(delay=0.03)
    teacher = Teacher(cfg, llm, conn)  # type: ignore[arg-type]
    teacher.batch_mode = False
    stats = teacher.score_pending(None, workers=3)
    assert stats["scored"] == 12 and llm.peak <= 3
    # a deadline already past: nothing new starts, and what was never started stays pending
    _pending(conn, 20)
    llm = CountingLLM(delay=0.01)
    teacher = Teacher(cfg, llm, conn)  # type: ignore[arg-type]
    teacher.batch_mode = False
    stats = teacher.score_pending(None, workers=4, stop_after=time.monotonic() - 1)
    assert stats["stopped_early"] and llm.calls == 0 and stats["pending_after"] == 8


def test_score_pending_model_config_error_cancels_queue(cfg, conn):
    """A rejected key used to let the pool run through its queued calls (17 rejected calls in
    a blast); the first rejection empties the queue."""
    cfg.judge.articles_per_call = 1
    _pending(conn, 10)
    llm = CountingLLM(delay=0.01, fail_with=ModelConfigError("rejected"))
    teacher = Teacher(cfg, llm, conn)  # type: ignore[arg-type]
    teacher.batch_mode = False
    with pytest.raises(ModelConfigError):
        teacher.score_pending(None, workers=2)
    assert llm.calls <= 2


def test_second_looks_record_each_result_as_it_lands(cfg, conn):
    """Sync second looks were applied only after the whole pool finished: an interrupt threw
    every paid look away and the next run paid again."""
    from conftest import seed_scored

    cfg.autocut.cutoff = 35
    ids = seed_scored(conn, cfg, [dict(n=i, score=30) for i in range(1, 4)])
    conn.execute("UPDATE articles SET text = ?", ("x" * 5000,))
    rows = store.unread_scored(conn)
    calls = {"n": 0}

    class Interrupting:
        plain_json = False

        def budget_left(self):
            return 100.0

        def judge(self, model, system_text, user_text, run_id, purpose):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            return [Verdict(n=1, quality=70, learning=70, fit=70, breadth=70, relevance=70)]

        def request_params(self, *a, **k):
            return {}

    teacher = Teacher(cfg, Interrupting(), conn)  # type: ignore[arg-type]
    with pytest.raises(KeyboardInterrupt):
        teacher.second_looks(rows, None, workers=1)
    looked = [r for r in store.unread_scored(conn) if r["second_look_score"] is not None]
    assert len(looked) == 1 and looked[0]["score"] == 70.0 and looked[0]["second_look_score"] == 70.0
    assert ids and calls["n"] == 2
    # the cap: with a limit of one look per run the other close calls are left unexamined, unread
    teacher = Teacher(cfg, CountingLLM(delay=0.0), conn)  # type: ignore[arg-type]
    rows = [r for r in store.unread_scored(conn) if r["second_look_score"] is None]
    results = teacher.second_looks(rows, None, max_looks=1)
    assert len(results) == 1 and list(results.values()) == [True]   # 60 clears the floor of 35
    assert len([r for r in store.unread_scored(conn) if r["second_look_score"] is None]) == 1


def test_secrets_shape_for_llm(cfg, conn):
    from bodh.teacher import LLM, TeacherError

    with pytest.raises(TeacherError):
        LLM(cfg, Secrets(), conn)
