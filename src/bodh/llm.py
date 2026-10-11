"""The model client the rest of the code talks to: one object that picks the provider a
model string names, keeps the daily budget across providers, accounts every call's cost in
the database, and turns a provider's answer into verdicts. Nothing else talks to a provider.
The providers themselves live in `providers/`."""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Any

from . import store
from .config import Config, Secrets
from .errors import BatchNotFound, BudgetExceeded, ModelConfigError, ModelError, NoJudgments
from .judgment import PLAIN_JSON_INSTRUCTION, JudgmentBatchOut, Verdict, parse_judgments
from .providers import make_provider
from .providers.base import (
    REQUIRED_SECRET,
    Answer,
    BatchResult,
    Capabilities,
    FlagStore,
    ModelRef,
    Provider,
    Usage,
)

log = logging.getLogger(__name__)

JudgeError = ModelError  # the name before October 2026
__all__ = [
    "LLM",
    "BatchNotFound",
    "BudgetExceeded",
    "JudgeError",
    "ModelConfigError",
    "NoJudgments",
]


def _output_schema() -> dict[str, Any]:
    return JudgmentBatchOut.model_json_schema()


def secret_field(provider: str) -> str:
    """The `Secrets` field a provider needs set: its key, or its base URL for a local service."""
    return REQUIRED_SECRET[provider].lower()


