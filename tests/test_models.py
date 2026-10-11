"""Phase 5 at the surface: the model strings in config.yaml, the keys command (never a value),
the models command (the roles, the providers, a comparison through two fakes), --rejudge on
run and catch-up, the per-model table on the dashboard, the doctor's per-provider key lines,
and the note in `bodh progress` when a provider reports no token counts."""

from __future__ import annotations

import os
import stat
import sys

import pytest
import yaml
from conftest import seed_scored
from fakes import FakeLLM, FakeOpenAIServer, FakeReader, make_item
from test_cli import (
    ENV_FILE,
    _env,
    _fakes,
    _project,
    _secrets_out_of_the_environment,  # noqa: F401  # the autouse fixture: no key outlives a test
    invoke,
)

from bodh import comparing, dashboard, progress, run, store
from bodh.config import load_config, load_secrets
from bodh.errors import ConfigError
from bodh.providers.base import ModelRef, Usage, model_name, same_model
from bodh.providers.openai import OpenAIProvider

OPENAI_KEY = "sk-openai-test-key-0123456789"


def _write_config(root, **models):
    (root / "config.yaml").write_text(yaml.safe_dump({"timezone": "UTC", "models": models}), encoding="utf-8")


# -- model strings --------------------------------------------------------------------


def test_model_string_forms(tmp_path):
    """A bare id is Anthropic's; `provider/model` names the provider; an OpenRouter id keeps its
    own slash; prices are keyed by the stored name, so `anthropic/x` and `x` are one entry."""
    assert ModelRef.parse("claude-sonnet-5-5") == ModelRef("anthropic", "claude-sonnet-5-5")
    assert ModelRef.parse("anthropic/claude-sonnet-5-5").name == "claude-sonnet-5-5"
    assert ModelRef.parse("openai/gpt-5-mini").name == "openai/gpt-5-mini"
    qwen = ModelRef.parse("openrouter/qwen/qwen3-235b")
    assert (qwen.provider, qwen.model, qwen.name) == (
        "openrouter",
        "qwen/qwen3-235b",
        "openrouter/qwen/qwen3-235b",
    )
    assert same_model("claude-sonnet-5-5", "anthropic/claude-sonnet-5-5")
    assert not same_model("claude-sonnet-5-5", "openai/claude-sonnet-5-5")
    assert model_name(" anthropic/claude-opus-5-5 ") == "claude-opus-5-5"
    _write_config(
        tmp_path,
        judge="gemini/gemini-2.5-pro",
        prices_per_mtok={"anthropic/claude-sonnet-5-5": [2, 10], "gemini/gemini-2.5-pro": [1.25, 10, 0.3]},
    )
    cfg = load_config(tmp_path)
    assert cfg.models.judge == "gemini/gemini-2.5-pro"
    assert set(cfg.models.prices_per_mtok) == {"claude-sonnet-5-5", "gemini/gemini-2.5-pro"}
    assert cfg.models.price("anthropic/claude-sonnet-5-5") == (2.0, 10.0)
    assert cfg.models.cache_read_price("gemini/gemini-2.5-pro") == 0.3
    assert cfg.models.has_price("openai/gpt-5") is False


@pytest.mark.parametrize(
    "bad",
    ["nope/some-model", "", "openai/", "/claude", "anthropic//x"],
)
def test_a_bad_model_string_is_a_config_error(tmp_path, bad):
    _write_config(tmp_path, judge=bad)
    with pytest.raises(ConfigError, match=r"models\.judge|provider|model"):
        load_config(tmp_path)
    _write_config(tmp_path, prices_per_mtok={bad or "x": [1, 2]})
    if bad:
        with pytest.raises(ConfigError):
            load_config(tmp_path)


