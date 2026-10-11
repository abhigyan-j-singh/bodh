"""End to end with a fake BazQux and a fake model: a run in shadow mode, a run that
cuts, a second look that saves a close call, a prune, an undo, a learner rewrite;
then the same through the batch path, where judgments arrive one run later."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from conftest import FakeClient, make_item

from triage import learner, pipeline, store
from triage.bazqux import Subscription
from triage.config import Secrets
from triage.teacher import Verdict


class FakeBazQux(FakeClient):
    def __init__(self, items, folders=None, **kw):
        super().__init__(**kw)
        self.items = {i.id: i for i in items}
        self.folders = folders or {}

    def subscriptions(self):
        return [Subscription(stream_id=i.feed_stream_id, title=i.feed_title, html_url="", folders=[self.folders[i.feed_title]] if i.feed_title in self.folders else [])
                for i in self.items.values()]

    def contents(self, ids):
        found = [self.items[i] for i in ids if i in self.items]
        return found, [i for i in ids if i not in self.items]

    def user_info(self):
        return {"userEmail": "test@example.com"}


# the shapes the Batches API hands back, reduced to what the teacher reads
@dataclass
class FakeText:
    text: str
    type: str = "text"


@dataclass
class FakeUsage:
    input_tokens: int = 1000
    output_tokens: int = 100
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeMessage:
    content: list
    usage: FakeUsage = field(default_factory=FakeUsage)


@dataclass
class FakeSucceeded:
    message: FakeMessage
    type: str = "succeeded"


@dataclass
class FakeErrorBody:
    message: str


@dataclass
class FakeError:
    error: FakeErrorBody


@dataclass
class FakeErrored:
    error: FakeError
    type: str = "errored"


@dataclass
class FakeResponse:
    custom_id: str
    result: object


def score_for(title: str, purpose: str) -> int:
    """'gold' is excellent, 'junk' is junk, 'edge' sits just under the floor on the first
    pass and is rescued by the second look, 'edge2' is confirmed junk by the second look."""
    title = title.lower()
    if "gold" in title:
        return 90
    if "junk" in title:
        return 10
    if "edge2" in title:
        return 34 if purpose == "second_look" else 33
    if "edge" in title:
        return 60 if purpose == "second_look" else 33
    return 55


class FakeLLM:
    """Scores by title, synchronously or through a pretend Batches API."""

    def __init__(self):
        self.calls = []
        self.batches: dict[str, list[dict]] = {}
        self.status: dict[str, str] = {}
        self.fail_ids: set[str] = set()   # custom_ids whose result is an error
        self.recorded = []

    # budget
    def budget_left(self):
        return 100.0

    def ensure_budget(self):
        return None

    def spent_today(self):
        return 0.0

    # synchronous
    def judge(self, model, system_text, user_text, run_id, purpose):
        self.calls.append((model, purpose))
        return self._verdicts(user_text, purpose)

    def _verdicts(self, user_text, purpose):
        out = []
        for m in re.finditer(r"\[(\d+)\]\n.*?\ntitle: (.*?)\n", user_text, re.S):
            n, title = int(m.group(1)), m.group(2)
            s = score_for(title, purpose)
            out.append(Verdict(n=n, quality=s, learning=s, fit=s, breadth=s, relevance=s, lesson=f"lesson {n}",
                               concept="gold things" if "gold" in title.lower() else f"topic {n % 3}", kind="reporting",
                               flags=[], dupe_of=None, confidence="high"))
        return out

    def complete(self, model, system_text, user_text, run_id, purpose, max_tokens=2000):
        self.calls.append((model, purpose))
        return ("## What the student has been learning\nGold things.\n\n## Moving past\nnothing\n\n"
                "## Concepts covered, last 90 days\ngold things\n\n## Expansion topics for next week\n- silver things: adjacent\n\n"
                "## Notes to the teacher\nbe kind")

    # batches
    def request_params(self, model, system_text, user_text, ttl=None):
        return {"model": model, "max_tokens": 8000, "system": [{"type": "text", "text": system_text}],
                "messages": [{"role": "user", "content": user_text}]}

    def submit_batch(self, requests):
        bid = f"msgbatch_{len(self.batches) + 1}"
        self.batches[bid] = list(requests)
        self.status[bid] = "in_progress"
        return bid

    def finish_all(self):
        for bid in self.status:
            self.status[bid] = "ended"

    def batch_status(self, bid):
        return self.status[bid]

    def batch_results(self, bid):
        out = []
        for req in self.batches[bid]:
            cid = req["custom_id"]
            if cid in self.fail_ids:
                out.append(FakeResponse(cid, FakeErrored(FakeError(FakeErrorBody("overloaded")))))
                continue
            purpose = "second_look" if cid.startswith("s") else "teacher"
            verdicts = self._verdicts(req["params"]["messages"][0]["content"], purpose)
            compact = [{"n": v.n, "s": [v.quality, v.learning, v.fit, v.breadth, v.relevance], "lesson": v.lesson,
                        "concept": v.concept, "kind": "news", "flags": [], "dupe": None, "conf": "high"} for v in verdicts]
            out.append(FakeResponse(cid, FakeSucceeded(FakeMessage([FakeText(json.dumps({"judgments": compact}))]))))
        return out

    def record(self, run_id, model, purpose, usage, discount=1.0):
        self.recorded.append((model, purpose, discount))
        return 0.0

    def cost(self, model, usage, discount=1.0):
        self.recorded.append((model, "batch", discount))
        return {"input_tokens": 1000, "output_tokens": 100, "cache_write": 0, "cache_read": 0}, 0.001 * discount

    def adapt_to_error(self, model, message):
        return False

    def retries(self, n):
        self.max_retries = n

    @staticmethod
    def message_text(message):
        return "".join(b.text for b in message.content)


def build(cfg, conn, titles, folders=None, starred=None, must_read=None, batch=False):
    cfg.models.batch = batch
    items = []
    for n, t in enumerate(titles, start=1):
        body = f"<p>{t} body text</p>"
        if "edge" in t.lower():  # a long article: the first pass saw only its opening
            body = "<p>" + f"{t} body text. " * 150 + "</p>"
        items.append(make_item(n, feed=f"Feed {n % 2}", title=t, html=body, crawled_msec=1_800_000_000_000 + n))
    client = FakeBazQux(items, folders=folders, unread=[i.id for i in items], starred=starred or [],
                        labels={cfg.cut.must_read_stream: must_read or []})
    ctx = pipeline.Context(cfg, Secrets(), conn)
    ctx._client = client
    ctx._llm = FakeLLM()
    return ctx, client, items


# -- synchronous ----------------------------------------------------------------

def test_shadow_run_scores_and_tags_but_cuts_nothing(cfg, conn):
    ctx, client, items = build(cfg, conn, ["gold essay", "junk listicle", "edge case", "plain news"])
    res = pipeline.run(ctx, enforce=False)
    assert res.ok and res.new == 4 and res.scored == 4 and res.cut_floor == 0
    buckets = {name for name, _ in client.tagged}
    assert "score-90" in buckets and "score-0" in buckets
    assert client.read == []
    assert (cfg.root / cfg.dashboard_dir / "index.html").exists()
    # a new prompt version re-scores unread articles; a bucket that moved loses its old tag
    conn.execute("UPDATE scores SET prompt_version = 'teacher.v0', bucket = 'score-50', bucket_tagged = 1")
    res = pipeline.run(ctx, enforce=False)
    assert res.scored == 4
    assert ("score-50", sorted(i.id for i in items if "plain" not in i.title)) in [(n, sorted(ids)) for n, ids in client.untagged]
    assert store.article(conn, items[0].id)["bucket"] == "score-90"


def test_enforcing_run_cuts_junk_and_second_look_saves_edge(cfg, conn):
    cfg.cut.exploration_share = 0.0
    ctx, client, items = build(cfg, conn, ["gold essay", "junk listicle", "edge case", "plain news", "junk again",
                                           "edge2 confirmed", "short close call"])
    # a close call with nothing more to read is not looked at again: the first verdict stands
    short = items[6]
    conn.execute("UPDATE articles SET text = 'tiny' WHERE id = ?", (short.id,))
    res = pipeline.run(ctx, enforce=True)
    assert res.ok and res.second_looks == 2 and res.saved == 1
    cut_ids = set(client.read[0])
    assert cut_ids == {items[1].id, items[4].id, items[5].id}, cut_ids
    # tag before read, and the cut tag was written
    assert any(name == cfg.tags.cut and set(ids) == cut_ids for name, ids in client.tagged)
    edge = store.article(conn, items[2].id)
    assert edge["status"] == "unread" and edge["second_look_score"] == 60.0 and edge["score"] == 60.0
    assert ("claude-opus-5-5", "second_look") in ctx.llm.calls
    # the second run looks at nothing twice
    calls_before = len(ctx.llm.calls)
    res2 = pipeline.run(ctx, enforce=True)
    assert res2.second_looks == 0 and len(ctx.llm.calls) == calls_before


def test_protected_and_starred_survive_the_floor(cfg, conn):
    cfg.cut.exploration_share = 0.0
    ctx, client, items = build(cfg, conn, ["junk one", "junk two", "junk three"],
                               starred=[], must_read=[])
    client.starred = [items[0].id]
    client.labels[cfg.cut.must_read_stream] = [items[1].id]
    res = pipeline.run(ctx, enforce=True)
    assert res.cut_floor == 1 and client.read[0] == [items[2].id]


def test_prune_and_undo(cfg, conn):
    cfg.cut.target_unread = 2
    ctx, client, items = build(cfg, conn, ["gold a", "gold b", "plain c", "plain d", "plain e"])
    pipeline.run(ctx, enforce=False)
    n, sel = pipeline.prune(ctx, dry_run=True)
    assert n == 0 and len(sel.cut) == 3 and client.read == []
    n, sel = pipeline.prune(ctx)
    assert n == 3 and sel.boundary == 90.0
    assert store.status_ids(conn, "cut") == {items[2].id, items[3].id, items[4].id}
    run_id, restored = pipeline.undo_last(ctx)
    assert restored == 3 and store.status_ids(conn, "unread") == {i.id for i in items}
    assert any(name == cfg.tags.cut for name, _ in client.untagged)


def test_prune_tag_triggers_one_step(cfg, conn):
    cfg.cut.target_unread = 1
    cfg.cut.exploration_share = 0.0
    ctx, client, items = build(cfg, conn, ["gold a", "plain b", "plain c"])
    pipeline.run(ctx, enforce=False)
    client.labels[cfg.tags.prune] = [items[1].id]
    res = pipeline.run(ctx, enforce=True)
    assert res.cut_prune == 2 and res.feedback["prune_requested"]


def test_weekly_rewrite_applies_when_gate_passes(cfg, conn):
    ctx, client, items = build(cfg, conn, [f"gold {i}" for i in range(6)])
    pipeline.run(ctx, enforce=False)
    for it in items:
        store.add_feedback(conn, it.id, "star")
    result = learner.weekly_rewrite(cfg, ctx.llm, conn, None)
    assert result["applied"], result["reason"]
    assert "silver things" in cfg.read_learner()
    assert learner.expansion_topics(cfg.read_learner()) == ["silver things"]
    v = learner.revert(cfg, conn)
    assert v is not None and "silver things" not in cfg.read_learner()


def test_old_and_unscored_folders_are_never_judged(cfg, conn):
    from triage.config import FolderPolicy

    cfg.scoring.max_age_days = 14
    cfg.cut.folders = {"Comics": FolderPolicy(protected=True, score=False)}
    ctx, client, items = build(cfg, conn, ["plain a", "plain b", "comic c"], folders={"Feed 1": "Comics"})
    # the first article is three weeks old
    conn.execute("UPDATE articles SET crawled_msec = ? WHERE id = ?", (0, items[0].id))
    store.upsert_article(conn, items[0], None)  # upsert keeps the given crawled_msec
    conn.execute("UPDATE articles SET crawled_msec = 1 WHERE id = ?", (items[0].id,))
    res = pipeline.run(ctx, enforce=False)
    scored = store.scored_ids(conn)
    assert items[1].id in scored and items[0].id not in scored and items[2].id not in scored
    assert res.scored == 1 and res.pending == 0


# -- batched ----------------------------------------------------------------------

def test_batch_run_submits_then_collects(cfg, conn):
    cfg.scoring.batch_size = 2
    ctx, client, items = build(cfg, conn, ["gold essay", "junk listicle", "plain news"], batch=True)
    res = pipeline.run(ctx, enforce=False)
    assert res.batch_mode and res.scored == 0 and res.submitted == 3 and res.inflight == 3 and res.pending == 0
    assert len(ctx.llm.batches) == 1 and len(next(iter(ctx.llm.batches.values()))) == 2  # 3 articles, 2 requests
    assert client.tagged == []  # nothing judged yet, nothing tagged
    # the batch has not finished: the next run neither scores nor resubmits
    res = pipeline.run(ctx, enforce=False)
    assert res.scored == 0 and res.submitted == 0 and res.inflight == 3
    ctx.llm.finish_all()
    res = pipeline.run(ctx, enforce=False)
    assert res.scored == 3 and res.inflight == 0 and res.pending == 0
    assert {name for name, _ in client.tagged} >= {"score-90", "score-0"}
    assert store.article(conn, items[0].id)["score"] == 90.0
    assert [b["status"] for b in store.recent_batches(conn)] == ["done"]
    assert ctx.llm.recorded and ctx.llm.recorded[0][2] == cfg.models.batch_discount


def test_batch_waits_for_a_fuller_batch_unless_articles_are_old(cfg, conn):
    cfg.scoring.batch_size = 20
    ctx, client, items = build(cfg, conn, ["plain a", "plain b"], batch=True)
    res = pipeline.run(ctx, enforce=False)
    assert res.submitted == 0 and "wait for a fuller batch" in " ".join(res.notes)
    conn.execute("UPDATE articles SET fetched_at = '2020-01-01T00:00:00+00:00'")
    res = pipeline.run(ctx, enforce=False)
    assert res.submitted == 2


def test_batch_second_look_holds_then_decides(cfg, conn):
    cfg.cut.exploration_share = 0.0
    cfg.scoring.batch_size = 2
    ctx, client, items = build(cfg, conn, ["gold essay", "junk listicle", "edge case", "edge2 confirmed"], batch=True)
    pipeline.run(ctx, enforce=True)            # submits the first pass
    ctx.llm.finish_all()
    res = pipeline.run(ctx, enforce=True)      # judgments land; junk goes; close calls are held
    assert res.scored == 4 and res.cut_floor == 1 and client.read[0] == [items[1].id]
    assert res.held == 2 and res.second_looks == 0
    assert len(store.inflight_ids(conn, "second_look")) == 2
    res = pipeline.run(ctx, enforce=True)      # second looks not back yet: still held, not resubmitted
    assert res.cut_floor == 0 and res.held == 0 and len(ctx.llm.batches) == 2
    ctx.llm.finish_all()
    res = pipeline.run(ctx, enforce=True)      # second looks land: edge saved, edge2 cut
    assert res.second_looks == 2 and res.saved == 1 and res.cut_floor == 1
    assert client.read[-1] == [items[3].id]
    edge = store.article(conn, items[2].id)
    assert edge["status"] == "unread" and edge["score"] == 60.0 and edge["second_look_score"] == 60.0


def test_batch_errors_go_back_to_pending_and_failed_second_looks_stay_unread(cfg, conn):
    cfg.cut.exploration_share = 0.0
    cfg.scoring.batch_size = 1
    ctx, client, items = build(cfg, conn, ["junk a", "junk b", "edge case"], batch=True)
    res = pipeline.run(ctx, enforce=True)
    first = next(iter(ctx.llm.batches.values()))
    carrying_junk_a = next(r["custom_id"] for r in first if "junk a" in r["params"]["messages"][0]["content"])
    ctx.llm.fail_ids.add(carrying_junk_a)   # that request errors
    ctx.llm.finish_all()
    res = pipeline.run(ctx, enforce=True)
    # junk b judged and cut, edge judged and held, junk a pending again and resubmitted at once
    assert res.scored == 2 and res.cut_floor == 1 and res.held == 1 and res.submitted == 1
    assert "requests failed" in " ".join(res.notes)
    assert [b["status"] for b in store.recent_batches(conn)][-1] == "partial"
    ctx.llm.fail_ids.clear()
    ctx.llm.fail_ids |= {r["custom_id"] for b in ctx.llm.batches.values() for r in b if r["custom_id"].startswith("s")}
    ctx.llm.finish_all()
    res = pipeline.run(ctx, enforce=True)
    # junk a lands and goes; the second look failed, so edge counts as examined and stays unread
    assert res.scored == 1 and res.cut_floor == 1 and res.second_looks == 0
    assert store.article(conn, items[2].id)["status"] == "unread"
    assert conn.execute("SELECT second_look_model FROM scores WHERE article_id = ?", (items[2].id,)).fetchone()[0] == "failed"
    res = pipeline.run(ctx, enforce=True)   # a failed look is not retried, and the article is not cut
    assert len(store.inflight_ids(conn, "second_look")) == 0 and res.held == 0 and res.cut_floor == 0


def test_sync_flag_overrides_batch_mode(cfg, conn):
    ctx, client, items = build(cfg, conn, ["gold essay", "junk listicle"], batch=True)
    res = pipeline.run(ctx, enforce=False, sync=True)
    assert not res.batch_mode and res.scored == 2 and ctx.llm.batches == {}


# -- the blast ------------------------------------------------------------------

def test_blast_judges_everything_in_rounds_and_tags(cfg, conn):
    cfg.scoring.batch_size = 2
    ctx, client, items = build(cfg, conn, [f"plain {i}" for i in range(7)] + ["gold essay"], batch=True)
    pipeline.run(ctx, enforce=False)                  # ingest; batch mode would only submit
    for bid in list(ctx.llm.batches):                 # pretend nothing was ever submitted
        store.close_batch(conn, bid, "lost")
    seen = []
    total = pipeline.blast(ctx, workers=3, chunk=3, on_progress=lambda t, s: seen.append((t["rounds"], s["scored"])))
    assert total["scored"] == 8 and total["rounds"] == 3 and total["pending"] == 0 and not total["interrupted"]
    assert seen and seen[-1][0] == 2
    assert {name for name, _ in client.tagged} >= {"score-90", "score-50"}
    assert ctx.llm.batches and all(store.recent_batches(conn)[0]["status"] != "submitted" for _ in [0])
    runs = store.last_runs(conn, 1)
    assert runs[0]["kind"] == "blast" and runs[0]["ok"] and "judged 8" in runs[0]["summary"]
    assert (cfg.root / cfg.dashboard_dir / "progress.json").exists()
    assert store.kv_get(conn, "phase") == ""


def test_blast_all_ages_and_budget_and_interrupt(cfg, conn):
    cfg.scoring.batch_size = 1
    cfg.models.daily_budget_usd = 1.0
    ctx, client, items = build(cfg, conn, ["plain a", "plain b", "plain c"], batch=False)
    conn.execute("INSERT INTO articles (id, title, crawled_msec, fetched_at, text, feed_title) VALUES ('old1', 'ancient', 1, '2020-01-01T00:00:00+00:00', 'body', 'f')")
    conn.execute("INSERT INTO state (article_id, status) VALUES ('old1', 'unread')")
    client.unread.append("old1")
    pipeline.run(ctx, enforce=False)   # judges the three recent ones synchronously; old1 is outside the window
    assert store.article(conn, "old1")["score"] is None
    total = pipeline.blast(ctx, workers=2, all_ages=True, budget=25.0)
    assert total["scored"] == 1 and store.article(conn, "old1")["score"] == 55.0
    assert cfg.models.daily_budget_usd == 25.0 and cfg.scoring.max_age_days is None

    # Ctrl-C in the middle leaves what was judged and marks the run interrupted
    for n in (11, 12):
        item = make_item(n, title=f"plain {n}", crawled_msec=1_800_000_000_000 + n)
        store.upsert_article(conn, item, None)
        client.unread.append(item.id)
    calls = {"n": 0}
    real = ctx.llm.judge

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real(*a, **k)

    ctx.llm.judge = flaky
    total = pipeline.blast(ctx, workers=1)
    assert total["interrupted"] and total["scored"] == 1 and total["pending"] == 1
    assert not store.last_runs(conn, 1)[0]["ok"] and "interrupted" in store.last_runs(conn, 1)[0]["summary"]


def test_blast_with_a_stand_in_model_then_the_teacher_rejudges_the_promising(cfg, conn):
    cfg.scoring.batch_size = 2
    cfg.models.prices_per_mtok["fast-model"] = [0.1, 0.5, 0.01]
    ctx, client, items = build(cfg, conn, ["gold essay", "junk listicle", "plain news", "gold again"], batch=False)
    # the teacher has already judged the first article; the stand-in must leave it alone
    pipeline.run(ctx, enforce=False, score_limit=1)
    first = store.article(conn, items[3].id)  # newest first: 'gold again' was the one judged
    assert first["score"] == 90.0 and first["model"] == "claude-sonnet-5-5"
    total = pipeline.blast(ctx, workers=2, model="fast-model")
    assert total["scored"] == 3 and cfg.models.teacher == "fast-model"
    assert store.article(conn, items[3].id)["model"] == "claude-sonnet-5-5"   # untouched
    assert store.article(conn, items[0].id)["model"] == "fast-model"
    cfg.models.teacher = "claude-sonnet-5-5"   # a new process reads the config afresh
    # pending for the teacher now: the stand-in's 50+ (gold 90, plain 55), not the junk (10)
    from triage.teacher import Teacher

    rule = Teacher.rejudge_rule(cfg)
    assert rule == ("claude-sonnet-5-5", 50.0)
    pending = {r["id"] for r in store.unscored_unread(conn, cfg.scoring.prompt_version, rejudge=rule)}
    assert pending == {items[0].id, items[2].id}
    res = pipeline.run(ctx, enforce=False)
    assert res.scored == 2
    assert store.article(conn, items[0].id)["model"] == "claude-sonnet-5-5"
    assert store.article(conn, items[1].id)["model"] == "fast-model"   # junk stays with the stand-in's verdict
    assert store.unscored_unread(conn, cfg.scoring.prompt_version, rejudge=rule) == []
    from triage import progress

    snap = progress.snapshot(conn, cfg)
    assert snap["stand_in"] == 1 and snap["rejudge"] == 0 and "stand-in" in progress.render_text(snap)


def test_blast_stops_at_the_time_limit(cfg, conn):
    cfg.scoring.batch_size = 1
    ctx, client, items = build(cfg, conn, [f"plain {i}" for i in range(6)], batch=False)
    pipeline.run(ctx, enforce=False, score_limit=0 or None)  # judges all six synchronously
    for n in range(20, 26):
        item = make_item(n, title=f"plain {n}", crawled_msec=1_800_000_000_000 + n)
        store.upsert_article(conn, item, None)
    real = ctx.llm.judge

    def slow(*a, **k):
        import time as _t
        _t.sleep(0.05)
        return real(*a, **k)

    ctx.llm.judge = slow
    total = pipeline.blast(ctx, workers=1, max_seconds=0.12)
    assert total["timed_out"] and 0 < total["scored"] < 6 and total["pending"] == 6 - total["scored"]
    assert "time limit" in store.last_runs(conn, 1)[0]["summary"]
    total = pipeline.blast(ctx, workers=3)
    assert total["pending"] == 0 and not total["timed_out"]


# -- the student's rescue and the undo hold against the next floor -----------------

def test_rescued_article_survives_the_floor(cfg, conn):
    cfg.cut.exploration_share = 0.0
    ctx, client, items = build(cfg, conn, ["gold essay", "junk listicle", "plain news"])
    res = pipeline.run(ctx, enforce=True)
    junk = items[1].id
    assert res.cut_floor == 1 and store.article(conn, junk)["status"] == "cut"
    client.labels[cfg.tags.wrong] = [junk]          # the student tags it `wrong` in BazQux
    res = pipeline.run(ctx, enforce=True)
    assert res.feedback["rescues"] == 1 and res.cut_floor == 0
    row = store.article(conn, junk)
    assert row["status"] == "unread" and row["rescued_at"]
    assert [k for k, _ in store.feedback_for(conn, junk)] == ["rescue", "wrong"]
    assert client.unread_marked[-1] == [junk] and (cfg.tags.wrong, [junk]) in client.untagged
    res = pipeline.run(ctx, enforce=True)              # and it stays
    assert res.cut_floor == 0 and store.article(conn, junk)["status"] == "unread"


def test_undo_is_not_reversed_by_the_next_floor(cfg, conn):
    cfg.cut.exploration_share = 0.0
    ctx, client, items = build(cfg, conn, ["gold essay", "junk listicle", "junk again"])
    res = pipeline.run(ctx, enforce=True)
    assert res.cut_floor == 2
    run_id, restored = pipeline.undo_last(ctx)
    assert restored == 2
    res = pipeline.run(ctx, enforce=True)
    assert res.cut_floor == 0 and store.status_ids(conn, "cut") == set()


def test_tripped_breaker_blocks_later_floors_and_shows_in_status(cfg, conn):
    from triage import control, cut

    cfg.breakers.min_new_for_breaker = 3
    ctx, client, items = build(cfg, conn, ["junk a", "junk b", "junk c", "junk d"])
    import pytest

    with pytest.raises(cut.BreakerTripped):
        pipeline.run(ctx, enforce=True)
    assert client.read == [] and not store.last_runs(conn, 1)[0]["ok"]
    res = pipeline.run(ctx, enforce=True)
    assert res.ok and res.cut_floor == 0 and "safety reset" in " ".join(res.notes)
    assert "BREAKER TRIPPED" in control.render_status_text(control.status(cfg, conn))
    assert cut.reset_breaker(conn)
    res = pipeline.run(ctx, enforce=True)   # nothing newly scored: the share test does not apply; the floor cuts
    assert res.cut_floor == 4


def test_must_read_protection_survives_label_failure(cfg, conn):
    """A failed read of the Must read stream used to clear every protection and let the floor
    cut a protected article while the run reported ok."""
    import pytest

    from triage.bazqux import BazQuxError

    cfg.cut.exploration_share = 0.0
    ctx, client, items = build(cfg, conn, ["junk one", "junk two"])
    client.labels[cfg.cut.must_read_stream] = [items[0].id]
    res = pipeline.run(ctx, enforce=True)
    assert res.cut_floor == 1 and client.read[0] == [items[1].id]
    assert store.article(conn, items[0].id)["protected"] == 1
    # the stream cannot be read on the next run: the protection stands and nothing is cut
    client.labels[cfg.cut.must_read_stream] = BazQuxError("HTTP 500 from stream/items/ids")
    res = pipeline.run(ctx, enforce=True)
    assert res.ok and res.cut_floor == 0 and len(client.read) == 1
    assert store.article(conn, items[0].id)["protected"] == 1
    assert "could not read the Must read stream" in " ".join(res.notes)
    with pytest.raises(BazQuxError):
        pipeline.bankruptcy(ctx, 0, dry_run=False)
    assert len(client.read) == 1 and not store.last_runs(conn, 1)[0]["ok"]
    # the stream answers again: the article is still protected, and is not cut
    client.labels[cfg.cut.must_read_stream] = [items[0].id]
    res = pipeline.run(ctx, enforce=True)
    assert res.cut_floor == 0 and store.article(conn, items[0].id)["protected"] == 1


def test_prune_tag_survives_shadow_mode_and_a_failed_run(cfg, conn):
    cfg.cut.target_unread = 1
    cfg.cut.exploration_share = 0.0
    ctx, client, items = build(cfg, conn, ["gold a", "plain b", "plain c"])
    client.labels[cfg.tags.prune] = [items[1].id]
    res = pipeline.run(ctx, enforce=False)                      # shadow: the tag is removed, the request kept
    assert res.cut_prune == 0 and "waits for an enforcing run" in " ".join(res.notes)
    assert any(name == cfg.tags.prune for name, _ in client.untagged)
    client.labels[cfg.tags.prune] = []
    real = client.starred_ids

    def broken():
        raise RuntimeError("BazQux down")

    client.starred_ids = broken
    import pytest

    with pytest.raises(RuntimeError):
        pipeline.run(ctx, enforce=True)                         # a failed run keeps it too
    assert store.kv_get(conn, "prune_requested") is not None and client.read == []
    client.starred_ids = real
    res = pipeline.run(ctx, enforce=True)
    assert res.cut_prune == 2 and store.kv_get(conn, "prune_requested") is None
    res = pipeline.run(ctx, enforce=True)
    assert res.cut_prune == 0                                   # applied once


# -- the batch lifecycle under trouble --------------------------------------------

def test_vanished_batch_is_closed_as_lost(cfg, conn):
    """A batch deleted in the console (or submitted under a rotated key) used to fail every
    run forever as a ModelConfigError; it is closed as lost and its articles are pending again."""
    from triage.teacher import BatchNotFound

    cfg.scoring.batch_size = 2
    ctx, client, items = build(cfg, conn, ["gold essay", "junk listicle"], batch=True)
    res = pipeline.run(ctx, enforce=False)
    assert res.submitted == 2

    def gone(bid):
        raise BatchNotFound("batch not found: 404")

    ctx.llm.batch_status = gone
    res = pipeline.run(ctx, enforce=False)
    assert res.ok and "given up on" in " ".join(res.notes)
    assert [b["status"] for b in store.recent_batches(conn)][-1] == "lost"
    assert res.submitted == 2 and res.inflight == 2        # resubmitted at once


def test_ended_batch_with_unfetchable_results_is_given_up(cfg, conn):
    cfg.scoring.batch_size = 2
    ctx, client, items = build(cfg, conn, ["gold essay", "junk listicle"], batch=True)
    pipeline.run(ctx, enforce=False)
    ctx.llm.finish_all()

    def broken(bid):
        raise RuntimeError("results stream reset")

    ctx.llm.batch_results = broken
    res = pipeline.run(ctx, enforce=False)
    assert res.scored == 0 and store.recent_batches(conn)[0]["status"] == "submitted"   # young: kept open
    conn.execute("UPDATE batches SET created_at = '2020-01-01T00:00:00+00:00'")
    res = pipeline.run(ctx, enforce=False)
    assert store.recent_batches(conn)[-1]["status"] == "lost" and res.submitted == 2       # old: given up, resubmitted


def test_batch_mode_off_still_collects_open_batches(cfg, conn):
    cfg.scoring.batch_size = 2
    ctx, client, items = build(cfg, conn, ["gold essay", "junk listicle"], batch=True)
    pipeline.run(ctx, enforce=False)
    ctx.llm.finish_all()
    cfg.models.batch = False                                   # the owner turns batches off
    res = pipeline.run(ctx, enforce=False)
    assert not res.batch_mode and res.scored == 2 and res.inflight == 0 and ctx.llm.batches
    assert store.article(conn, items[0].id)["score"] == 90.0


def test_zero_verdict_batch_result_is_noted_and_resubmission_stops(cfg, conn):
    """A succeeded request with nothing parsable in it was silent, done, and resubmitted at
    once: a broken prompt cost half price every thirty minutes with no signal."""
    cfg.scoring.batch_size = 2
    ctx, client, items = build(cfg, conn, ["plain a", "plain b"], batch=True)
    real = ctx.llm._verdicts
    ctx.llm._verdicts = lambda user_text, purpose: []
    res = pipeline.run(ctx, enforce=False)
    assert res.submitted == 2
    for attempt in range(1, 4):
        ctx.llm.finish_all()
        res = pipeline.run(ctx, enforce=False)                  # collected: nothing in it; resubmitted or not
        assert res.scored == 0 and "no judgment" in " ".join(res.notes), attempt
        assert "partial" in [b["status"] for b in store.recent_batches(conn)]
        if attempt < 3:
            assert res.submitted == 2, attempt                  # tried again
        else:
            assert res.submitted == 0 and res.pending == 0      # given up on: not resubmitted
    assert store.given_up(conn) == 2
    from triage import progress

    assert "given up on" in progress.render_text(progress.snapshot(conn, cfg))
    ctx.llm._verdicts = real
    assert store.reset_judge_failures(conn) == 2                # `triage safety reset`
    res = pipeline.run(ctx, enforce=False)
    assert res.submitted == 2
    ctx.llm.finish_all()
    assert pipeline.run(ctx, enforce=False).scored == 2 and store.given_up(conn) == 0


def test_collect_counts_duplicate_results_once_and_is_idempotent_after_an_interrupt(cfg, conn):
    cfg.scoring.batch_size = 2
    ctx, client, items = build(cfg, conn, ["gold essay", "junk listicle"], batch=True)
    pipeline.run(ctx, enforce=False)
    ctx.llm.finish_all()
    real_results = ctx.llm.batch_results
    ctx.llm.batch_results = lambda bid: real_results(bid) * 2   # the same result listed twice
    # the first absorb is interrupted half way: nothing of it may stick
    from triage import teacher as teachermod

    real_absorb = teachermod.Teacher._absorb_teacher

    def interrupted(self, ids, verdicts):
        raise KeyboardInterrupt

    teachermod.Teacher._absorb_teacher = interrupted
    import pytest

    try:
        with pytest.raises(KeyboardInterrupt):
            pipeline.run(ctx, enforce=False)
    finally:
        teachermod.Teacher._absorb_teacher = real_absorb
    assert store.recent_batches(conn)[0]["status"] == "submitted"
    assert conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 0 and store.scored_ids(conn) == set()
    res = pipeline.run(ctx, enforce=False)
    assert res.scored == 2 and conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 1
    assert store.recent_batches(conn)[0]["status"] == "done"


def test_run_timeout_mid_scoring_stops_promptly(cfg, conn):
    """The 15-minute limit was one SIGALRM in the main thread while the pool kept queued calls
    going for minutes; the run now stops starting calls at its deadline and reports ok."""
    import time as _t

    cfg.scoring.batch_size = 1
    ctx, client, items = build(cfg, conn, [f"plain {i}" for i in range(8)], batch=False)
    real = ctx.llm.judge

    def slow(*a, **k):
        _t.sleep(0.03)
        return real(*a, **k)

    ctx.llm.judge = slow
    res = pipeline.run(ctx, enforce=False, workers=1, stop_after=_t.monotonic() + 0.1)
    assert res.ok and 0 < res.scored < 8 and "time limit near" in " ".join(res.notes)
    assert res.pending == 8 - res.scored and store.last_runs(conn, 1)[0]["ok"]
    res = pipeline.run(ctx, enforce=False, workers=4)
    assert res.pending == 0


def test_bankruptcy_dry_run_records_no_run_row(cfg, conn):
    ctx, client, items = build(cfg, conn, ["old a", "old b"])
    pipeline.run(ctx, enforce=False)
    client.unread_ids = lambda: [__import__("triage.bazqux", fromlist=["ItemRef"]).ItemRef(id=i.id, timestamp_usec=1) for i in items]
    before = len(store.last_runs(conn, 50))
    candidates, done = pipeline.bankruptcy(ctx, 1, dry_run=True)
    assert candidates == 2 and done == 0 and len(store.last_runs(conn, 50)) == before and client.read == []
    candidates, done = pipeline.bankruptcy(ctx, 1, dry_run=False)
    assert done == 2 and len(store.last_runs(conn, 50)) == before + 1 and len(client.read[0]) == 2
    # a rescued article is spared by bankruptcy
    pipeline.undo_last(ctx)
    store.restore_unread(conn, [items[0].id], rescued=True)
    client.unread = [i.id for i in items]
    candidates, done = pipeline.bankruptcy(ctx, 1, dry_run=False)
    assert done == 1 and client.read[-1] == [items[1].id]


def test_refresh_protection_is_linear(cfg, conn):
    """set(prot) was rebuilt inside a comprehension for every unread row: 23 s at 20,000."""
    import time as _t

    from conftest import make_item

    now = store.utcnow_iso()
    ids = [make_item(n).id for n in range(1, 50_001)]
    with store.transaction(conn):
        conn.executemany("INSERT INTO articles (id, title, folder, fetched_at) VALUES (?, 'a', 'F', ?)", [(i, now) for i in ids])
        conn.executemany("INSERT INTO state (article_id, status) VALUES (?, 'unread')", [(i,) for i in ids])
    ctx = pipeline.Context(cfg, Secrets(), conn)
    t0 = _t.monotonic()
    pipeline.refresh_protection(ctx, set(ids[:25_000]), set(ids[:10]))
    assert _t.monotonic() - t0 < 2.0
    assert conn.execute("SELECT COUNT(*) FROM state WHERE protected = 1").fetchone()[0] == 25_000


# -- the review's findings ---------------------------------------------------------

def test_absorb_adapts_to_a_rejected_shape_outside_its_transaction(cfg, conn):
    """adapt_to_error writes through the LLM's own connection; called inside the absorb's
    transaction it waited on that transaction's lock until it timed out, and every run after
    failed on the same batch."""
    from triage import store as storemod
    from triage.teacher import LLM

    cfg.scoring.batch_size = 1
    ctx, client, items = build(cfg, conn, ["junk a"], batch=True)
    pipeline.run(ctx, enforce=False)
    first = next(iter(ctx.llm.batches.values()))
    ctx.llm.fail_ids.add(first[0]["custom_id"])
    ctx.llm.finish_all()
    # a real LLM-style adapt_to_error: a write on a second connection to the same database
    other = storemod.connect(cfg.db_path)

    def adapt(model, message):
        other.execute("PRAGMA busy_timeout=500")
        storemod.kv_set(other, "plain_json", "1")
        return True

    ctx.llm.adapt_to_error = adapt
    res = pipeline.run(ctx, enforce=False)
    assert res.ok and storemod.kv_get(conn, "plain_json") == "1"
    assert store.recent_batches(conn)[-1]["status"] == "partial" and LLM is not None


def test_read_then_unread_again_is_not_cut_by_the_floor(cfg, conn):
    """The common reader gesture: read it, mark it unread to finish later. Its old score sits
    under the floor; the floor must leave it (a prune may still take it)."""
    cfg.cut.exploration_share = 0.0
    ctx, client, items = build(cfg, conn, ["gold essay", "junk listicle"])
    pipeline.run(ctx, enforce=False)
    junk = items[1].id
    client.mark_read([junk])                        # the student reads it
    res = pipeline.run(ctx, enforce=False)
    assert res.feedback["reads"] == 1 and store.article(conn, junk)["status"] == "read"
    client.mark_unread([junk])                      # and keeps it for later
    res = pipeline.run(ctx, enforce=True)
    assert res.feedback["returned"] == 1 and res.cut_floor == 0
    assert store.article(conn, junk)["status"] == "unread" and len(client.read) == 1   # only the student's own read
    cfg.cut.target_unread = 1
    n, sel = pipeline.prune(ctx)
    assert n == 1 and sel.cut_ids == [junk]        # the student's own prune still may


def test_tag_prune_waits_while_the_breaker_is_tripped(cfg, conn):
    from triage import cut

    cfg.breakers.min_new_for_breaker = 3
    cfg.cut.target_unread = 1
    ctx, client, items = build(cfg, conn, ["junk a", "junk b", "junk c", "junk d"])
    client.labels[cfg.tags.prune] = [items[0].id]
    import pytest

    with pytest.raises(cut.BreakerTripped):
        pipeline.run(ctx, enforce=True)
    res = pipeline.run(ctx, enforce=True)
    assert res.ok and res.cut_prune == 0 and client.read == [] and "waits for `triage safety reset`" in " ".join(res.notes)
    with pytest.raises(cut.BreakerTripped):
        pipeline.prune(ctx)                         # the student's own prune is refused too
    assert cut.reset_breaker(conn)
    res = pipeline.run(ctx, enforce=True)
    assert res.cut_floor == 4 and res.cut_prune == 0 and store.kv_get(conn, "prune_requested") is None


def test_wrong_on_a_pending_cut_rescues_it(cfg, conn):
    """A cut whose read mark failed is still unread in BazQux, carrying the cut tag; `wrong` on it
    used to record the positive only, and finish_pending_cuts then marked it read."""
    cfg.cut.exploration_share = 0.0
    ctx, client, items = build(cfg, conn, ["junk a", "junk b"])
    real = client.mark_read

    def failing(ids):
        raise RuntimeError("edit-tag 500")

    client.mark_read = failing
    import pytest

    with pytest.raises(RuntimeError):
        pipeline.run(ctx, enforce=True)              # tagged cut, not marked read
    client.mark_read = real
    assert store.cut_pending(conn)[1] and client.read == []
    client.labels[cfg.tags.wrong] = [items[0].id]
    res = pipeline.run(ctx, enforce=True)
    assert res.feedback["rescues"] == 1
    assert store.article(conn, items[0].id)["status"] == "unread" and store.article(conn, items[0].id)["rescued_at"]
    assert client.read == [[items[1].id]]            # the other pending cut is finished
    assert store.feedback_ids(conn, "rescue") == {items[0].id}


def test_pending_cut_moved_into_must_read_is_restored(cfg, conn):
    cfg.cut.exploration_share = 0.0
    ctx, client, items = build(cfg, conn, ["junk a", "junk b"])

    def failing(ids):
        raise RuntimeError("edit-tag 500")

    real = client.mark_read
    client.mark_read = failing
    import pytest

    with pytest.raises(RuntimeError):
        pipeline.run(ctx, enforce=True)
    client.mark_read = real
    client.labels[cfg.cut.must_read_stream] = [items[0].id]   # the student moved it into Must read meanwhile
    res = pipeline.run(ctx, enforce=True)
    assert res.ok and client.read == [[items[1].id]]
    assert store.article(conn, items[0].id)["status"] == "unread" and store.article(conn, items[0].id)["protected"] == 1


def test_prune_cuts_only_what_was_confirmed(cfg, conn):
    cfg.cut.target_unread = 1
    ctx, client, items = build(cfg, conn, ["gold a", "plain b", "plain c"])
    pipeline.run(ctx, enforce=False)
    _, shown = pipeline.prune(ctx, dry_run=True)
    assert len(shown.cut) == 2
    n, sel = pipeline.prune(ctx, confirmed={items[1].id})      # a run in between would have widened it
    assert n == 1 and sel.cut_ids == [items[1].id]
