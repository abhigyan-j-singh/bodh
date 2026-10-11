"""The compact judgment format, cost accounting, the request shapes, and the progress views."""

from __future__ import annotations

import json
from dataclasses import dataclass

from conftest import FakeClient, make_item, seed_scored

from bodh import locks, progress, store
from bodh.config import FolderPolicy, Secrets
from bodh.judge import Judge, has_more_to_read
from bodh.judgment import JudgmentIn, parse_judgments
from bodh.llm import LLM


def test_parse_compact_judgments_maps_codes_and_clamps():
    text = """```json
    {"judgments": [
      {"n": 1, "s": [120, -5, 60, 55, 40], "lesson": "Retrofits cost more: manifolds, CDUs, leak detection.",
       "concept": " Liquid Cooling Economics ", "kind": "Analysis", "flags": ["Primary", "INDIA", "bogus"], "dupe": null, "conf": "High"},
      {"n": 2, "s": [10, 10, 10, 10, 10], "lesson": "A press release.", "concept": "promo", "kind": "promo",
       "flags": ["vendor"], "dupe": 1, "conf": "medium"},
      {"n": 3, "s": [1, 2, 3], "lesson": "broken", "concept": "x", "kind": "news", "flags": [], "dupe": null, "conf": "low"}
    ]}
    ```"""
    out = parse_judgments(text)
    assert [v.n for v in out] == [1, 2]  # the three-score judgment is dropped, never guessed
    v = out[0]
    assert (v.quality, v.learning, v.fit, v.breadth, v.relevance) == (100, 0, 60, 55, 40)
    assert v.kind == "original_analysis" and v.flags == [
        "primary_source",
        "home_angle",
    ]  # "INDIA" is the old code
    assert v.concept == "liquid cooling economics" and v.confidence == "high"
    assert out[1].kind == "promo" and out[1].flags == ["vendor_marketing_no_data"] and out[1].dupe_of == 1
    assert parse_judgments("no json here") is None
    assert parse_judgments('{"judgments": "nope"}') == []


def test_judgment_schema_has_no_unsupported_constraints():
    from bodh.llm import _output_format

    fmt = _output_format()
    assert fmt["type"] == "json_schema"
    props = fmt["schema"]["$defs"]["JudgmentOut"]["properties"]
    assert set(props) == {"n", "s", "lesson", "concept", "kind", "flags", "dupe", "conf"}
    assert "minimum" not in str(fmt["schema"]) and props["kind"]["enum"][0] == "src"
    assert JudgmentIn(n=1, s=[1, 2, 3, 4, 5], kind="src", dupe="2").to_verdict().kind == "primary_source"
    assert JudgmentIn(n=1, s=[1, 2, 3, 4, 5], dupe="2").to_verdict().dupe_of == 2
    assert JudgmentIn(n=1, s=[1, 2, 3, 4, 5], dupe="x", conf="sure").to_verdict().confidence == "medium"


@dataclass
class U:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation: object = None


@dataclass
class CC:
    ephemeral_1h_input_tokens: int = 0
    ephemeral_5m_input_tokens: int = 0


def test_cost_accounting_with_cache_tiers_and_batch_discount(cfg, conn):
    llm = LLM(cfg, Secrets(anthropic_api_key="k"), conn)
    usage, usd = llm.cost(
        "claude-sonnet-5-5",
        U(
            input_tokens=1_000_000,
            output_tokens=100_000,
            cache_read_input_tokens=1_000_000,
            cache_creation=CC(ephemeral_1h_input_tokens=100_000, ephemeral_5m_input_tokens=100_000),
        ),
    )
    # 2.00 input + 1.00 output + 0.10 cache read + 0.40 (1h write at 2x) + 0.25 (5m write at 1.25x)
    assert abs(usd - 3.75) < 1e-6 and usage["cache_write"] == 200_000 and usage["cache_read"] == 1_000_000
    _, half = llm.cost("claude-sonnet-5-5", U(input_tokens=1_000_000), discount=0.5)
    assert abs(half - 1.0) < 1e-6
    # an older SDK without the breakdown: the total write is priced by the configured ttl
    _, usd1h = llm.cost("claude-sonnet-5-5", U(cache_creation_input_tokens=1_000_000))
    assert abs(usd1h - 4.0) < 1e-6
    assert cfg.models.price("claude-opus-5-5") == (4.0, 20.0)
    # an unknown model is never free: it is charged as the dearest known one until a price is added
    assert not cfg.models.has_price("nope") and cfg.models.price("nope") == (4.0, 20.0)
    assert cfg.models.cache_read_price("nope") == 0.2
    _, unknown = llm.cost("nope", U(input_tokens=1_000_000))
    assert abs(unknown - 4.0) < 1e-6
    assert cfg.models.cache_read_price("claude-haiku-5-5") == 0.01
    cfg.models.prices_per_mtok["old-style"] = [2.0, 10.0]
    assert abs(cfg.models.cache_read_price("old-style") - 0.2) < 1e-9