def test_the_quick_judge_is_validated_too(tmp_path):
    (tmp_path / "config.yaml").write_text("timezone: UTC\ncatch_up:\n  model: nope/fast\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"catch_up\.model|provider"):
        load_config(tmp_path)


# -- keys ------------------------------------------------------------------------------


def test_keys_list_names_the_variables_and_never_a_value(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    _env(root)
    result = invoke(["keys"])  # `list` is the default
    assert result.exit_code == 0, result.output
    assert "anthropic          ANTHROPIC_API_KEY            set" in result.output
    assert "openai             OPENAI_API_KEY               not set" in result.output
    assert "openai-compatible  OPENAI_COMPATIBLE_BASE_URL   not set (the base URL" in result.output
    assert "sk-ant" not in result.output
    result = invoke(["keys", "frobnicate"])
    assert result.exit_code == 1 and "bodh keys set <provider>" in result.output
    result = invoke(["keys", "set", "nope"])
    assert result.exit_code == 1 and "unknown provider" in result.output


def test_keys_set_writes_env_owner_only_and_keeps_the_other_lines(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    _env(root)
    result = invoke(["keys", "set", "openai"], input=OPENAI_KEY + "\n")
    assert result.exit_code == 0, result.output
    assert OPENAI_KEY not in result.output and "OPENAI_API_KEY: written to" in result.output
    text = (root / ".env").read_text(encoding="utf-8")
    assert text.startswith(ENV_FILE) and text.endswith(f"OPENAI_API_KEY={OPENAI_KEY}\n")
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(root / ".env").st_mode) == 0o600
    # a second set replaces the line in place, nothing is duplicated
    result = invoke(["keys", "set", "openai"], input=OPENAI_KEY + "-2\n")
    assert result.exit_code == 0 and (root / ".env").read_text(encoding="utf-8").count("OPENAI_API_KEY=") == 1
    assert load_secrets(root).value("openai_api_key") == OPENAI_KEY + "-2"
    # nothing typed: nothing changes
    before = (root / ".env").read_text(encoding="utf-8")
    result = invoke(["keys", "set", "openai"], input="\n")
    assert result.exit_code == 1 and "unchanged" in result.output
    assert (root / ".env").read_text(encoding="utf-8") == before
    # a local service takes its base URL in the same breath
    result = invoke(["keys", "set", "openai-compatible"], input="\nhttp://localhost:11434/v1\n")
    assert result.exit_code == 1 and "unchanged" in result.output  # an empty key stops before the URL
    result = invoke(["keys", "set", "openai-compatible"], input="ollama\nhttp://localhost:11434/v1\n")
    assert result.exit_code == 0, result.output
    text = (root / ".env").read_text(encoding="utf-8")
    assert (
        "OPENAI_COMPATIBLE_API_KEY=ollama\n" in text
        and "OPENAI_COMPATIBLE_BASE_URL=http://localhost:11434/v1\n" in text
    )


def test_keys_set_keychain_without_keyring_is_a_config_error(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    _env(root)
    monkeypatch.setitem(sys.modules, "keyring", None)  # `import keyring` raises ImportError
    result = invoke(["keys", "set", "openai", "--keychain"], input=OPENAI_KEY + "\n")
    assert result.exit_code == 2 and "pip install bodh[keychain]" in result.output
    assert "OPENAI_API_KEY" not in (root / ".env").read_text(encoding="utf-8")


def test_keys_set_keychain_stores_under_the_service_name(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    _env(root)
    stored: dict[tuple[str, str], str] = {}

    class Keyring:
        @staticmethod
        def set_password(service, name, value):
            stored[(service, name)] = value

        @staticmethod
        def get_password(service, name):
            return stored.get((service, name))

    monkeypatch.setitem(sys.modules, "keyring", Keyring)
    result = invoke(["keys", "set", "gemini", "--keychain"], input="AIza-test-key-0123456789\n")
    assert result.exit_code == 0, result.output
    assert stored == {("bodh", "GEMINI_API_KEY"): "AIza-test-key-0123456789"}
    assert "GEMINI_API_KEY" not in (root / ".env").read_text(encoding="utf-8")
    # the lookup order: .env, the environment, then the keychain
    assert load_secrets(root).value("gemini_api_key") == "AIza-test-key-0123456789"


# -- models ----------------------------------------------------------------------------


def test_models_list_shows_roles_providers_prices_and_capabilities(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch, config="timezone: UTC\nmodels:\n  judge: openai/gpt-5-mini\n")
    _env(root)
    result = invoke(["models"])
    assert result.exit_code == 0, result.output
    assert "judge        openai/gpt-5-mini" in result.output
    assert "unpriced (charged as the dearest known)" in result.output and "(key not set)" in result.output
    assert (
        "reviewer     anthropic/claude-opus-5-5" in result.output and "batch, schema, cache" in result.output
    )
    assert "anthropic          key set" in result.output
    assert "openai             not set (OPENAI_API_KEY)" in result.output
    assert "claude-sonnet-5-5                        [2.0, 10.0, 0.1]" in result.output
    assert "budget: $8.00 a day across every provider" in result.output
    result = invoke(["models", "nope"])
    assert result.exit_code == 1 and "models compare --with" in result.output
    result = invoke(["models", "compare"])
    assert result.exit_code == 1 and "needs --with" in result.output


def _judged_unread(cfg, conn, n=6):
    """`n` unread articles judged by the judge, with text, for a comparison to re-judge."""
    ids = []
    for i in range(1, n + 1):
        title = "gold essay" if i % 2 else "junk listicle"
        item = make_item(i, feed="Feed A", title=f"{title} {i}", crawled_msec=1_800_000_000_000 + i)
        store.upsert_article(conn, item, "Essays")
        store.record_score(
            conn,
            item.id,
            {
                "quality": 50,
                "learning": 50,
                "fit": 50,
                "breadth": 50,
                "relevance": 50,
                "lesson": "l",
                "concept": "c",
                "kind": "reporting",
                "flags": [],
                "dupe_of": None,
                "confidence": "high",
            },
            50,
            50,
            cfg.tags.band_for(50),
            cfg.judge.prompt_version,
            model_name(cfg.models.judge),
        )
        ids.append(item.id)
    return ids


def _with_openai(llm, server):
    llm._providers["openai"] = OpenAIProvider(server.key, transport=server.transport())
    return llm


def test_models_compare_judges_the_sample_with_both_and_stores_only_the_costs(cfg, conn, monkeypatch):
    cfg.models.batch = False
    cfg.models.prices_per_mtok["openai/gpt-5-mini"] = [0.25, 2.0, 0.025]
    _judged_unread(cfg, conn)
    ctx = run.Context(cfg, load_secrets(cfg.root), conn)
    ctx._client = FakeReader()
    server = FakeOpenAIServer()
    ctx._llm = _with_openai(FakeLLM(cfg, conn), server)
    assert comparing.plan(ctx, "openai/gpt-5-mini", 4)["n"] == 4
    assert "costs about $" in comparing.plan(ctx, "openai/gpt-5-mini", 4)["preview"]
    before = {r[0] for r in conn.execute("SELECT article_id, score FROM scores")}
    seen = []
    result = comparing.compare(ctx, "openai/gpt-5-mini", 4, on_progress=lambda d, t: seen.append((d, t)))
    assert result["n"] == 4 and result["sample"] == 4 and seen[-1] == (8, 8)  # both passes counted
    assert result["first"]["model"] == cfg.models.judge and result["second"]["model"] == "openai/gpt-5-mini"
    # both fakes answer by title (gold 90, junk 10): the two agree on every band
    assert result["band_agreement"] == 1.0 and result["within_one_band"] == 1.0
    assert result["mean_abs_difference"] == 0.0 and result["disagreements"]
    assert set(result["per_1000"]) == {cfg.models.judge, "openai/gpt-5-mini"}
    assert all(v > 0 for v in result["per_1000"].values())
    # nothing stored changed: the scores are the seeded ones, the tags untouched; the costs are recorded
    assert {r[0] for r in conn.execute("SELECT article_id, score FROM scores")} == before
    assert conn.execute("SELECT COUNT(*) FROM scores WHERE score != 50").fetchone()[0] == 0
    purposes = {r[0] for r in conn.execute("SELECT DISTINCT purpose FROM costs")}
    assert purposes == {"compare"}
    assert {r[0] for r in conn.execute("SELECT DISTINCT model FROM costs")} == {
        model_name(cfg.models.judge),
        "openai/gpt-5-mini",
    }
    calls = [m for m, _p in ctx.llm.calls]  # the four articles fit one call per model
    assert calls == [cfg.models.judge, "openai/gpt-5-mini"]
    assert [p for m, p, _b in server.requests] == ["/v1/responses"]  # the second model went over HTTP
    text = comparing.render_comparison(result)
    assert "compared 4 of 4 articles" in text and "same band 100%" in text and "per 1,000 articles" in text
    assert "nothing stored changed" in text


def test_models_compare_needs_prices_and_a_sample(cfg, conn):
    cfg.models.batch = False
    ctx = run.Context(cfg, load_secrets(cfg.root), conn)
    ctx._client = FakeReader()
    ctx._llm = FakeLLM(cfg, conn)
    with pytest.raises(Exception, match="price"):
        comparing.plan(ctx, "openai/gpt-5-mini", 4)
    cfg.models.prices_per_mtok["openai/gpt-5-mini"] = [0.25, 2.0]
    assert comparing.plan(ctx, "openai/gpt-5-mini", 4)["n"] == 0
    with pytest.raises(Exception, match="nothing to compare"):
        comparing.compare(ctx, "openai/gpt-5-mini", 4)


def test_models_compare_from_the_command_line(tmp_path, monkeypatch):
    root = _project(
        tmp_path,
        monkeypatch,
        config="timezone: UTC\nmodels:\n  batch: false\n  prices_per_mtok:\n    claude-sonnet-5-5: [2, 10, 0.1]\n"
        "    openai/gpt-5-mini: [0.25, 2, 0.025]\n",
    )
    _env(root)
    cfg, _client, llm = _fakes(root, monkeypatch)
    conn = store.connect(cfg.db_path)
    _judged_unread(cfg, conn, 3)
    conn.close()
    _with_openai(llm, FakeOpenAIServer())
    result = invoke(["models", "compare", "--with", "openai/gpt-5-mini", "--sample", "3"], input="n\n")
    assert result.exit_code == 0 and "3 unread articles judged by claude-sonnet-5-5" in result.output
    assert "compared" not in result.output  # declined
    result = invoke(["models", "compare", "--with", "openai/gpt-5-mini", "--sample", "3", "--yes"])
    assert result.exit_code == 0, result.output
    assert "compared 3 of 3 articles: claude-sonnet-5-5 against openai/gpt-5-mini" in result.output
    assert "models.judge in config.yaml is the switch" in result.output


# -- rejudge ---------------------------------------------------------------------------


def _stand_in_judged(cfg, conn, n=4):
    """Unread articles judged by a quick judge (a different model), so the judge has stand-ins."""
    specs = [{"n": i, "score": 40 + i, "crawled_msec": 1_800_000_000_000 + i} for i in range(1, n + 1)]
    return seed_scored(conn, cfg, specs)  # model "test"


def test_run_rejudge_has_the_judge_read_what_another_model_judged(cfg, conn):
    cfg.models.batch = False
    ids = _stand_in_judged(cfg, conn)
    ctx = run.Context(cfg, load_secrets(cfg.root), conn)
    ctx._client = FakeReader([make_item(i, feed="Feed A", title=f"plain {i}") for i in range(1, 5)])
    ctx._llm = FakeLLM(cfg, conn)
    res = run.run(ctx, enforce=False, workers=1)
    assert res.scored == 0 and ctx.llm.calls == []  # judged already, by whoever: nothing to do
    res = run.run(ctx, enforce=False, workers=1, rejudge=True)
    assert res.scored == 4 and "re-judging" in res.summary().lower()
    models = {
        r[0] for r in conn.execute(f"SELECT model FROM scores WHERE article_id IN ({','.join('?' * 4)})", ids)
    }
    assert models == {model_name(cfg.models.judge)}
    res = run.run(ctx, enforce=False, workers=1, rejudge=True)
    assert res.scored == 0  # the judge's own judgments are never re-judged


def test_catch_up_rejudge_reads_every_stand_in_whatever_it_rated(cfg, conn):
    cfg.models.batch = False
    cfg.catch_up.rejudge_at = 70  # the standing rule would re-judge nothing rated 41..44
    ids = _stand_in_judged(cfg, conn)
    ctx = run.Context(cfg, load_secrets(cfg.root), conn)
    ctx._client = FakeReader()
    ctx._llm = FakeLLM(cfg, conn)
    total = run.catch_up(ctx, workers=1)
    assert total["scored"] == 0
    total = run.catch_up(ctx, workers=1, rejudge=True)
    assert total["scored"] == 4 and total["pending"] == 0
    models = {
        r[0] for r in conn.execute(f"SELECT model FROM scores WHERE article_id IN ({','.join('?' * 4)})", ids)
    }
    assert models == {model_name(cfg.models.judge)}
    # a quick judge never re-judges, --rejudge or not
    total = run.catch_up(ctx, workers=1, rejudge=True, model="claude-haiku-5-5")
    assert total["scored"] == 0


def test_rejudge_from_the_command_line(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch, config="timezone: UTC\nmodels:\n  batch: false\n")
    _env(root)
    items = [
        make_item(i, feed="Feed A", title=f"plain {i}", crawled_msec=1_800_000_000_000 + i) for i in (1, 2)
    ]
    cfg, _client, _llm = _fakes(root, monkeypatch, client=FakeReader(items))
    conn = store.connect(cfg.db_path)
    _stand_in_judged(cfg, conn, 2)
    conn.close()
    result = invoke(["catch-up", "--yes"])
    assert result.exit_code == 0 and "nothing pending" in result.output, result.output
    result = invoke(["catch-up", "--rejudge", "--yes", "--model", "claude-haiku-5-5"])
    assert result.exit_code == 1 and "--rejudge is the judge's pass" in result.output
    result = invoke(["catch-up", "--rejudge", "--yes"])  # the judge's pass, not the quick judge's
    assert result.exit_code == 0, result.output
    assert "2 articles pending" in result.output and "with claude-sonnet-5-5" in result.output
    assert "judged 2" in result.output and "pending 0" in result.output
    conn = store.connect(cfg.db_path)
    conn.execute("UPDATE scores SET model = 'test', score = 30")  # stand-ins again, under the standing rule
    conn.commit()
    conn.close()
    result = invoke(["run"])
    assert result.exit_code == 0 and "judged 0" in result.output, result.output
    result = invoke(["run", "--rejudge"])
    assert result.exit_code == 0, result.output
    assert "judged 2" in result.output and "re-judging what other models judged" in result.output


# -- the dashboard, the doctor, the progress note ---------------------------------------


def test_dashboard_by_model_needs_two_models_with_enough_judgments(cfg, conn, monkeypatch):
    assert dashboard.by_model(conn, min_n=2) == []
    ids = seed_scored(conn, cfg, [{"n": i, "score": 80} for i in range(1, 4)])  # model "test"
    for i in range(4, 7):  # three by the judge
        item = make_item(i, feed="Feed A", title=f"t {i}")
        store.upsert_article(conn, item, None)
        store.record_score(
            conn,
            item.id,
            {
                "quality": 40,
                "learning": 40,
                "fit": 40,
                "breadth": 40,
                "relevance": 40,
                "lesson": "l",
                "concept": "c",
                "kind": "reporting",
                "flags": [],
                "dupe_of": None,
                "confidence": "high",
            },
            40,
            40,
            cfg.tags.band_for(40),
            cfg.judge.prompt_version,
            "claude-sonnet-5-5",
        )
    store.add_feedback_many(conn, ids[:2], "read")
    store.add_feedback(conn, ids[0], "star")
    store.add_feedback(conn, ids[1], "great")
    rows = dashboard.by_model(conn, min_n=2)
    assert [r["model"] for r in rows] == ["claude-sonnet-5-5", "test"] or [r["model"] for r in rows] == [
        "test",
        "claude-sonnet-5-5",
    ]
    by = {r["model"]: r for r in rows}
    assert by["test"]["n"] == 3 and by["test"]["mean_score"] == 80.0
    assert by["test"]["read_share"] == pytest.approx(2 / 3) and by["test"]["star_share"] == pytest.approx(
        1 / 3
    )
    assert by["test"]["liked"] == 1 and by["test"]["disliked"] == 0
    assert by["claude-sonnet-5-5"]["n"] == 3 and by["claude-sonnet-5-5"]["read_share"] == 0
    assert dashboard.by_model(conn, min_n=4) == []  # neither reaches the floor
    assert "By the model that judged" not in dashboard.render(cfg, conn).read_text(encoding="utf-8")
    monkeypatch.setattr(dashboard, "BY_MODEL_MIN_N", 2)
    html = dashboard.render(cfg, conn).read_text(encoding="utf-8")
    assert "By the model that judged" in html and "claude-sonnet-5-5" in html and "great+good" in html


def test_doctor_names_the_key_each_role_needs(tmp_path, monkeypatch):
    root = _project(
        tmp_path,
        monkeypatch,
        config="timezone: UTC\nmodels:\n  judge: openai/gpt-5-mini\n  profiler: gemini/gemini-2.5-pro\n",
    )
    _env(root)
    result = invoke(["doctor"])
    assert result.exit_code == 1, result.output
    assert "openai key: MISSING (OPENAI_API_KEY in .env; used by the judge)" in result.output
    assert "gemini key: MISSING (GEMINI_API_KEY in .env; used by the profiler)" in result.output
    assert "anthropic key: set (reviewer, quick judge)" in result.output
    assert "unpriced models" in result.output and "openai/gpt-5-mini" in result.output


def test_progress_says_when_the_spend_is_an_estimate(cfg, conn):
    llm = FakeLLM(cfg, conn)
    llm.record(None, "openai-compatible/llama3.3", "teacher", Usage(input_tokens=100, output_tokens=10))
    assert store.estimated_usage_models(conn) == []
    llm.record(
        None,
        "openai-compatible/llama3.3",
        "teacher",
        Usage(input_tokens=100, output_tokens=10, estimated=True),
    )
    llm.record(
        None,
        "openai-compatible/llama3.3",
        "teacher",
        Usage(input_tokens=100, output_tokens=10, estimated=True),
    )
    assert store.estimated_usage_models(conn) == ["openai-compatible/llama3.3"]
    snap = progress.snapshot(conn, cfg)
    assert snap["estimated_models"] == ["openai-compatible/llama3.3"]
    text = progress.render_text(snap)
    assert "spend is estimated for openai-compatible/llama3.3" in text
    store.kv_set(conn, store.ESTIMATED_USAGE_KEY, "not json")
    assert store.estimated_usage_models(conn) == []
