"""Locks, logging, the heartbeat: the plumbing under every command."""

from __future__ import annotations

import json
import logging
import sys

import pytest
from conftest import seed_scored

from bodh import dashboard, locks, logs, progress


def test_live_file_never_carries_env_values(conn, cfg, tmp_path, monkeypatch):
    """A log line with a secret in it must not reach progress.json or the dashboard, which
    README says to publish; the redactor cleans every line before it is written at all."""
    log_path = tmp_path / "logs" / "bodh.log"
    monkeypatch.setattr(logs, "log_file", lambda data_dir: log_path)
    email, url = "student@example.com", "https://hc-ping.com/0123abcd-secret-uuid"
    logs.setup_logging(cfg.root / cfg.data_dir, verbose=True, secrets=[email, url, "hunter2"])
    try:
        log = logging.getLogger("bodh.test")
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
        assert 'id="log"' not in page  # the export carries no log
    finally:
        _reset_logging()


def _reset_logging() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)


def test_setup_logging_rotates_and_skips_stderr_when_not_a_terminal(cfg, tmp_path, monkeypatch):
    from logging.handlers import RotatingFileHandler

    log_path = tmp_path / "logs" / "bodh.log"
    monkeypatch.setattr(logs, "log_file", lambda data_dir: log_path)
    monkeypatch.setattr(logs, "_stderr_is_a_terminal", lambda: False)
    logs.setup_logging(cfg.root / cfg.data_dir)
    try:
        handlers = logging.getLogger().handlers
        assert len(handlers) == 1 and isinstance(handlers[0], RotatingFileHandler)
        assert handlers[0].maxBytes == 5_000_000 and handlers[0].backupCount == 3
        old = list(logging.getLogger().handlers)
        logs.setup_logging(cfg.root / cfg.data_dir, verbose=True)
        for handler in old:
            handler.close()
        assert len(logging.getLogger().handlers) == 2  # -v always shows the log in the terminal
    finally:
        _reset_logging()


def test_run_lock_is_not_refused_by_status_probes(cfg):
    """The control room probes twice every 3 s; a probe that took the lock for an instant
    refused a launchd run its lock now and then (2,035 of 20,000 attempts in the audit's probe)."""
    import threading

    data_dir = cfg.root / cfg.data_dir
    stop = threading.Event()
    probes = {"n": 0, "true": 0}

    def probe():
        while not stop.is_set():
            probes["n"] += 1
            probes["true"] += locks.run_in_progress(data_dir)

    t = threading.Thread(target=probe)
    t.start()
    try:
        refused = 0
        for _ in range(300):
            try:
                with locks.run_lock(data_dir, "run"):
                    pass
            except locks.AlreadyRunning:
                refused += 1
    finally:
        stop.set()
        t.join()
    assert refused == 0 and probes["n"] > 0


def test_pid_file_and_liveness(cfg, monkeypatch):
    data_dir = cfg.root / cfg.data_dir
    pid_path = data_dir / "bodh.pid"
    with locks.run_lock(data_dir, "blast"):
        pid, kind, started = pid_path.read_text(encoding="utf-8").split()
        assert int(pid) == __import__("os").getpid() and kind == "blast"
        assert locks.run_in_progress(data_dir) and locks.running_process(data_dir)["kind"] == "blast"
        assert (data_dir / "bodh.lock").stat().st_size == 0  # never truncated, never written
        with __import__("pytest").raises(locks.AlreadyRunning), locks.run_lock(data_dir, "run"):
            pass
    assert not pid_path.exists() and not locks.run_in_progress(data_dir)
    # a stale pid file (a crash) names a process that no longer exists: not running
    pid_path.write_text("999999999 run 1700000000\n", encoding="utf-8")
    assert not locks.run_in_progress(data_dir) and locks.running_process(data_dir) is None
    # a reused pid (alive, but not holding the lock): not running either
    pid_path.write_text(f"{__import__('os').getpid()} run 1700000000\n", encoding="utf-8")
    assert not locks.run_in_progress(data_dir)


def test_running_process_never_signals_on_windows(monkeypatch):
    import os

    calls = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(locks, "_pid_alive_windows", lambda pid: pid == 4242)
    assert locks.pid_alive(4242) and not locks.pid_alive(4243) and calls == []


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX signals; Windows stops a run with a hard kill until Phase 9"
)
def test_sigterm_ends_a_run_like_ctrl_c(cfg):
    """launchctl bootout sends SIGTERM; it used to kill the run with no run row finished and
    the pid file left behind."""
    import signal
    import subprocess
    import time
    from pathlib import Path

    data_dir = cfg.root / cfg.data_dir
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        f"import sys, time; sys.path.insert(0, {src!r}); from pathlib import Path\n"
        "from bodh.locks import run_lock, graceful_sigterm\n"
        f"with run_lock(Path({str(data_dir)!r}), 'run'), graceful_sigterm():\n"
        "    print('locked', flush=True)\n"
        "    try:\n        time.sleep(30)\n    except KeyboardInterrupt:\n        print('interrupted', flush=True)\n"
    )
    with subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True) as proc:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "locked"
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        out = proc.stdout.read()
        proc.wait(timeout=10)
    assert "interrupted" in out and proc.returncode == 0
    assert not (data_dir / "bodh.pid").exists()


def test_time_limit_without_sigalrm_is_a_noop(monkeypatch):
    import signal

    monkeypatch.delattr(signal, "SIGALRM", raising=False)
    with locks.time_limit(1):
        pass


def test_redactor_cleans_tracebacks_with_a_single_handler(cfg, tmp_path, monkeypatch):
    """With the file handler alone (launchd: no terminal), the filter runs before the formatter
    caches the traceback; the exception line used to reach the file unredacted."""
    log_path = tmp_path / "logs" / "bodh.log"
    monkeypatch.setattr(logs, "log_file", lambda data_dir: log_path)
    monkeypatch.setattr(logs, "_stderr_is_a_terminal", lambda: False)
    logs.setup_logging(cfg.root / cfg.data_dir, secrets=["https://hc-ping.com/uuid-secret"])
    try:
        assert len(logging.getLogger().handlers) == 1
        try:
            raise RuntimeError("ping https://hc-ping.com/uuid-secret failed, header GoogleLogin auth=tok123")
        except RuntimeError:
            logging.getLogger("bodh.test").exception("boom")
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = log_path.read_text(encoding="utf-8")
        assert "RuntimeError" in text and "uuid-secret" not in text and "tok123" not in text
    finally:
        _reset_logging()
