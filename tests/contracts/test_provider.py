"""The model provider contract: what every provider must do for the judge, through the real
`llm.LLM` over a fake of each service (`tests/fakes.py`). Parametrised over the five
providers: Anthropic (the SDK, faked), OpenAI (the Responses and Batch APIs), Gemini
(generateContent and its batch operation), OpenRouter and an OpenAI-compatible local service
(Chat Completions, no batch). A new provider is accepted only with this suite green."""

from __future__ import annotations

import pytest
from fakes import (
    FakeAnthropic,
    FakeChatServer,
    FakeGeminiServer,
    FakeOpenAIServer,
    FakeUsage,
    ScriptedJudge,
    api_error,
)

from bodh import store
from bodh.config import Secrets
from bodh.errors import BatchNotFound, BudgetExceeded, ModelConfigError, ModelError, NoJudgments
from bodh.llm import LLM
from bodh.providers.anthropic import AnthropicProvider
from bodh.providers.base import ModelRef, Usage
from bodh.providers.gemini import GeminiProvider
from bodh.providers.openai import OpenAICompatibleProvider, OpenAIProvider

SYSTEM = "You are the teacher."
ARTICLES = (
    "[1]\nfeed: f · folder: none\ntitle: gold essay\nauthor: unknown · published: ? · site: x\ntext: t\n\n"
    "[2]\nfeed: f · folder: none\ntitle: junk listicle\nauthor: unknown · published: ? · site: x\ntext: t\n"
)
KEY = "sk-test-key-0123456789"
MODELS = {
    "anthropic": "anthropic/claude-sonnet-5-5",
    "openai": "openai/gpt-5",
    "gemini": "gemini/gemini-2.5-pro",
    "openrouter": "openrouter/qwen/qwen3-235b",
    "openai-compatible": "openai-compatible/llama3.3",
}
SECRETS = Secrets(
    anthropic_api_key=KEY,
    openai_api_key=KEY,
    gemini_api_key=KEY,
    openrouter_api_key=KEY,
    openai_compatible_base_url="http://ollama.local:11434/v1",
)


class World:
    """One provider over its fake, with the knobs every test turns the same way."""

    def __init__(self, name: str, cfg, conn) -> None:
        self.name = name
        self.model = MODELS[name]
        self.ref = ModelRef.parse(self.model)
        cfg.models.prices_per_mtok[self.ref.name] = [2.0, 10.0, 0.2]
        self.llm = LLM(cfg, SECRETS, conn)
        self.scripted = ScriptedJudge()
        if name == "anthropic":
            self.api = FakeAnthropic()
            self.server = None
            self.llm._providers[name] = AnthropicProvider(KEY, self.llm._flags(), client=self.api)
        elif name == "openai":
            self.server = FakeOpenAIServer(self.scripted, key=KEY)
            self.llm._providers[name] = OpenAIProvider(KEY, transport=self.server.transport())
        elif name == "gemini":
            self.server = FakeGeminiServer(self.scripted, key=KEY)
            self.llm._providers[name] = GeminiProvider(KEY, transport=self.server.transport())
        elif name == "openrouter":
            self.server = FakeChatServer(self.scripted, key=KEY)
            self.llm._providers[name] = OpenAICompatibleProvider(
                KEY, "https://openrouter.ai/api/v1", transport=self.server.transport(), name="openrouter"
            )
        else:
            self.server = FakeChatServer(self.scripted, key="")
            self.llm._providers[name] = OpenAICompatibleProvider(
                "",
                "http://ollama.local:11434/v1",
                transport=self.server.transport(),
                name="openai-compatible",
            )
        self.supports_batch = self.llm.capabilities(self.model).batch

    # knobs
    def prose(self) -> None:
        if self.server is None:
            self.api.prose = True
        else:
            self.scripted.prose = True

    def finish_batches(self) -> None:
        (self.api if self.server is None else self.server).finish_all()

    def fail_result(self, custom_id: str) -> None:
        (self.api if self.server is None else self.server).fail_ids.add(custom_id)

    def expire_result(self, custom_id: str) -> None:
        (self.api if self.server is None else self.server).expire_ids.add(custom_id)

    def vanish_batch(self, batch_id: str) -> None:
        (self.api if self.server is None else self.server).missing_batches.add(batch_id)

    def break_results(self, batch_id: str) -> None:
        (self.api if self.server is None else self.server).broken_results.add(batch_id)

    def wrong_key(self) -> None:
        if self.server is None:
            self.api.fail_with = api_error(
                __import__("anthropic").AuthenticationError, f"invalid x-api-key ({KEY})", 401
            )
        else:
            self.server.wrong_key = True

    def unknown_model(self) -> None:
        if self.server is None:
            self.api.fail_with = api_error(
                __import__("anthropic").NotFoundError, f"model: {self.ref.model} not found", 404
            )
        else:
            self.server.unknown_model = True

    def calls(self) -> int:
        if self.server is None:
            return len(self.api.calls)
        return sum(
            1
            for m, p, _ in self.server.requests
            if m == "POST" and ("responses" in p or "completions" in p or "generateContent" in p)
        )

    def last_model_sent(self) -> str:
        if self.server is None:
            return str(self.api.calls[-1]["model"])
        for _m, path, body in reversed(self.server.requests):
            if isinstance(body, dict) and body.get("model"):
                return str(body["model"])
            if "generateContent" in path:
                return path.split("/models/")[1].split(":")[0]
        return ""


