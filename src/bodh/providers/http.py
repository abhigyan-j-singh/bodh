"""The plumbing the HTTP providers share: one `httpx.Client`, a bounded retry with backoff and
jitter on 429 and 5xx and on connection errors, the key never in a message, and the
translation of a status code into the error kinds the run understands (a rejected key,
permission or model id stops the run; anything else is one failed call)."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from ..errors import ModelConfigError, ModelError
from .base import Provider

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 180.0
DEFAULT_ATTEMPTS = 3  # the first try and two retries, as the Anthropic SDK does by default
BACKOFF_BASE = 1.5
BACKOFF_CAP = 20.0


class HttpProvider(Provider):
    """A provider over plain HTTP. Subclasses set `name`, `base_url` and the headers."""

    def __init__(self, key: str, base_url: str, transport: Any = None, name: str | None = None) -> None:
        super().__init__(key, base_url.rstrip("/"))
        if name:
            self.name = name
        self._http = httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport, follow_redirects=False)
        self._attempts = DEFAULT_ATTEMPTS

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}

    def retries(self, n: int) -> None:
        self._attempts = max(0, n) + 1

    def _url(self, path: str) -> str:
        assert self.base_url is not None
        return path if path.startswith("http") else f"{self.base_url}{path}"

    def request(
        self, model: str, system: str, user: str, schema: dict[str, Any] | None, ttl: str | None
    ) -> dict[str, Any]:
        raise NotImplementedError

    def call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One request with the bounded retry; a 4xx other than 429 is not retried."""
        last: Exception | None = None
        for attempt in range(self._attempts):
            if attempt:
                sleep = min(BACKOFF_CAP, BACKOFF_BASE**attempt) * random.uniform(
                    0.5, 1.5
                )  # jitter, not security
                log.warning(
                    "%s: %s %s: retry %d in %.1fs (%s)", self.name, method, path, attempt, sleep, last
                )
                time.sleep(sleep)
            try:
                response = self._http.request(
                    method, self._url(path), headers={**self.headers(), **kwargs.pop("headers", {})}, **kwargs
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last = exc
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last = ModelError(
                    f"{self.name}: HTTP {response.status_code} from {path}: {self.scrub(response.text[:200])}"
                )
                continue
            if response.status_code >= 400:
                raise self.classify(response, path)
            return response
        raise ModelError(
            f"{self.name}: {method} {path} failed after {self._attempts} attempts: {self.scrub(str(last))}"
        )

    def classify(self, response: httpx.Response, path: str) -> Exception:
        """A 401 or 403, or a 404 that names the model: the configuration is wrong and the run
        stops (exit 4). Any other 4xx is this call's failure."""
        text = self.scrub(response.text[:300])
        if response.status_code in (401, 403) or (response.status_code == 404 and "model" in text.lower()):
            return ModelConfigError(
                f"{self.name}: model call rejected: HTTP {response.status_code}: {text}. "
                f"Check the key in .env and the model ids in config.yaml"
            )
        return ModelError(f"{self.name}: HTTP {response.status_code} from {path}: {text}")

    @staticmethod
    def error_text(body: Any) -> str:
        """The message out of an error body, whatever its shape."""
        if isinstance(body, dict):
            err = body.get("error", body)
            if isinstance(err, dict):
                return str(err.get("message") or err.get("type") or err)
            return str(err)
        return str(body)