class LLM:
    """Every provider behind one door, with cost accounting and a daily budget."""

    def __init__(self, cfg: Config, secrets: Secrets, conn: sqlite3.Connection) -> None:
        self.cfg = cfg
        self.secrets = secrets
        # worker threads account for their calls here, under the lock, on a connection of their
        # own: sqlite3 shares prepared statements per connection, so two threads running the
        # same query on one connection can hand each other empty results
        self._lock = threading.Lock()
        self.conn = self._private_connection(cfg, conn)
        self._providers: dict[str, Provider] = {}
        self._schema = _output_schema()
        self._retries: int | None = None
        for role, text in (
            ("models.judge", cfg.models.judge),
            ("models.reviewer", cfg.models.reviewer),
            ("models.profiler", cfg.models.profiler),
        ):
            ref = ModelRef.parse(text)  # a bad provider name is a configuration error, found before any work
            if not secrets.is_set(secret_field(ref.provider)):
                raise ModelConfigError(
                    f"{REQUIRED_SECRET[ref.provider]} not configured, and {role} is {text}"
                )

    @staticmethod
    def _private_connection(cfg: Config, shared: sqlite3.Connection) -> sqlite3.Connection:
        try:
            if cfg.db_path.exists():
                return store.connect(cfg.db_path)
        except sqlite3.Error:
            pass
        return shared

    # -- providers --------------------------------------------------------------

    def _flags(self) -> FlagStore:
        def get(key: str) -> str | None:
            with self._lock:
                return store.kv_get(self.conn, key)

        def set_(key: str, value: str) -> None:
            with self._lock:
                store.kv_set(self.conn, key, value)

        return FlagStore(get, set_)

    def provider(self, model: str) -> Provider:
        """The provider the model string names, made on first use (the key must be set)."""
        ref = ModelRef.parse(model)
        found = self._providers.get(ref.provider)
        if found is None:
            if not self.secrets.is_set(secret_field(ref.provider)):
                raise ModelConfigError(f"{REQUIRED_SECRET[ref.provider]} not configured (needed for {model})")
            found = make_provider(ref.provider, self.secrets, self._flags())
            if self._retries is not None:
                found.retries(self._retries)
            self._providers[ref.provider] = found
        return found

    def capabilities(self, model: str) -> Capabilities:
        return self.provider(model).capabilities(ModelRef.parse(model).model)

    def plain_json(self, model: str) -> bool:
        """Does this model get the plain-JSON instruction instead of structured outputs?"""
        return not self.capabilities(model).structured_output

    def long_ttl_ok(self, model: str) -> bool:
        provider = self.provider(model)
        check = getattr(provider, "long_ttl_ok", None)
        return bool(check(ModelRef.parse(model).model)) if check else False

    def adapt_to_error(self, model: str, message: str) -> bool:
        """A rejected request shape is remembered for that model, so the next request to it is
        different. Returns True when something changed (so a retry makes sense)."""
        return self.provider(model).adapt_to_error(ModelRef.parse(model).model, message)

    def retries(self, n: int) -> None:
        """More patience with 429s and 5xx, for a deliberate catch-up through a backlog."""
        self._retries = max(0, n)
        for provider in self._providers.values():
            provider.retries(self._retries)

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
        """(token counts, USD) for one answer's usage, the batch discount applied if any. An
        SDK usage object of Anthropic's shape is read too, for the callers that still hold one."""
        if usage is None:
            u = Usage()
        elif isinstance(usage, Usage):
            u = usage
        else:
            from .providers.anthropic import usage_from

            u = usage_from(usage)
        name = ModelRef.parse(model).name
        p_in, p_out = self.cfg.models.price(name)
        p_read = self.cfg.models.cache_read_price(name)
        write_1h, write_5m = u.cache_write_1h, u.cache_write_5m
        unsplit = max(0, u.cache_write - write_1h - write_5m)  # an SDK that reports one total
        if self.cfg.models.cache_ttl == "5m":
            write_5m += unsplit
        else:
            write_1h += unsplit
        counts = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_write": write_1h + write_5m,
            "cache_read": u.cache_read,
        }
        usd = (
            u.input_tokens * p_in
            + u.output_tokens * p_out
            + write_5m * p_in * 1.25
            + write_1h * p_in * 2.0
            + u.cache_read * p_read
        ) / 1_000_000
        return counts, usd * discount

    def record(
        self, run_id: int | None, model: str, purpose: str, usage: Any, discount: float = 1.0
    ) -> float:
        counts, usd = self.cost(model, usage, discount)
        with self._lock:
            store.record_cost(self.conn, run_id, ModelRef.parse(model).name, purpose, counts, usd)
        return usd

    # -- request shapes -------------------------------------------------------

    def _user(self, model: str, user_text: str) -> tuple[str, dict[str, Any] | None]:
        """The user text and the schema to send: the plain-JSON instruction is appended when
        the provider (or this model, after a rejection) does not enforce a schema."""
        if self.capabilities(model).structured_output:
            return user_text, self._schema
        return user_text + PLAIN_JSON_INSTRUCTION, None

    def request_params(
        self, model: str, system_text: str, user_text: str, ttl: str | None = None
    ) -> dict[str, Any]:
        """One judgment request in the provider's shape, the same for a direct call and for a batch."""
        user, schema = self._user(model, user_text)
        return self.provider(model).request(ModelRef.parse(model).model, system_text, user, schema, ttl)

    @staticmethod
    def message_text(message: Any) -> str:
        """The text of an answer: an `Answer`, or an SDK message of Anthropic's shape."""
        if isinstance(message, Answer):
            return message.text
        from .providers.anthropic import message_text

        return message_text(message)

    # -- synchronous ----------------------------------------------------------

    def judge(
        self, model: str, system_text: str, user_text: str, run_id: int | None, purpose: str
    ) -> list[Verdict]:
        """One call, waited for. Returns validated verdicts; invalid ones are dropped."""
        self.ensure_budget()
        user, schema = self._user(model, user_text)
        answer = self.provider(model).judge(ModelRef.parse(model).model, system_text, user, schema, None)
        self.record(run_id, model, purpose, answer.usage)
        verdicts = parse_judgments(answer.text)
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
        answer = self.provider(model).complete(
            ModelRef.parse(model).model, system_text, user_text, max_tokens
        )
        self.record(run_id, model, purpose, answer.usage)
        return answer.text.strip()

    # -- batches --------------------------------------------------------------

    def submit_batch(self, model: str, requests: list[dict[str, Any]]) -> str:
        """requests: [{"custom_id": ..., "params": ...}] for one model. Returns the batch id."""
        return self.provider(model).submit_batch(ModelRef.parse(model).model, requests)

    def batch_status(self, model: str, batch_id: str) -> str:
        """'in_progress', 'canceling' or 'ended'."""
        return self.provider(model).batch_status(ModelRef.parse(model).model, batch_id)

    def batch_results(self, model: str, batch_id: str) -> list[BatchResult]:
        return self.provider(model).collect_batch(ModelRef.parse(model).model, batch_id)