def test_request_params_shapes(cfg, conn):
    llm = LLM(cfg, Secrets(anthropic_api_key="k"), conn)
    p = llm.request_params("m", "SYS", "USER", ttl="1h")
    assert p["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert p["output_config"]["format"]["type"] == "json_schema" and p["messages"][0]["content"] == "USER"
    llm._enable_plain_json("m")
    p = llm.request_params("m", "SYS", "USER")
    assert "output_config" not in p and "JSON object" in p["messages"][0]["content"]
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert store.kv_get(conn, store.PLAIN_JSON_KEY) == '["m"]'
    again = LLM(cfg, Secrets(anthropic_api_key="k"), conn)
    assert again.plain_json("m") and not again.plain_json("other")  # remembered across runs, per model
    assert "output_config" in again.request_params("other", "SYS", "USER")
    # a rejected 1-hour cache falls back to the default cache, for that model, and that is remembered too
    assert llm.adapt_to_error("m", "cache_control: ttl is not supported") and not llm.long_ttl_ok("m")
    assert llm.request_params("m", "SYS", "USER", ttl="1h")["system"][0]["cache_control"] == {
        "type": "ephemeral"
    }
    assert llm.request_params("other", "SYS", "USER", ttl="1h")["system"][0]["cache_control"]["ttl"] == "1h"
    assert not LLM(cfg, Secrets(anthropic_api_key="k"), conn).long_ttl_ok("m")
    assert not llm.adapt_to_error("m", "something else")
    # the single flag written before October 2026 means every model
    store.kv_set(conn, store.PLAIN_JSON_KEY, "1")
    store.kv_set(conn, store.CACHE_TTL_KEY, "5m")
    old = LLM(cfg, Secrets(anthropic_api_key="k"), conn)
    assert old.plain_json("anything") and not old.long_ttl_ok("anything")


def test_has_more_to_read(cfg):
    cfg.judge.text_chars = 1000
    assert not has_more_to_read({"text": "x" * 1100}, cfg)
    assert has_more_to_read({"text": "x" * 1300}, cfg)
    assert has_more_to_read({"text_len": 5000}, cfg) and not has_more_to_read({"text_len": None}, cfg)


def test_teacher_prefix_is_stable_and_concepts_travel_with_the_articles(cfg, conn):
    class StubLLM:
        plain_json = False

        def request_params(self, model, system_text, user_text, ttl=None):
            return {"system": system_text, "user": user_text}

    t = Judge(cfg, StubLLM(), conn, context="Read: gpus: 3")  # type: ignore[arg-type]
    assert (
        "gpus: 3" not in t.system_text
        and "# About the student" in t.system_text
        and "# Learned profile" in t.system_text
    )
    text = t._user_text(
        [
            {
                "id": "a",
                "text": "body",
                "url": "",
                "feed_title": "f",
                "folder": None,
                "title": "T",
                "author": None,
                "published": None,
            }
        ],
        100,
    )
    assert text.startswith("# Concepts\nRead: gpus: 3") and "[1]\n" in text and "title: T" in text


def test_unscored_unread_filters(conn, cfg):
    old = make_item(1, title="old one", crawled_msec=1)
    new = make_item(2, title="new one", crawled_msec=4_000_000_000_000)
    comic = make_item(3, feed="Comics feed", title="a comic", crawled_msec=4_000_000_000_000)
    unknown = make_item(4, title="no dates")
    for item, folder in ((old, None), (new, None), (comic, "Comics"), (unknown, None)):
        store.upsert_article(conn, item, folder)
    assert {r["id"] for r in store.unscored_unread(conn, "teacher.v2")} == {
        old.id,
        new.id,
        comic.id,
        unknown.id,
    }
    within = store.unscored_unread(conn, "teacher.v2", max_age_days=14, skip_folders=["Comics"])
    assert {r["id"] for r in within} == {new.id, unknown.id}
    store.add_batch(conn, "b1", 1, "teacher", "m", [("t1-0", [new.id])])
    assert {r["id"] for r in store.unscored_unread(conn, "teacher.v2", 14, [], exclude_inflight=True)} == {
        comic.id,
        unknown.id,
    }
    assert store.batch_members(conn, "b1") == {"t1-0": [new.id]} and store.inflight_ids(conn, "teacher") == {
        new.id
    }
    store.close_batch(conn, "b1", "done")
    assert store.inflight_ids(conn, "teacher") == set() and store.recent_batches(conn)[0]["status"] == "done"


def test_progress_views_render(conn, cfg):
    cfg.folders = {"Comics": FolderPolicy(protected=True, judge=False)}
    seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=90, folder="Essays"),
            dict(n=2, score=20, folder="Essays"),
            dict(n=3, score=40, folder="News", confidence="low"),
        ],
    )
    store.upsert_article(conn, make_item(10, title="pending one", crawled_msec=4_000_000_000_000), "News")
    store.upsert_article(conn, make_item(11, title="ancient", crawled_msec=1), "News")
    store.upsert_article(
        conn, make_item(12, feed="Comics feed", title="comic", crawled_msec=4_000_000_000_000), "Comics"
    )
    flying = make_item(13, title="in flight", crawled_msec=4_000_000_000_000)
    store.upsert_article(conn, flying, "News")
    store.add_batch(conn, "msgbatch_abc", 1, "teacher", "claude-sonnet-5-5", [("t1-0", [flying.id])])
    store.record_cost(
        conn,
        1,
        "claude-sonnet-5-5",
        "teacher",
        {"input_tokens": 100, "output_tokens": 50, "cache_write": 10, "cache_read": 300},
        0.01,
    )
    run_id = store.begin_run(conn, "run")
    store.finish_run(conn, run_id, True, summary="judged 3")
    snap = progress.snapshot(conn, cfg)
    assert snap["unread"] == 7 and snap["judged_unread"] == 3 and snap["pending"] == 1
    assert snap["too_old"] == 1 and snap["skipped"] == 1 and snap["low_conf"] == 1 and snap["inflight"] == 1
    assert snap["cache_hit"] and abs(snap["cache_hit"] - 300 / 410) < 1e-6
    folders = {f["folder"]: f for f in snap["folders"]}
    assert (
        folders["News"]["pending"] == 1
        and folders["News"]["inflight"] == 1
        and folders["Essays"]["good"] == 1
    )
    assert folders["Comics"]["scored"] is False
    text = progress.render_text(snap, width=120)
    assert "judged" in text and "msgbatch_abc" in text and "Comics" in text and "in progress" in text
    html = progress.render_html(snap)
    assert "class='prog'" in html and "msgbatch_abc" in html