@pytest.fixture(params=list(MODELS))
def world(request, cfg, conn, monkeypatch) -> World:
    monkeypatch.setattr("bodh.providers.http.time.sleep", lambda _s: None)  # no backoff waits in the suite
    return World(request.param, cfg, conn)


def test_sync_judge_returns_verdicts_and_records_the_cost(world, conn, cfg):
    verdicts = world.llm.judge(world.model, SYSTEM, ARTICLES, None, "teacher")
    assert [v.n for v in verdicts] == [1, 2] and verdicts[0].quality == 90 and verdicts[1].quality == 10
    row = conn.execute("SELECT model, purpose, usd FROM costs").fetchone()
    assert row["model"] == world.ref.name and row["purpose"] == "teacher" and row["usd"] > 0
    assert world.llm.spent_today() > 0 and world.llm.budget_left() < cfg.models.daily_budget_usd
    assert world.last_model_sent() == world.ref.model  # the id reaches the service unchanged


def test_a_model_id_never_seen_before_reaches_the_provider_unchanged(world, cfg):
    model = f"{world.ref.provider}/{world.ref.model}-preview-2027"
    cfg.models.prices_per_mtok[ModelRef.parse(model).name] = [1.0, 1.0]
    world.llm.judge(model, SYSTEM, ARTICLES, None, "teacher")
    assert world.last_model_sent() == f"{world.ref.model}-preview-2027"


def test_complete_returns_text(world):
    text = world.llm.complete(world.model, SYSTEM, "rewrite the profile", None, "learner_rewrite")
    assert text.startswith("## What the student has been learning")


def test_no_judgment_in_the_answer_is_an_error(world):
    world.prose()
    with pytest.raises(NoJudgments):
        world.llm.judge(world.model, SYSTEM, ARTICLES, None, "teacher")


def test_structured_output_is_asked_for_where_the_service_takes_a_schema(world):
    params = world.llm.request_params(world.model, SYSTEM, ARTICLES, ttl="1h")
    caps = world.llm.capabilities(world.model)
    assert caps.structured_output
    if world.name == "anthropic":
        assert params["output_config"]["format"]["type"] == "json_schema"
    elif world.name == "openai":
        assert params["text"]["format"]["type"] == "json_schema" and params["instructions"] == SYSTEM
    elif world.name == "gemini":
        schema = params["generationConfig"]["responseSchema"]
        assert params["generationConfig"]["responseMimeType"] == "application/json"
        assert (
            "$ref" not in str(schema)
            and "$defs" not in schema
            and schema["properties"]["judgments"]["items"]["properties"]["n"]["type"] == "integer"
        )
    else:
        assert (
            params["response_format"]["type"] == "json_schema" and params["messages"][0]["role"] == "system"
        )
    assert "JSON object" not in str(params)  # the plain-JSON instruction only travels without a schema


