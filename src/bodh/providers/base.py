"""One interface for every model provider, and the model reference that names one.

A role in `config.yaml` (`models.judge`, `models.reviewer`, `models.profiler`, `catch_up.model`)
is a string `provider/model-id`: `anthropic/claude-sonnet-5-5`, `openai/gpt-5`,
`gemini/gemini-2.5-pro`, `openrouter/qwen/qwen3-235b`, `openai-compatible/llama3.3`. A bare
model id means Anthropic, as every configuration before October 2026 wrote it, and the
database keeps that bare spelling for Anthropic models (`ModelRef.name`), so nothing stored
changes. No allow-list: an unknown model id is passed to the provider unchanged, probed once
by `doctor --live`, and recorded with every score it produces.

A provider answers a judgment request synchronously (`judge`), a plain completion
(`complete`), and, when it can, a batch (`request`, `submit_batch`, `batch_status`,
`collect_batch`); it reports what it can do (`capabilities`) and turns its own errors into
`errors.ModelError` kinds. The run never knows which provider it is talking to."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from ..errors import ConfigError

# the providers, with the .env name of each one's key
PROVIDERS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai-compatible": "OPENAI_COMPATIBLE_API_KEY",
}
DEFAULT_PROVIDER = "anthropic"
# what must be set in .env before a provider can be used: its key, or for a local or
# self-hosted service that may need no key, where it is
REQUIRED_SECRET: dict[str, str] = {**PROVIDERS, "openai-compatible": "OPENAI_COMPATIBLE_BASE_URL"}


@dataclass(frozen=True)
class ModelRef:
    """A provider and a model id, parsed from the string a role names."""

    provider: str
    model: str

    @classmethod
    def parse(cls, text: str) -> ModelRef:
        """`provider/model` (the model may carry slashes of its own), or a bare Anthropic model id."""
        text = (text or "").strip()
        if not text:
            raise ConfigError("a model is an empty string; name one as provider/model-id")
        head, sep, rest = text.partition("/")
        if sep and head in PROVIDERS:
            if not rest.strip():
                raise ConfigError(f"{text!r} names no model after the provider")
            return cls(head, rest.strip())
        if sep and head not in PROVIDERS:
            raise ConfigError(
                f"{text!r}: unknown provider {head!r}; one of {', '.join(PROVIDERS)} (a bare model id means anthropic)"
            )
        return cls(DEFAULT_PROVIDER, text)

    @property
    def name(self) -> str:
        """What the database stores and `models.prices_per_mtok` is keyed by: the bare id for
        Anthropic (as before October 2026), `provider/model` for the rest."""
        return self.model if self.provider == DEFAULT_PROVIDER else f"{self.provider}/{self.model}"

    @property
    def key_name(self) -> str:
        return PROVIDERS[self.provider]

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


def model_name(text: str) -> str:
    """The stored name for a role's string (see ModelRef.name)."""
    return ModelRef.parse(text).name


def same_model(a: str | None, b: str | None) -> bool:
    """Do two model strings name the same model, whatever their spelling?"""
    if not a or not b:
        return False
    try:
        return ModelRef.parse(a) == ModelRef.parse(b)
    except ConfigError:
        return a == b


@dataclass
class Usage:
    """Token counts as the budget reads them, whatever the provider calls them. `cache_write`
    is split by lifetime where the provider prices them apart; `estimated` says the provider
    reported nothing and these are a guess from the text lengths."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_1h: int = 0
    cache_write_5m: int = 0
    estimated: bool = False


@dataclass
class Answer:
    text: str
    usage: Usage = field(default_factory=Usage)


BatchStatus = Literal["succeeded", "errored", "expired", "canceled"]


def batch_status_of(text: str) -> BatchStatus:
    """A result's status as the run reads it; anything unknown is an error."""
    if text == "succeeded":
        return "succeeded"
    if text == "expired":
        return "expired"
    if text in ("canceled", "cancelled"):
        return "canceled"
    return "errored"


@dataclass
class BatchResult:
    custom_id: str
    status: BatchStatus
    answer: Answer | None = None
    error: str = ""


@dataclass(frozen=True)
class Capabilities:
    batch: bool
    structured_output: bool
    prompt_cache: bool
    max_context: int | None = None


class Provider(ABC):
    """What `llm.LLM` asks of a provider. Implementations translate their service's errors
    into `errors.ModelError` kinds (`llm.ModelConfigError` for a rejected key, permission or
    model id; `llm.BatchNotFound` for a batch the service no longer knows) and never let the
    key into a message."""

    name: str = ""

    def __init__(self, key: str, base_url: str | None = None) -> None:
        self.key = key
        self.base_url = base_url

    @abstractmethod
    def capabilities(self, model: str) -> Capabilities: ...

    @abstractmethod
    def judge(
        self, model: str, system: str, user: str, schema: dict[str, Any] | None, ttl: str | None
    ) -> Answer:
        """One judgment call, waited for. `schema` is the judgment batch's JSON schema, for a
        provider that enforces one; `ttl` the cache lifetime wanted for the prefix."""

    @abstractmethod
    def complete(self, model: str, system: str, user: str, max_tokens: int) -> Answer: ...

    @abstractmethod
    def request(
        self, model: str, system: str, user: str, schema: dict[str, Any] | None, ttl: str | None
    ) -> dict[str, Any]:
        """The request body one batch item carries, in the provider's own shape."""

    @abstractmethod
    def submit_batch(self, model: str, requests: list[dict[str, Any]]) -> str:
        """`requests`: `[{"custom_id": ..., "params": ...}]`; returns the batch id."""

    @abstractmethod
    def batch_status(self, model: str, batch_id: str) -> str:
        """'in_progress', 'canceling' or 'ended'."""

    @abstractmethod
    def collect_batch(self, model: str, batch_id: str) -> list[BatchResult]: ...

    def adapt_to_error(self, model: str, message: str) -> bool:
        """A rejected request shape remembered for the model, so the next request differs;
        True when something changed. Most providers have nothing to adapt."""
        return False

    def retries(self, n: int) -> None:  # noqa: B027  # a provider with no retry of its own has nothing to set
        """More patience with 429s and 5xx, for a deliberate catch-up through a backlog."""

    def scrub(self, text: str) -> str:
        """The text with the key, if it found its way in, replaced."""
        return text.replace(self.key, "[the API key]") if self.key else text


class FlagStore:
    """Where a provider remembers what a model rejected (the plain-JSON and short-cache
    fallbacks), across runs: a get and a set on a kv table, under the LLM's lock."""

    def __init__(self, get: Any, set_: Any) -> None:
        self.get = get
        self.set = set_


def estimate_usage(system: str, user: str, answer: str) -> Usage:
    """A guess at the token counts, four characters to a token, for a provider that reports
    none; the budget then counts the estimate and `progress` says so."""
    return Usage(
        input_tokens=(len(system) + len(user)) // 4, output_tokens=max(1, len(answer) // 4), estimated=True
    )
