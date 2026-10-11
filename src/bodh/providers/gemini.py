"""Google Gemini over its REST API: `generateContent` in JSON mode with a response schema
(the judgment schema rewritten into the OpenAPI subset Gemini takes: references inlined,
the keys it does not know dropped), and the batch API (`batchGenerateContent` with the
requests inline, polled as an operation, the answers inline in the result). The implicit
context cache counts as a prompt cache for the budget."""

from __future__ import annotations

import logging
from typing import Any

from ..errors import BatchNotFound, ModelError
from .base import Answer, BatchResult, Capabilities, Usage, batch_status_of, estimate_usage
from .http import HttpProvider

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
MAX_OUTPUT_TOKENS = 8000
ENDED = {"BATCH_STATE_SUCCEEDED", "BATCH_STATE_FAILED", "BATCH_STATE_CANCELLED", "BATCH_STATE_EXPIRED"}
_KEEP = {"type", "properties", "items", "enum", "description", "required", "nullable", "format", "anyOf"}


def gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The JSON schema pydantic writes, as Gemini's response schema: `$ref`s inlined,
    `anyOf [X, null]` folded into `nullable`, and only the keys Gemini accepts kept."""
    defs = schema.get("$defs") or {}

    def convert(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            name = str(node["$ref"]).rsplit("/", 1)[-1]
            return convert(defs.get(name, {}))
        out: dict[str, Any] = {}
        options = node.get("anyOf")
        if isinstance(options, list):
            real = [o for o in options if not (isinstance(o, dict) and o.get("type") == "null")]
            if len(real) == 1 and len(real) < len(options):
                out = convert(real[0])
                out["nullable"] = True
                if "description" in node:
                    out["description"] = node["description"]
                return out
        for key, value in node.items():
            if key not in _KEEP:
                continue
            if key == "properties" and isinstance(value, dict):
                out[key] = {k: convert(v) for k, v in value.items()}
            elif key == "items":
                out[key] = convert(value)
            elif key == "anyOf" and isinstance(value, list):
                out[key] = [convert(v) for v in value]
            else:
                out[key] = value
        return out

    converted = convert(schema)
    return dict(converted) if isinstance(converted, dict) else {}


def _usage(raw: dict[str, Any] | None, system: str, user: str, text: str) -> Usage:
    if not raw:
        return estimate_usage(system, user, text)
    prompt = int(raw.get("promptTokenCount") or 0)
    cached = int(raw.get("cachedContentTokenCount") or 0)
    return Usage(
        input_tokens=max(0, prompt - cached),
        output_tokens=int(raw.get("candidatesTokenCount") or 0) + int(raw.get("thoughtsTokenCount") or 0),
        cache_read=cached,
    )


def candidate_text(body: dict[str, Any]) -> str:
    parts = []
    for candidate in body.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            if "text" in part and not part.get("thought"):
                parts.append(str(part["text"]))
    return "".join(parts)


class GeminiProvider(HttpProvider):
    name = "gemini"

    def __init__(self, key: str, base_url: str = GEMINI_URL, transport: Any = None) -> None:
        super().__init__(key, base_url, transport)

    def headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.key}

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(batch=True, structured_output=True, prompt_cache=True)

    def request(
        self, model: str, system: str, user: str, schema: dict[str, Any] | None, ttl: str | None
    ) -> dict[str, Any]:
        config: dict[str, Any] = {"maxOutputTokens": MAX_OUTPUT_TOKENS}
        if schema is not None:
            config["responseMimeType"] = "application/json"
            config["responseSchema"] = gemini_schema(schema)
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }

    def _answer(self, body: dict[str, Any], system: str, user: str) -> Answer:
        text = candidate_text(body)
        return Answer(text, _usage(body.get("usageMetadata"), system, user, text))

    def judge(
        self, model: str, system: str, user: str, schema: dict[str, Any] | None, ttl: str | None
    ) -> Answer:
        response = self.call(
            "POST", f"/models/{model}:generateContent", json=self.request(model, system, user, schema, ttl)
        )
        return self._answer(response.json(), system, user)

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> Answer:
        body = self.request(model, system, user, None, None)
        body["generationConfig"]["maxOutputTokens"] = max_tokens
        response = self.call("POST", f"/models/{model}:generateContent", json=body)
        answer = self._answer(response.json(), system, user)
        return Answer(answer.text.strip(), answer.usage)

    # -- batches ----------------------------------------------------------------

    def submit_batch(self, model: str, requests: list[dict[str, Any]]) -> str:
        body = {
            "batch": {
                "display_name": "bodh",
                "input_config": {
                    "requests": {
                        "requests": [
                            {"request": r["params"], "metadata": {"key": r["custom_id"]}} for r in requests
                        ]
                    }
                },
            }
        }
        attempts, self._attempts = self._attempts, 1  # no retry on the create: see openai.py
        try:
            response = self.call("POST", f"/models/{model}:batchGenerateContent", json=body)
        finally:
            self._attempts = attempts
        return str(response.json()["name"])

    def _operation(self, batch_id: str) -> dict[str, Any]:
        try:
            return dict(self.call("GET", f"/{batch_id}").json())
        except ModelError as exc:
            if "404" in str(exc):
                raise BatchNotFound(f"batch not found: {batch_id}") from exc
            raise

    def batch_status(self, model: str, batch_id: str) -> str:
        op = self._operation(batch_id)
        state = str((op.get("metadata") or {}).get("state") or "")
        if state in ENDED or op.get("done"):
            return "ended"
        return "canceling" if state == "BATCH_STATE_CANCELLING" else "in_progress"

    def collect_batch(self, model: str, batch_id: str) -> list[BatchResult]:
        op = self._operation(batch_id)
        state = str((op.get("metadata") or {}).get("state") or "")
        inlined = ((op.get("response") or {}).get("inlinedResponses") or {}).get("inlinedResponses") or []
        out: list[BatchResult] = []
        for item in inlined:
            custom_id = str((item.get("metadata") or {}).get("key") or "")
            if item.get("error") or not item.get("response"):
                message = self.error_text(item.get("error") or {"message": "no response"})
                status = batch_status_of("expired" if state == "BATCH_STATE_EXPIRED" else "errored")
                out.append(BatchResult(custom_id, status, None, f"{status}: {self.scrub(message)}"))
                continue
            body = item["response"]
            text = candidate_text(body)
            out.append(
                BatchResult(
                    custom_id, "succeeded", Answer(text, _usage(body.get("usageMetadata"), "", "", text))
                )
            )
        if not out and state in ENDED and state != "BATCH_STATE_SUCCEEDED":
            log.warning(
                "gemini: batch %s %s with no results (%s)", batch_id, state, self.error_text(op.get("error"))
            )
        return out