def test_a_schema_rejected_once_becomes_plain_json_from_then_on(world):
    if world.name == "anthropic":
        world.api.reject_structured_once = True
    elif world.name in ("openrouter", "openai-compatible"):
        world.server.rejects_schema = True
    else:
        pytest.skip(f"{world.name} always takes the schema")
    verdicts = world.llm.judge(world.model, SYSTEM, ARTICLES, None, "teacher")
    assert len(verdicts) == 2 and world.calls() == 2  # rejected once, answered on the retry
    assert not world.llm.capabilities(world.model).structured_output
    params = world.llm.request_params(world.model, SYSTEM, ARTICLES)
    assert "JSON object" in str(params)  # the instruction now travels in the prompt
    if world.name == "anthropic":  # remembered across runs, in the database
        assert world.ref.model in (store.kv_get(world.llm.conn, store.PLAIN_JSON_KEY) or "")


def test_batch_lifecycle(world, cfg):
    if not world.supports_batch:
        with pytest.raises(ModelError, match="no batch API"):
            world.llm.submit_batch(world.model, [{"custom_id": "t1-0", "params": {}}])
        return
    requests = [
        {"custom_id": "t1-0", "params": world.llm.request_params(world.model, SYSTEM, ARTICLES, ttl="1h")},
        {"custom_id": "s1-0", "params": world.llm.request_params(world.model, SYSTEM, ARTICLES)},
    ]
    bid = world.llm.submit_batch(world.model, requests)
    assert bid and world.llm.batch_status(world.model, bid) == "in_progress"
    assert world.llm.batch_results(world.model, bid) == [] or world.name != "openai"  # nothing before the end
    world.finish_batches()
    assert world.llm.batch_status(world.model, bid) == "ended"
    results = world.llm.batch_results(world.model, bid)
    assert [r.custom_id for r in results] == ["t1-0", "s1-0"]
    assert all(r.status == "succeeded" and r.answer is not None for r in results)
    counts, usd = world.llm.cost(world.model, results[0].answer.usage, cfg.models.batch_discount)
    assert usd >= 0 and '"judgments"' in results[0].answer.text


def test_batch_results_of_every_kind(world):
    if not world.supports_batch:
        pytest.skip("no batch API")
    requests = [
        {"custom_id": f"t1-{n}", "params": world.llm.request_params(world.model, SYSTEM, ARTICLES)}
        for n in range(3)
    ]
    bid = world.llm.submit_batch(world.model, requests)
    world.fail_result("t1-1")
    world.expire_result("t1-2")
    world.finish_batches()
    results = {r.custom_id: r for r in world.llm.batch_results(world.model, bid)}
    assert results["t1-0"].status == "succeeded"
    assert results["t1-1"].status == "errored" and "overloaded" in results["t1-1"].error
    # an expired request is reported expired, or not at all (OpenAI and Gemini leave it out):
    # either way the article is pending again
    assert "t1-2" not in results or results["t1-2"].status == "expired"


def test_vanished_batch_and_broken_results(world):
    if not world.supports_batch:
        with pytest.raises(BatchNotFound):
            world.llm.batch_status(world.model, "batch_never")
        return
    bid = world.llm.submit_batch(
        world.model,
        [{"custom_id": "t1-0", "params": world.llm.request_params(world.model, SYSTEM, ARTICLES)}],
    )
    world.finish_batches()
    world.break_results(bid)
    with pytest.raises(Exception) as exc:  # transient: the caller polls again next run, not a lost batch
        world.llm.batch_results(world.model, bid)
    assert not isinstance(exc.value, (BatchNotFound, ModelConfigError))
    world.vanish_batch(bid)
    with pytest.raises(BatchNotFound):
        world.llm.batch_status(world.model, bid)
    with pytest.raises(BatchNotFound):
        world.llm.batch_results(world.model, bid)
    with pytest.raises(BatchNotFound):
        world.llm.batch_status(world.model, "batch_never")


