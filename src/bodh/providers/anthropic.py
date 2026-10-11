"""Anthropic: the Messages API through the official SDK, with the Message Batches API, the
1-hour prompt cache, structured outputs, and the two fallbacks a model may force (plain JSON
instead of structured outputs, the 5-minute cache instead of the 1-hour one), remembered per
model across runs."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..errors import BatchNotFound, ModelConfigError
from .base import Answer, BatchResult, Capabilities, FlagStore, Provider, Usage, batch_status_of

log = logging.getLogger(__name__)

PLAIN_JSON_KEY = "provider.plain_json"  # a JSON list of the models that rejected structured outputs
CACHE_TTL_KEY = "provider.cache_ttl"  # a JSON list of the models that rejected the 1-hour cache
_STRUCTURED_WORDS = re.compile(r"\b(output_config|output_format|structured|json_schema)\b", re.I)
_TTL_WORDS = re.compile(r"\b(ttl|cache_control)\b", re.I)
MAX_TOKENS = 8000


def mentions_structured_outputs(text: str) -> bool:
    return bool(_STRUCTURED_WORDS.search(text or ""))


def mentions_cache_ttl(text: str) -> bool:
    # whole words only: "throttled" contains "ttl" and used to disable the 1-hour cache for good
    return bool(_TTL_WORDS.search(text or ""))


def flagged_models(stored: str | None, legacy: str) -> set[str]:
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


def output_format(schema: dict[str, Any]) -> dict[str, Any]:
    """The structured-outputs format for a judgment batch, built the way the SDK's
    ``messages.parse`` builds it (bounds moved into descriptions, no extra properties)."""
    try:
        from anthropic.lib._parse._transform import transform_schema

        schema = transform_schema(schema)
    except Exception:  # an SDK without that private helper: the plain schema is sent, and still accepted
        log.debug("anthropic: anthropic.lib._parse._transform is not available; sending the plain schema")
    return {"type": "json_schema", "schema": schema}


def usage_from(raw: Any) -> Usage:
    """The SDK's usage object as the budget reads it; an older SDK without the lifetime split
    reports one total, which the caller prices by the configured lifetime."""
    creation = getattr(raw, "cache_creation", None)
    write_1h = int(getattr(creation, "ephemeral_1h_input_tokens", 0) or 0)
    write_5m = int(getattr(creation, "ephemeral_5m_input_tokens", 0) or 0)
    total = int(getattr(raw, "cache_creation_input_tokens", 0) or 0)
    # an older SDK reports one total and no split: cache_write carries it, the lifetimes stay 0,
    # and the budget prices the unsplit part by the configured lifetime
    return Usage(
        input_tokens=int(getattr(raw, "input_tokens", 0) or 0),
        output_tokens=int(getattr(raw, "output_tokens", 0) or 0),
        cache_read=int(getattr(raw, "cache_read_input_tokens", 0) or 0),
        cache_write=max(total, write_1h + write_5m),
        cache_write_1h=write_1h,
        cache_write_5m=write_5m,
    )


def message_text(message: Any) -> str:
    return "".join(getattr(b, "text", "") for b in getattr(message, "content", []) or [])


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, key: str, flags: FlagStore, client: Any = None, base_url: str | None = None) -> None:
        super().__init__(key, base_url)
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=key, max_retries=2, timeout=180.0, base_url=base_url)
        self._client = client
        self._flags = flags
        # once a model has rejected structured outputs, every later call to it asks for plain JSON
        # instead; once it has rejected the 1-hour cache, it gets the default 5-minute cache. Both are
        # remembered per model in the database ("1"/"5m" alone, from before October 2026: every model)
        self._plain_json_models = flagged_models(flags.get(PLAIN_JSON_KEY), "1")
        self._short_ttl_models = flagged_models(flags.get(CACHE_TTL_KEY), "5m")

    # -- capabilities and flags -------------------------------------------------

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            batch=True, structured_output=not self.plain_json(model), prompt_cache=True, max_context=200_000
        )

    def plain_json(self, model: str) -> bool:
        """Does this model get the plain-JSON instruction instead of structured outputs?"""
        return "*" in self._plain_json_models or model in self._plain_json_models

    def long_ttl_ok(self, model: str) -> bool:
        """May this model's prompt prefix be cached for an hour?"""
        return "*" not in self._short_ttl_models and model not in self._short_ttl_models

    def _enable_plain_json(self, model: str) -> None:
        if not self.plain_json(model):
            log.warning(
                "anthropic: %s rejected structured outputs; asking it for plain JSON from now on", model
            )
            self._plain_json_models.add(model)
            self._flags.set(PLAIN_JSON_KEY, json.dumps(sorted(self._plain_json_models)))

    def _disable_long_ttl(self, model: str) -> None:
        if self.long_ttl_ok(model):
            log.warning(
                "anthropic: %s rejected the 1-hour cache; using the 5-minute cache for it from now on", model
            )
            self._short_ttl_models.add(model)
            self._flags.set(CACHE_TTL_KEY, json.dumps(sorted(self._short_ttl_models)))

    def adapt_to_error(self, model: str, message: str) -> bool:
        if not self.plain_json(model) and mentions_structured_outputs(message):
            self._enable_plain_json(model)
            return True
        if self.long_ttl_ok(model) and mentions_cache_ttl(message):
            self._disable_long_ttl(model)
            return True
        return False

    def retries(self, n: int) -> None:
        self._client = self._client.with_options(max_retries=max(0, n))

    # -- errors -----------------------------------------------------------------

    def _classify(self, exc: Exception) -> Exception:
        """A rejected key, permission or model id becomes a ModelConfigError (exit 4); anything
        else is passed through. The message never carries the key, whatever the API echoed."""
        import anthropic

        if isinstance(
            exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError)
        ):
            return ModelConfigError(
                f"model call rejected: {self.scrub(str(exc))}. Check ANTHROPIC_API_KEY and the model ids in config.yaml"
            )
        return exc

    def _batch_classify(self, exc: Exception) -> Exception:
        import anthropic

        if isinstance(exc, anthropic.NotFoundError):
            return BatchNotFound(f"batch not found: {self.scrub(str(exc))}")
        return self._classify(exc)

    # -- request shapes ---------------------------------------------------------

    def _system(self, model: str, system: str, ttl: str | None) -> list[dict[str, Any]]:
        cache: dict[str, Any] = {"type": "ephemeral"}
        if ttl == "1h" and self.long_ttl_ok(model):
            cache["ttl"] = "1h"
        return [{"type": "text", "text": system, "cache_control": cache}]

    def request(
        self, model: str, system: str, user: str, schema: dict[str, Any] | None, ttl: str | None
    ) -> dict[str, Any]:
        """One judgment request, the same shape for a direct call and for a batch. The caller
        has already appended the plain-JSON instruction when `structured_output` is off."""
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": self._system(model, system, ttl),
            "messages": [{"role": "user", "content": user}],
        }
        if schema is not None and not self.plain_json(model):
            params["output_config"] = {"format": output_format(schema)}
        return params

    # -- synchronous ------------------------------------------------------------

    def judge(
        self, model: str, system: str, user: str, schema: dict[str, Any] | None, ttl: str | None
    ) -> Answer:
        import anthropic

        try:
            response = self._client.messages.create(**self.request(model, system, user, schema, ttl))
        except anthropic.BadRequestError as exc:
            if not self.adapt_to_error(model, str(exc)):
                raise self._classify(exc) from exc
            try:
                response = self._client.messages.create(**self.request(model, system, user, schema, ttl))
            except Exception as exc2:
                raise self._classify(exc2) from exc2
        except Exception as exc:
            raise self._classify(exc) from exc
        return Answer(message_text(response), usage_from(response.usage))

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> Answer:
        try:
            response = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        return Answer(message_text(response).strip(), usage_from(response.usage))

    # -- batches ----------------------------------------------------------------

    def submit_batch(self, model: str, requests: list[dict[str, Any]]) -> str:
        try:
            # no automatic retry here: a create that timed out after the server accepted it would
            # be retried into a second, paid batch nobody collects. A failed create is raised, its
            # articles stay pending, and the next run submits them again.
            no_retry = self._client.with_options(max_retries=0)
            batch = no_retry.messages.batches.create(requests=requests)
        except Exception as exc:
            raise self._classify(exc) from exc
        return str(batch.id)

    def batch_status(self, model: str, batch_id: str) -> str:
        try:
            return str(self._client.messages.batches.retrieve(batch_id).processing_status)
        except Exception as exc:
            raise self._batch_classify(exc) from exc

    def collect_batch(self, model: str, batch_id: str) -> list[BatchResult]:
        try:
            raw = list(self._client.messages.batches.results(batch_id))
        except Exception as exc:
            raise self._batch_classify(exc) from exc
        out: list[BatchResult] = []
        for item in raw:
            result = item.result
            kind = str(getattr(result, "type", "") or "")
            if kind == "succeeded":
                message = result.message
                out.append(
                    BatchResult(
                        item.custom_id, "succeeded", Answer(message_text(message), usage_from(message.usage))
                    )
                )
                continue
            err = getattr(getattr(result, "error", None), "error", None)
            status = batch_status_of(kind)
            out.append(
                BatchResult(
                    item.custom_id, status, None, f"{kind or '?'}: {getattr(err, 'message', '') or ''}"
                )
            )
        return out