def test_fake_client_tracks_read_state():
    c = FakeClient(unread=["a", "b"])
    c.mark_read(["a"])
    assert [r.id for r in c.unread_ids()] == ["b"]
    c.mark_unread(["a"])
    assert {r.id for r in c.unread_ids()} == {"a", "b"}


def test_live_file_and_activity(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=90), dict(n=2, score=20), dict(n=3, score=40)])
    progress.set_phase(conn, "judging")
    path = progress.write_live(cfg, conn)
    assert path is not None and path.name == "progress.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["judged_unread"] == 3 and data["running"] is False and data["phase"] == ""  # no lock held
    assert data["judged_2min"] == 3 and data["rate_per_min"] == 1.5 and sum(data["series"]) == 3
    assert data["eta_min"] is None  # nothing pending
    # throttled writes
    assert progress.write_live(cfg, conn, min_interval=60) is None
    # a held lock shows as a run in progress, with its phase
    with locks.run_lock(cfg.root / cfg.data_dir):
        snap = progress.snapshot(conn, cfg)
        assert snap["running"] and snap["phase"] == "judging" and snap["run_started"]
        text = progress.render_text(snap)
        assert "RUN IN PROGRESS" in text and "judging" in text and "Last 3 h" in text
        html = progress.render_html(snap)
        assert (
            "dot on" in html and "progress.json" in html and "p-spark" in html and "run in progress" in html
        )
    assert ids and progress.log_tail(cfg.root / "nope.log") == []


def test_many_workers_share_the_database_safely(conn, cfg):
    """Sixteen threads judging through the real LLM wrapper (stub API) while the main thread
    records scores and writes the live file: no empty cursors, every cost recorded."""
    import threading as _th
    from dataclasses import dataclass, field

    from conftest import make_item

    @dataclass
    class StubUsage:
        input_tokens: int = 500
        output_tokens: int = 60
        cache_read_input_tokens: int = 2000
        cache_creation_input_tokens: int = 0
        cache_creation: object = None

    @dataclass
    class StubBlock:
        text: str
        type: str = "text"

    @dataclass
    class StubResponse:
        content: list
        usage: StubUsage = field(default_factory=StubUsage)

    class StubMessages:
        def __init__(self):
            self.calls = 0
            self.lock = _th.Lock()

        def create(self, **params):
            import re as _re
            import time as _t

            with self.lock:
                self.calls += 1
            _t.sleep(0.002)
            text = params["messages"][0]["content"]
            ns = [int(m) for m in _re.findall(r"^\[(\d+)\]$", text, _re.M)]
            js = [
                {
                    "n": n,
                    "s": [60, 60, 60, 60, 60],
                    "lesson": "l",
                    "concept": "c",
                    "kind": "news",
                    "flags": [],
                    "dupe": None,
                    "conf": "high",
                }
                for n in ns
            ]
            return StubResponse([StubBlock(json.dumps({"judgments": js}))])

    class StubClient:
        def __init__(self):
            self.messages = StubMessages()

        def with_options(self, **kw):
            return self

    for n in range(1, 241):
        store.upsert_article(
            conn, make_item(n, title=f"article number {n}", crawled_msec=1_800_000_000_000 + n), None
        )
    cfg.judge.articles_per_call = 5
    llm = LLM(cfg, Secrets(anthropic_api_key="k"), conn)
    llm._client = StubClient()
    judge = Judge(cfg, llm, conn)
    judge.batch_mode = False
    stats = judge.score_pending(1, workers=16, on_progress=progress.live_writer(cfg, conn, min_interval=0.0))
    assert stats["scored"] == 240 and stats["failed_batches"] == 0
    assert llm._client.messages.calls == 48
    assert conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 48
    assert llm.spent_today() > 0 and llm.budget_left() < cfg.models.daily_budget_usd
