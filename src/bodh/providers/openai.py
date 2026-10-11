"""OpenAI, and the services that speak its shape.

`OpenAIProvider`: the Responses API with a JSON schema, and the Batch API (a JSONL file
uploaded, a batch created over it, polled, its output file read back). Prompt caching is
automatic there, so a request carries no cache control. `OpenAICompatibleProvider`: the Chat
Completions shape, which OpenRouter, Ollama, LM Studio, vLLM and most gateways answer; a JSON
schema when the service says it accepts one, else JSON mode and the lenient parser; no
batches, so the run judges synchronously with `limits.parallel_calls`."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..errors import BatchNotFound, ModelError
from .base import Answer, BatchResult, Capabilities, Usage, batch_status_of, estimate_usage
from .http import HttpProvider

log = logging.getLogger(__name__)

OPENAI_URL = "https://api.openai.com/v1"
MAX_OUTPUT_TOKENS = 8000
BATCH_STATES_ENDED = {"completed", "failed", "expired", "cancelled"}


def _usage(raw: dict[str, Any] | None, system: str, user: str, text: str) -> Usage:
    if not raw:
        return estimate_usage(system, user, text)
    details = raw.get("input_tokens_details") or raw.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    prompt = int(raw.get("input_tokens") or raw.get("prompt_tokens") or 0)
    return Usage(
        input_tokens=max(0, prompt - cached),
        output_tokens=int(raw.get("output_tokens") or raw.get("completion_tokens") or 0),
        cache_read=cached,
    )


class OpenAIProvider(HttpProvider):
    name = "openai"

    def __init__(self, key: str, base_url: str = OPENAI_URL, transport: Any = None) -> None:
        super().__init__(key, base_url, transport)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(batch=True, structured_output=True, prompt_cache=True)

    # -- the Responses API ------------------------------------------------------

    def request(
        self, model: str, system: str, user: str, schema: dict[str, Any] | None, ttl: str | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "instructions": system,
            "input": user,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }
        if schema is not None:
            body["text"] = {
                "format": {"type": "json_schema", "name": "judgments", "schema": schema, "strict": False}
            }
        return body

    @staticmethod
    def response_text(body: dict[str, Any]) -> str:
        """The output text of a Responses API answer; `output_text` when the service adds it."""
        if body.get("output_text"):
            return str(body["output_text"])
        parts = []
        for item in body.get("output") or []:
            if item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if content.get("type") == "output_text":
                    parts.append(str(content.get("text") or ""))
        return "".join(parts)

    def _answer(self, body: dict[str, Any], system: str, user: str) -> Answer:
        text = self.response_text(body)
        return Answer(text, _usage(body.get("usage"), system, user, text))

    def judge(
        self, model: str, system: str, user: str, schema: dict[str, Any] | None, ttl: str | None
    ) -> Answer:
        response = self.call("POST", "/responses", json=self.request(model, system, user, schema, ttl))
        return self._answer(response.json(), system, user)

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> Answer:
        body = {"model": model, "instructions": system, "input": user, "max_output_tokens": max_tokens}
        response = self.call("POST", "/responses", json=body)
        answer = self._answer(response.json(), system, user)
        return Answer(answer.text.strip(), answer.usage)

    # -- the Batch API ----------------------------------------------------------

    def submit_batch(self, model: str, requests: list[dict[str, Any]]) -> str:
        lines = [
            json.dumps(
                {"custom_id": r["custom_id"], "method": "POST", "url": "/v1/responses", "body": r["params"]}
            )
            for r in requests
        ]
        content = ("\n".join(lines) + "\n").encode("utf-8")
        upload = self.call(
            "POST",
            "/files",
            files={"file": ("batch.jsonl", content, "application/jsonl")},
            data={"purpose": "batch"},
        )
        file_id = str(upload.json()["id"])
        # no retry on the create itself: a create that timed out after the server accepted it would
        # be retried into a second, paid batch nobody collects
        attempts, self._attempts = self._attempts, 1
        try:
            created = self.call(
                "POST",
                "/batches",
                json={"input_file_id": file_id, "endpoint": "/v1/responses", "completion_window": "24h"},
            )
        finally:
            self._attempts = attempts
        return str(created.json()["id"])

    def _batch(self, batch_id: str) -> dict[str, Any]:
        try:
            return dict(self.call("GET", f"/batches/{batch_id}").json())
        except ModelError as exc:
            if "404" in str(exc):
                raise BatchNotFound(f"batch not found: {batch_id}") from exc
            raise

    def batch_status(self, model: str, batch_id: str) -> str:
        status = str(self._batch(batch_id).get("status") or "")
        if status in BATCH_STATES_ENDED:
            return "ended"
        return "canceling" if status == "cancelling" else "in_progress"

    def collect_batch(self, model: str, batch_id: str) -> list[BatchResult]:
        batch = self._batch(batch_id)
        status = str(batch.get("status") or "")
        out: list[BatchResult] = []
        for file_key in ("output_file_id", "error_file_id"):
            file_id = batch.get(file_key)
            if not file_id:
                continue
            text = self.call("GET", f"/files/{file_id}/content").text
            for line in text.splitlines():
                if not line.strip():
                    continue
                out.append(self._result(json.loads(line), status))
        if not out and status in ("expired", "cancelled", "failed"):
            # the service gave the whole batch up with nothing in it: the caller closes it and its
            # articles are pending again
            log.warning(
                "openai: batch %s %s with no results (%s)",
                batch_id,
                status,
                self.error_text(batch.get("errors")),
            )
        return out

    def _result(self, line: dict[str, Any], batch_status: str) -> BatchResult:
        custom_id = str(line.get("custom_id") or "")
        response = line.get("response") or {}
        body = response.get("body") or {}
        if line.get("error") or int(response.get("status_code") or 0) >= 400 or not body:
            message = self.error_text(
                line.get("error") or body or {"message": f"HTTP {response.get('status_code')}"}
            )
            status = batch_status_of("expired" if batch_status == "expired" else "errored")
            return BatchResult(custom_id, status, None, f"{status}: {self.scrub(message)}")
        text = self.response_text(body)
        return BatchResult(custom_id, "succeeded", Answer(text, _usage(body.get("usage"), "", "", text)))


class OpenAICompatibleProvider(HttpProvider):
    """Chat Completions against any base URL: OpenRouter (`openrouter`), Ollama, LM Studio,
    vLLM and gateways (`openai-compatible`, with `OPENAI_COMPATIBLE_BASE_URL`). A JSON schema is
    sent as `response_format`; a service that rejects it gets JSON mode from then on."""

    name = "openai-compatible"

    def __init__(self, key: str, base_url: str, transport: Any = None, name: str | None = None) -> None:
        if not base_url:
            raise ModelError("OPENAI_COMPATIBLE_BASE_URL not configured: where the service is")
        super().__init__(key, base_url, transport, name)
        self._json_mode_models: set[str] = set()

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            batch=False, structured_output=model not in self._json_mode_models, prompt_cache=False
        )

    def adapt_to_error(self, model: str, message: str) -> bool:
        text = (message or "").lower()
        if model not in self._json_mode_models and (
            "response_format" in text or "json_schema" in text or "schema" in text
        ):
            log.warning(
                "%s: %s rejected the JSON schema; asking it for plain JSON from now on", self.name, model
            )
            self._json_mode_models.add(model)
            return True
        return False

    def request(
        self, model: str, system: str, user: str, schema: dict[str, Any] | None, ttl: str | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": MAX_OUTPUT_TOKENS,
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "judgments", "schema": schema, "strict": False},
            }
        elif model in self._json_mode_models:
            body["response_format"] = {"type": "json_object"}
        return body

    @staticmethod
    def completion_text(body: dict[str, Any]) -> str:
        choices = body.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # some gateways answer with parts
            return "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
        return str(content or "")

    def judge(
        self, model: str, system: str, user: str, schema: dict[str, Any] | None, ttl: str | None
    ) -> Answer:
        try:
            response = self.call(
                "POST", "/chat/completions", json=self.request(model, system, user, schema, ttl)
            )
        except ModelError as exc:
            if schema is None or not self.adapt_to_error(model, str(exc)):
                raise
            response = self.call(
                "POST", "/chat/completions", json=self.request(model, system, user, None, ttl)
            )
        body = response.json()
        text = self.completion_text(body)
        return Answer(text, _usage(body.get("usage"), system, user, text))

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> Answer:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
        }
        response = self.call("POST", "/chat/completions", json=body)
        data = response.json()
        text = self.completion_text(data)
        return Answer(text.strip(), _usage(data.get("usage"), system, user, text))

    def submit_batch(self, model: str, requests: list[dict[str, Any]]) -> str:
        raise ModelError(
            f"{self.name} has no batch API; set models.batch: false or pick another provider for the judge"
        )

    def batch_status(self, model: str, batch_id: str) -> str:
        raise BatchNotFound(f"{self.name} has no batch API; batch {batch_id} cannot exist")

    def collect_batch(self, model: str, batch_id: str) -> list[BatchResult]:
        raise BatchNotFound(f"{self.name} has no batch API; batch {batch_id} cannot exist")
