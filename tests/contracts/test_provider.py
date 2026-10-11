"""The model provider contract: what every provider must do for the judge. Parametrised over
the implementations (today the Anthropic client over the fake SDK; Phase 5 adds the rest):
a synchronous judgment, a plain completion, the batch lifecycle, structured output or its
fallback, the long cache or its fallback, cost reporting, the budget, classified errors that
carry no secret. A new provider is accepted only with this suite green."""

from __future__ import annotations

import anthropic
import pytest
from fakes import FakeLLM, FakeUsage, api_error

from bodh import store
from bodh.errors import ModelError
from bodh.llm import BatchNotFound, BudgetExceeded, ModelConfigError, NoJudgments

SYSTEM = "You are the teacher."
ARTICLES = (
    "[1]\nfeed: f · folder: none\ntitle: gold essay\nauthor: unknown · published: ? · site: x\ntext: t\n\n"
    "[2]\nfeed: f · folder: none\ntitle: junk listicle\nauthor: unknown · published: ? · site: x\ntext: t\n"
)
KEY = "sk-ant-test-key"


@pytest.fixture(params=["anthropic"])
def provider(request, cfg, conn) -> FakeLLM:
    if request.param == "anthropic":
        return FakeLLM(cfg, conn)
    raise NotImplementedError(request.param)


def test_sync_judge_returns_verdicts_and_records_the_cost(provider, conn, cfg):
    verdicts = provider.judge(cfg.models.judge, SYSTEM, ARTICLES, None, "teacher")
    assert [v.n for v in verdicts] == [1, 2] and verdicts[0].quality == 90 and verdicts[1].quality == 10
    assert verdicts[0].kind == "reporting" and verdicts[0].confidence == "high"
    assert conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 1
    assert provider.spent_today() > 0 and provider.budget_left() < cfg.models.daily_budget_usd
    assert provider.calls == [(cfg.models.judge, "teacher")]


def test_complete_returns_text(provider, cfg):
    text = provider.complete(cfg.models.profiler, SYSTEM, "rewrite the profile", None, "learner_rewrite")
    assert text.startswith("## What the student has been learning")


def test_no_judgment_in_the_answer_is_an_error(provider, cfg):
    provider.api.prose = True
    with pytest.raises(NoJudgments):
        provider.judge(cfg.models.judge, SYSTEM, ARTICLES, None, "teacher")


def test_structured_output_or_its_fallback(provider, cfg, conn):
    model = cfg.models.judge
    assert "output_config" in provider.request_params(model, SYSTEM, ARTICLES)
    provider.api.reject_structured_once = True
    verdicts = provider.judge(model, SYSTEM, ARTICLES, None, "teacher")
    assert len(verdicts) == 2 and len(provider.api.calls) == 2  # rejected once, answered on the retry
    assert "output_config" not in provider.api.calls[-1] and provider.plain_json(model)
    assert model in (store.kv_get(conn, store.PLAIN_JSON_KEY) or "")  # remembered for the next process
    assert not provider.plain_json("another-model")  # per model