def test_a_rejected_key_or_model_is_a_configuration_error_without_the_key(world):
    world.wrong_key()
    with pytest.raises(ModelConfigError) as exc:
        world.llm.judge(world.model, SYSTEM, ARTICLES, None, "teacher")
    assert exc.value.exit_code == 4 and KEY not in str(exc.value)
    if world.name == "anthropic":
        world.api.fail_with = None
    else:
        world.server.wrong_key = False
    world.unknown_model()
    with pytest.raises(ModelConfigError) as exc:
        world.llm.judge(world.model, SYSTEM, ARTICLES, None, "teacher")
    assert world.ref.model in str(exc.value) and KEY not in str(exc.value)


def test_a_passing_failure_is_retried_and_a_lasting_one_is_this_calls_error(world):
    if world.name == "anthropic":
        pytest.skip(
            "the Anthropic SDK retries 429 and 5xx on its own (max_retries=2), not modelled by the fake"
        )
    world.server.fail_next = [(429, "slow down"), (503, "unavailable")]
    assert len(world.llm.judge(world.model, SYSTEM, ARTICLES, None, "teacher")) == 2
    assert world.calls() == 3
    world.server.fail_next = [(500, "down")] * 10
    with pytest.raises(ModelError) as exc:
        world.llm.judge(world.model, SYSTEM, ARTICLES, None, "teacher")
    assert not isinstance(exc.value, ModelConfigError) and "after 3 attempts" in str(exc.value)
    world.llm.retries(0)
    world.server.fail_next = [(500, "down")]
    with pytest.raises(ModelError, match="after 1 attempts"):
        world.llm.judge(world.model, SYSTEM, ARTICLES, None, "teacher")


def test_budget_stops_the_calls(world, cfg):
    cfg.models.daily_budget_usd = 0.00001
    world.llm.judge(world.model, SYSTEM, ARTICLES, None, "teacher")  # the first call lands
    with pytest.raises(BudgetExceeded):
        world.llm.judge(world.model, SYSTEM, ARTICLES, None, "teacher")
    with pytest.raises(BudgetExceeded):
        world.llm.complete(world.model, SYSTEM, "x", None, "learner_rewrite")
    assert world.calls() == 1


def test_usage_reported_or_estimated(world, cfg):
    answer_usage = world.llm.provider(world.model).judge(world.ref.model, SYSTEM, ARTICLES, None, None).usage
    if world.name == "anthropic":
        assert answer_usage.input_tokens == 1000 and not answer_usage.estimated
    else:
        assert answer_usage.estimated and answer_usage.input_tokens > 0  # the fake reports nothing
        world.server.usage = {"input": 1000, "output": 100, "cached": 300}
        reported = world.llm.provider(world.model).judge(world.ref.model, SYSTEM, ARTICLES, None, None).usage
        assert (reported.input_tokens, reported.output_tokens, reported.cache_read, reported.estimated) == (
            1000,
            100,
            300,
            False,
        )
    counts, usd = world.llm.cost(world.model, Usage(input_tokens=1_000_000, cache_read=1_000_000))
    assert counts["cache_read"] == 1_000_000 and usd == pytest.approx(2.0 + 0.2)
    assert world.llm.cost(world.model, None) == (
        {"input_tokens": 0, "output_tokens": 0, "cache_write": 0, "cache_read": 0},
        0.0,
    )
    assert world.llm.cost(world.model, FakeUsage(input_tokens=1_000_000), discount=0.5)[1] == pytest.approx(
        1.0
    )
