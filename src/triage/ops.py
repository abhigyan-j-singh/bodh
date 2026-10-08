"""Operational plumbing: the dead-man's switch, a run lock, a run timeout, logging."""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class RunTimeout(Exception):
    pass


class AlreadyRunning(Exception):
    pass


def log_file(data_dir: Path) -> Path:
    """Where the run log lives: ~/Library/Logs/triage on a Mac, data/logs elsewhere."""
    log_dir = Path(os.path.expanduser("~/Library/Logs/triage"))
    if not log_dir.parent.exists():
        log_dir = data_dir / "logs"
    return log_dir / "triage.log"


def setup_logging(data_dir: Path, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        path = log_file(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


def run_in_progress(data_dir: Path) -> bool:
    """True while another process holds the run lock."""
    path = data_dir / "triage.lock"
    if not path.exists():
        return False
    try:
        with open(path) as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


@contextmanager
def run_lock(data_dir: Path, kind: str = "run") -> Iterator[None]:
    """One run at a time. launchd fires every 30 minutes regardless; a slow run must not overlap.
    While held, data/triage.pid names the process, so `triage stop` and the panel can end it."""
    path = data_dir / "triage.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    pid_path = data_dir / "triage.pid"
    with open(path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AlreadyRunning("another triage run holds the lock") from None
        try:
            pid_path.write_text(f"{os.getpid()} {kind} {time.time():.0f}\n", encoding="utf-8")
        except OSError:
            pass
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            try:
                pid_path.unlink()
            except OSError:
                pass


def running_process(data_dir: Path) -> dict | None:
    """The process holding the run lock, from the pid file: {pid, kind, started} or None."""
    pid_path = data_dir / "triage.pid"
    try:
        pid_s, kind, started = pid_path.read_text(encoding="utf-8").split()
        pid = int(pid_s)
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    if not run_in_progress(data_dir):
        return None
    return {"pid": pid, "kind": kind, "started": float(started)}


def stop_process(data_dir: Path, wait: float = 20.0) -> str:
    """Interrupt the running triage process (Ctrl-C), then insist. The run fails open:
    what was judged stays, nothing half-cut is left behind the next run cannot finish."""
    proc = running_process(data_dir)
    if proc is None:
        return "nothing is running"
    pid = proc["pid"]
    try:
        os.kill(pid, signal.SIGINT)
    except OSError as exc:
        return f"could not signal {pid}: {exc}"
    deadline = time.time() + wait
    while time.time() < deadline:
        if running_process(data_dir) is None:
            return f"stopped {proc['kind']} (pid {pid})"
        time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    return f"sent SIGTERM to {proc['kind']} (pid {pid}) after {wait:.0f}s without a clean stop"


@contextmanager
def time_limit(seconds: int) -> Iterator[None]:
    """A hard wall clock limit for a run; the next run picks up whatever is left."""

    def _alarm(signum, frame):  # noqa: ANN001
        raise RunTimeout(f"run exceeded {seconds}s")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


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
        except Exception as exc:  # noqa: BLE001
            log.warning("heartbeat: could not reach %s%s (%s)", self.url, suffix, exc)

    def ok(self, summary: str) -> None:
        self._post("", summary)

    def fail(self, error: str) -> None:
        self._post("/fail", error)

    def start(self) -> None:
        self._post("/start", "")
