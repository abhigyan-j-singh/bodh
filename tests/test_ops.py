"""Locks, logging, the heartbeat: the plumbing under every command."""

from __future__ import annotations

import json
import logging

from conftest import seed_scored

from triage import dashboard, ops, progress


def test_live_file_never_carries_env_values(conn, cfg, tmp_path, monkeypatch):
    """A log line with a secret in it must not reach progress.json or the dashboard, which
    README says to publish; the redactor cleans every line before it is written at all."""
    log_path = tmp_path / "logs" / "triage.log"
    monkeypatch.setattr(ops, "log_file", lambda data_dir: log_path)
    email, url = "student@example.com", "https://hc-ping.com/0123abcd-secret-uuid"
    ops.setup_logging(cfg.root / cfg.data_dir, verbose=True, secrets=[email, url, "hunter2"])
    try:
        log = logging.getLogger("triage.test")
        log.info("logged in as %s with %s", email, "hunter2")
        log.warning("heartbeat %s failed", url)
        log.error("header was GoogleLogin auth=abc123token and key sk-ant-xyz")
        try:
            raise RuntimeError(f"boom {email}")
        except RuntimeError:
            log.exception("with traceback")
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = log_path.read_text(encoding="utf-8")
        assert "[redacted]" in text
        for secret in (email, "hunter2", "0123abcd-secret-uuid", "abc123token", "sk-ant-xyz"):
            assert secret not in text
        seed_scored(conn, cfg, [dict(n=1, score=90)])
        live = progress.write_live(cfg, conn)
        assert live is not None
        data = json.loads(live.read_text(encoding="utf-8"))
        assert "log" not in data
        page = dashboard.render(cfg, conn).read_text(encoding="utf-8")
        for secret in (email, "0123abcd-secret-uuid", "abc123token"):
            assert secret not in live.read_text(encoding="utf-8") and secret not in page
        assert "p-log" not in page
    finally:
        logging.getLogger().handlers.clear()


def test_setup_logging_rotates_and_skips_stderr_when_not_a_terminal(cfg, tmp_path, monkeypatch):
    from logging.handlers import RotatingFileHandler

    log_path = tmp_path / "logs" / "triage.log"
    monkeypatch.setattr(ops, "log_file", lambda data_dir: log_path)
    monkeypatch.setattr(ops, "_stderr_is_a_terminal", lambda: False)
    ops.setup_logging(cfg.root / cfg.data_dir)
    try:
        handlers = logging.getLogger().handlers
        assert len(handlers) == 1 and isinstance(handlers[0], RotatingFileHandler)
        assert handlers[0].maxBytes == 5_000_000 and handlers[0].backupCount == 3
        ops.setup_logging(cfg.root / cfg.data_dir, verbose=True)
        assert len(logging.getLogger().handlers) == 2  # -v always shows the log in the terminal
    finally:
        logging.getLogger().handlers.clear()