def test_long_cache_or_its_fallback(provider, cfg, conn):
    model = cfg.models.judge
    params = provider.request_params(model, SYSTEM, ARTICLES, ttl="1h")
    assert params["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert provider.adapt_to_error(model, "cache_control: ttl of 1h is not supported")
    assert provider.request_params(model, SYSTEM, ARTICLES, ttl="1h")["system"][0]["cache_control"] == {
        "type": "ephemeral"
    }
    assert model in (store.kv_get(conn, store.CACHE_TTL_KEY) or "") and provider.long_ttl_ok("another-model")
    assert not provider.adapt_to_error(model, "something unrelated")


def test_batch_lifecycle(provider, cfg):
    model = cfg.models.judge
    requests = [
        {"custom_id": "t1-0", "params": provider.request_params(model, SYSTEM, ARTICLES, ttl="1h")},
        {"custom_id": "s1-0", "params": provider.request_params(model, SYSTEM, ARTICLES)},
    ]
    bid = provider.submit_batch(requests)
    assert bid and provider.api.options[-1] == {"max_retries": 0}  # no automatic retry on the create
    assert provider.batch_status(bid) == "in_progress"
    provider.api.status_sequence[bid] = ["in_progress", "canceling", "ended"]
    assert [provider.batch_status(bid) for _ in range(4)] == ["in_progress", "canceling", "ended", "ended"]
    results = provider.batch_results(bid)
    assert [r.custom_id for r in results] == ["t1-0", "s1-0"]
    assert all(r.result.type == "succeeded" for r in results)
    usage, usd = provider.cost(model, results[0].result.message.usage, cfg.models.batch_discount)
    assert usage["input_tokens"] == 1000 and 0 < usd < 0.01
    text = provider.message_text(results[0].result.message)
    assert '"judgments"' in text


def test_batch_results_of_every_kind(provider, cfg):
    model = cfg.models.judge
    requests = [
        {"custom_id": f"t1-{n}", "params": provider.request_params(model, SYSTEM, ARTICLES)} for n in range(4)
    ]
    bid = provider.submit_batch(requests)
    provider.api.fail_ids.add("t1-1")
    provider.api.expire_ids.add("t1-2")
    provider.api.cancel_ids.add("t1-3")
    provider.api.finish_all()
    kinds = {r.custom_id: r.result.type for r in provider.batch_results(bid)}
    assert kinds == {"t1-0": "succeeded", "t1-1": "errored", "t1-2": "expired", "t1-3": "canceled"}
    errored = next(r for r in provider.batch_results(bid) if r.custom_id == "t1-1")
    assert errored.result.error.error.message == "overloaded"


def test_vanished_batch_and_broken_results(provider, cfg):
    bid = provider.submit_batch(
        [{"custom_id": "t1-0", "params": provider.request_params(cfg.models.judge, SYSTEM, ARTICLES)}]
    )
    provider.api.broken_results.add(bid)
    with pytest.raises(anthropic.APIConnectionError):  # transient: the caller polls again next run
        provider.batch_results(bid)
    provider.api.missing_batches.add(bid)
    with pytest.raises(BatchNotFound):
        provider.batch_status(bid)
    with pytest.raises(BatchNotFound):
        provider.batch_results(bid)
    with pytest.raises(BatchNotFound):
        provider.batch_status("msgbatch_never")


def test_errors_are_classified_and_carry_no_secret(provider, cfg):
    model = cfg.models.judge
    for cls, status in (
        (anthropic.AuthenticationError, 401),
        (anthropic.PermissionDeniedError, 403),
        (anthropic.NotFoundError, 404),
    ):
        provider.api.fail_calls[len(provider.api.calls) + 1] = api_error(cls, f"rejected ({KEY})", status)
        with pytest.raises(ModelConfigError) as exc:
            provider.judge(model, SYSTEM, ARTICLES, None, "teacher")
        assert isinstance(exc.value, ModelError) and exc.value.exit_code == 4
        assert KEY not in str(exc.value) and "config.yaml" in str(exc.value)
    provider.api.fail_calls[len(provider.api.calls) + 1] = api_error(
        anthropic.RateLimitError, "slow down", 429
    )
    with pytest.raises(
        anthropic.RateLimitError
    ):  # anything else is passed through for the caller to retry or report
        provider.judge(model, SYSTEM, ARTICLES, None, "teacher")


def test_budget_stops_the_calls(provider, cfg):
    cfg.models.daily_budget_usd = 0.0001
    provider.judge(cfg.models.judge, SYSTEM, ARTICLES, None, "teacher")  # the first call lands
    with pytest.raises(BudgetExceeded):
        provider.judge(cfg.models.judge, SYSTEM, ARTICLES, None, "teacher")
    with pytest.raises(BudgetExceeded):
        provider.complete(cfg.models.profiler, SYSTEM, "x", None, "learner_rewrite")
    assert len(provider.api.calls) == 1


def test_cost_reporting_shapes(provider, cfg):
    model = cfg.models.judge
    assert provider.cost(model, None) == (
        {"input_tokens": 0, "output_tokens": 0, "cache_write": 0, "cache_read": 0},
        0.0,
    )
    usage, usd = provider.cost(
        model, FakeUsage(input_tokens=1_000_000, cache_creation_input_tokens=1_000_000)
    )
    assert usage["cache_write"] == 1_000_000 and usd > 0
    assert provider.cost(model, FakeUsage(input_tokens=1_000_000), discount=0.5)[1] == pytest.approx(
        provider.cost(model, FakeUsage(input_tokens=1_000_000))[1] / 2
    )
