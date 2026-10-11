"""Logging: where the log lives, the redactor that keeps secrets out of it, the handlers."""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger(__name__)

def log_file(data_dir: Path) -> Path:
    """Where the run log lives: ~/Library/Logs/bodh on a Mac, data/logs elsewhere."""
    logs = Path(os.path.expanduser("~/Library/Logs"))
    if not logs.exists():
        return data_dir / "logs" / "bodh.log"
    return logs / "bodh" / "bodh.log"

class Redactor(logging.Filter):
    """Every secret value, and any auth token, is replaced before a line is written anywhere:
    the log file's tail reaches the console, and a traceback or an SDK error can carry
    what the code itself never prints."""

    _TOKENS = re.compile(r"(GoogleLogin auth=|Bearer |sk-ant-)\S+")

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        return self._TOKENS.sub(r"\1[redacted]", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a bad format string is logging's problem, not ours
            return True
        clean = self.redact(message)
        if clean != message or record.args:
            record.msg = clean
            record.args = ()
        if record.exc_info and not record.exc_text:
            # a handler's filter runs before its formatter caches the traceback: cache it clean
            record.exc_text = self.redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        return True

def setup_logging(data_dir: Path, verbose: bool = False, secrets: Iterable[str] = ()) -> None:
    """The run log: the file (rotated) plus stderr when someone is watching a terminal.
    ``secrets`` are the values that must never appear in a line."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    redactor = Redactor(secrets)
    handlers: list[logging.Handler] = []
    if verbose or _stderr_is_a_terminal():
        handlers.append(logging.StreamHandler(sys.stderr))
    try:
        path = log_file(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass
    for handler in handlers:
        handler.addFilter(redactor)
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)

def _stderr_is_a_terminal() -> bool:
    # under launchd stderr is a file that already receives the log through the file handler
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False
