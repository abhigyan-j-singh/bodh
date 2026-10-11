"""The model client: one place that talks to the Anthropic API. Requests, batches, retries,
cost accounting against the daily budget, and the two fallbacks a model may force (plain
JSON instead of structured outputs, the 5-minute cache instead of the 1-hour one)."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from typing import Any

from . import store
from .config import Config, Secrets
from .errors import ModelError
from .judgment import PLAIN_JSON_INSTRUCTION, JudgmentBatchOut, Verdict, parse_judgments

log = logging.getLogger(__name__)


JudgeError = ModelError  # the name before October 2026


class BudgetExceeded(JudgeError):
    pass


class ModelConfigError(JudgeError):
    """The API rejected the key, the permission or the model id. Retrying is pointless;
    the run stops at once so the problem is visible instead of 27 identical tracebacks."""


class BatchNotFound(JudgeError):
    """The API no longer knows the batch (deleted in the console, or submitted under another
    key): it is closed as lost and its articles are pending again, nothing else stops."""


class NoJudgments(JudgeError):
    """The model answered, but with nothing a judgment could be read from."""


def _batch_classify(exc: Exception) -> Exception:
    import anthropic

    if isinstance(exc, anthropic.NotFoundError):
        return BatchNotFound(f"batch not found: {exc}")
    return _classify(exc)


def _classify(exc: Exception) -> Exception:
    import anthropic

    if isinstance(
        exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError)
    ):
        return ModelConfigError(
            f"model call rejected: {exc}. Check ANTHROPIC_API_KEY and the model ids in config.yaml"
        )
    return exc


_STRUCTURED_WORDS = re.compile(r"\b(output_config|output_format|structured|json_schema)\b", re.I)
_TTL_WORDS = re.compile(r"\b(ttl|cache_control)\b", re.I)


def _mentions_structured_outputs(text: str) -> bool:
    return bool(_STRUCTURED_WORDS.search(text or ""))


def _flagged_models(stored: str | None, legacy: str) -> set[str]:
    """The models a provider flag names: a JSON list, or "*" for every model when the value is
    the single flag the code wrote before October 2026."""
    if not stored:
        return set()
    if stored == legacy:
        return {"*"}
    try:
        models = json.loads(stored)
    except ValueError:
        return {"*"}
    return {str(m) for m in models} if isinstance(models, list) else {"*"}


def _mentions_cache_ttl(text: str) -> bool:
    # whole words only: "throttled" contains "ttl" and used to disable the 1-hour cache for good
    return bool(_TTL_WORDS.search(text or ""))


def _output_format() -> dict[str, Any]:
    """The structured-outputs format for a judgment batch, built the way the SDK's
    ``messages.parse`` builds it (bounds moved into descriptions, no extra properties)."""
    schema = JudgmentBatchOut.model_json_schema()
    try:
        from anthropic.lib._parse._transform import transform_schema

        schema = transform_schema(schema)
    except Exception:  # an SDK without that private helper: the plain schema is sent, and still accepted
        log.debug("llm: anthropic.lib._parse._transform is not available; sending the plain schema")
    return {"type": "json_schema", "schema": schema}


class LLM:
    """Anthropic client with cost accounting and a daily budget. Nothing else talks to the API."""

    def __init__(self, cfg: Config, secrets: Secrets, conn: sqlite3.Connection) -> None:
        if not secrets.is_set("anthropic_api_key"):
            raise JudgeError("ANTHROPIC_API_KEY not configured")
        import anthropic

        self.cfg = cfg
        # worker threads account for their calls here, under the lock, on a connection of their
        # own: sqlite3 shares prepared statements per connection, so two threads running the
        # same query on one connection can hand each other empty results
        self._lock = threading.Lock()
        self.conn = self._private_connection(cfg, conn)
        self._client = anthropic.Anthropic(
            api_key=secrets.value("anthropic_api_key"), max_retries=2, timeout=180.0
        )
        self._format = _output_format()
        # once a model has rejected structured outputs, every later call to it asks for plain JSON
        # instead; once it has rejected the 1-hour cache, it gets the default 5-minute cache. Both are
        # remembered per model in the database ("1"/"5m" alone, from before October 2026: every model)
        with self._lock:
            self._plain_json_models = _flagged_models(store.kv_get(self.conn, store.PLAIN_JSON_KEY), "1")
            self._short_ttl_models = _flagged_models(store.kv_get(self.conn, store.CACHE_TTL_KEY), "5m")

    def plain_json(self, model: str) -> bool:
        """Does this model get the plain-JSON instruction instead of structured outputs?"""
        return "*" in self._plain_json_models or model in self._plain_json_models

    def long_ttl_ok(self, model: str) -> bool:
        """May this model's prompt prefix be cached for an hour?"""
        return "*" not in self._short_ttl_models and model not in self._short_ttl_models

    @staticmethod
    def _private_connection(cfg: Config, shared: sqlite3.Connection) -> sqlite3.Connection:
        try:
            if cfg.db_path.exists():
                return store.connect(cfg.db_path)
        except sqlite3.Error:
            pass
        return shared

    # -- budget ---------------------------------------------------------------

    def spent_today(self) -> float:
        with self._lock:
            return store.spent_since(self.conn, store.local_midnight_iso(self.cfg.timezone))

    def budget_left(self) -> float:
        return self.cfg.models.daily_budget_usd - self.spent_today()

    def ensure_budget(self) -> None:
        if self.budget_left() <= 0:
            raise BudgetExceeded(
                f"daily model budget of ${self.cfg.models.daily_budget_usd:.2f} reached; scoring pauses until midnight"
            )

    # -- accounting -----------------------------------------------------------

    def cost(self, model: str, usage: Any, discount: float = 1.0) -> tuple[dict[str, int], float]:
        """(token counts, USD) for one response's usage, the batch discount applied if any."""
        p_in, p_out = self.cfg.models.price(model)
        creation = getattr(usage, "cache_creation", None)
        write_1h = int(getattr(creation, "ephemeral_1h_input_tokens", 0) or 0)
        write_5m = int(getattr(creation, "ephemeral_5m_input_tokens", 0) or 0)
        write_total = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        if write_1h + write_5m == 0 and write_total:
            write_5m = write_total if self.cfg.models.cache_ttl == "5m" else 0
            write_1h = write_total - write_5m
        u = {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "cache_write": write_1h + write_5m,
            "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        }
        p_read = self.cfg.models.cache_read_price(model)
        usd = (
            u["input_tokens"] * p_in
            + u["output_tokens"] * p_out
            + write_5m * p_in * 1.25
            + write_1h * p_in * 2.0
            + u["cache_read"] * p_read
        ) / 1_000_000
        return u, usd * discount

    def record(
        self, run_id: int | None, model: str, purpose: str, usage: Any, discount: float = 1.0
    ) -> float:
        u, usd = self.cost(model, usage, discount)
        with self._lock:
            store.record_cost(self.conn, run_id, model, purpose, u, usd)
        return usd

    def _enable_plain_json(self, model: str) -> None:
        if not self.plain_json(model):
            log.warning("judge: %s rejected structured outputs; asking it for plain JSON from now on", model)
            self._plain_json_models.add(model)
            with self._lock:
                store.kv_set(self.conn, store.PLAIN_JSON_KEY, json.dumps(sorted(self._plain_json_models)))

    def _disable_long_ttl(self, model: str) -> None:
        if self.long_ttl_ok(model):
            log.warning(
                "judge: %s rejected the 1-hour cache; using the 5-minute cache for it from now on", model
            )
            self._short_ttl_models.add(model)
            with self._lock:
                store.kv_set(self.conn, store.CACHE_TTL_KEY, json.dumps(sorted(self._short_ttl_models)))

    def adapt_to_error(self, model: str, message: str) -> bool:
        """A rejected request shape is remembered for that model, so the next request to it is
        different. Returns True when something changed (so a retry makes sense)."""
        if not self.plain_json(model) and _mentions_structured_outputs(message):
            self._enable_plain_json(model)
            return True
        if self.long_ttl_ok(model) and _mentions_cache_ttl(message):
            self._disable_long_ttl(model)
            return True
        return False

    # -- request shapes -------------------------------------------------------

    def _system(self, model: str, system_text: str, ttl: str | None = None) -> list[dict[str, Any]]:
        cache: dict[str, Any] = {"type": "ephemeral"}
        if ttl == "1h" and self.long_ttl_ok(model):
            cache["ttl"] = "1h"
        return [{"type": "text", "text": system_text, "cache_control": cache}]

    def request_params(
        self, model: str, system_text: str, user_text: str, ttl: str | None = None
    ) -> dict[str, Any]:
        """One judgment request, the same shape for a direct call and for a batch."""
        if self.plain_json(model):
            return {
                "model": model,
                "max_tokens": 8000,
                "system": self._system(model, system_text, ttl),
                "messages": [{"role": "user", "content": user_text + PLAIN_JSON_INSTRUCTION}],
            }
        return {
            "model": model,
            "max_tokens": 8000,
            "system": self._system(model, system_text, ttl),
            "messages": [{"role": "user", "content": user_text}],
            "output_config": {"format": self._format},
        }

    @staticmethod
    def message_text(message: Any) -> str:
        return "".join(getattr(b, "text", "") for b in getattr(message, "content", []) or [])

    # -- synchronous ----------------------------------------------------------

    def judge(
        self, model: str, system_text: str, user_text: str, run_id: int | None, purpose: str
    ) -> list[Verdict]:
        """One call, waited for. Returns validated verdicts; invalid ones are dropped."""
        import anthropic

        self.ensure_budget()
        try:
            response = self._client.messages.create(**self.request_params(model, system_text, user_text))
        except anthropic.BadRequestError as exc:
            if not self.adapt_to_error(model, str(exc)):
                raise _classify(exc) from exc
            try:
                response = self._client.messages.create(**self.request_params(model, system_text, user_text))
            except Exception as exc2:
                raise _classify(exc2) from exc2
        except Exception as exc:
            raise _classify(exc) from exc
        self.record(run_id, model, purpose, response.usage)
        verdicts = parse_judgments(self.message_text(response))
        if verdicts is None:
            raise NoJudgments(f"{model} returned no parsable judgments")
        return verdicts

    def complete(
        self,
        model: str,
        system_text: str,
        user_text: str,
        run_id: int | None,
        purpose: str,
        max_tokens: int = 2000,
    ) -> str:
        """Plain text completion (the weekly profile rewrite)."""
        self.ensure_budget()
        try:
            response = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_text,
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception as exc:
            raise _classify(exc) from exc
        self.record(run_id, model, purpose, response.usage)
        return self.message_text(response).strip()

    # -- batches --------------------------------------------------------------

    def submit_batch(self, requests: list[dict[str, Any]]) -> str:
        """requests: [{"custom_id": ..., "params": ...}]. Returns the batch id."""
        try:
            # no automatic retry here: a create that timed out after the server accepted it would
            # be retried into a second, paid batch nobody collects. A failed create is raised, its
            # articles stay pending, and the next run submits them again.
            no_retry = self._client.with_options(max_retries=0)
            batch = no_retry.messages.batches.create(requests=requests)  # type: ignore[arg-type]  # the SDK's TypedDict; the dicts have its shape
        except Exception as exc:
            raise _classify(exc) from exc
        return batch.id

    def batch_status(self, batch_id: str) -> str:
        """'in_progress', 'canceling' or 'ended'."""
        try:
            return self._client.messages.batches.retrieve(batch_id).processing_status
        except Exception as exc:
            raise _batch_classify(exc) from exc

    def retries(self, n: int) -> None:
        """More patience with 429s and 5xx, for a deliberate catch-up through a backlog."""
        self._client = self._client.with_options(max_retries=max(0, n))

    def batch_results(self, batch_id: str) -> list[Any]:
        try:
            return list(self._client.messages.batches.results(batch_id))
        except Exception as exc:
            raise _batch_classify(exc) from exc
