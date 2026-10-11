"""The heartbeat: a ping to healthchecks.io after every run, so silence raises an alert."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


def _host_only(url: str) -> str:
    """A ping URL without its secret path (the check's UUID), for log lines."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}/…"
    except ValueError:
        return "<heartbeat url>"


class Heartbeat:
    """healthchecks.io: a ping on success (with the run summary), /fail on failure.
    Never raises; a missing heartbeat must not fail a run."""

    def __init__(self, url: str) -> None:
        self.url = url.strip().rstrip("/")

    def _post(self, suffix: str, body: str) -> None:
        if not self.url:
            return
        try:
            httpx.post(f"{self.url}{suffix}", content=body[:10000].encode("utf-8"), timeout=10.0)
        except Exception as exc:
            log.warning("heartbeat: could not reach %s (%s)", _host_only(self.url) + suffix, exc)

    def ok(self, summary: str) -> None:
        self._post("", summary)

    def fail(self, error: str) -> None:
        self._post("/fail", error)

    def start(self) -> None:
        self._post("/start", "")
