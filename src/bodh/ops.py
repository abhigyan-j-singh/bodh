"""Operational plumbing: the dead-man's switch, a run lock, a run timeout, logging."""

from __future__ import annotations

import logging
import os
import re
import signal
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx

try:
    import fcntl
except ImportError:  # Windows: no flock; the lock below falls back to an exclusive file
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

LOCK_RETRY_SECONDS = 0.1  # a status probe may hold the lock for an instant; a run waits this long for it


def _host_only(url: str) -> str:
    """A ping URL without its secret path (the check's UUID), for log lines."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}/…"
    except ValueError:
        return "<heartbeat url>"


class RunTimeout(Exception):
    pass


class AlreadyRunning(Exception):
    pass


def log_file(data_dir: Path) -> Path:
    """Where the run log lives: ~/Library/Logs/bodh on a Mac, data/logs elsewhere."""
    logs = Path(os.path.expanduser("~/Library/Logs"))
    if not logs.exists():
        return data_dir / "logs" / "bodh.log"
    return logs / "bodh" / "bodh.log"


class Redactor(logging.Filter):
    """Every secret value, and any auth token, is replaced before a line is written anywhere:
    the log file's tail reaches the control room, and a traceback or an SDK error can carry
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


def pid_alive(pid: int) -> bool:
    """Is there a process with this id? Never a signal on Windows, where os.kill(pid, 0) would
    terminate the process it was meant to probe."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == 259  # STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(handle)


def _pid_record(data_dir: Path) -> tuple[int, str, float] | None:
    try:
        pid_s, kind, started = (data_dir / "bodh.pid").read_text(encoding="utf-8").split()
        return int(pid_s), kind, float(started)
    except (OSError, ValueError):
        return None


def _lock_held_by_someone(path: Path) -> bool:
    """A non-blocking probe of the lock file: taken and released in the same instant. Used only
    when the pid file names a live process, so a refused run_lock cannot be our own probe."""
    if fcntl is None:
        return path.exists()
    try:
        with open(path, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def run_in_progress(data_dir: Path) -> bool:
    """True while another process holds the run lock. Decided from the pid file first (no
    lock is touched, so the control room's polling cannot refuse a launchd run its lock);
    the lock itself is probed only when the pid file names a live process, to tell a reused
    pid from a run."""
    rec = _pid_record(data_dir)
    if rec is None or not pid_alive(rec[0]):
        return False
    return _lock_held_by_someone(data_dir / "bodh.lock")


@contextmanager
def run_lock(data_dir: Path, kind: str = "run") -> Iterator[None]:
    """One run at a time. launchd fires every 30 minutes regardless; a slow run must not overlap.
    While held, data/bodh.pid names the process, so `bodh stop` and the panel can end it,
    and `bodh progress` can say how long it has been running."""
    path = data_dir / "bodh.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    pid_path = data_dir / "bodh.pid"
    if fcntl is None:
        with _exclusive_file_lock(path):
            with _pid_file(pid_path, kind):
                yield
        return
    with open(path, "a") as fh:  # never truncated: a refused attempt leaves the file as it was
        deadline = time.monotonic() + LOCK_RETRY_SECONDS
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AlreadyRunning("another bodh run holds the lock") from None
                time.sleep(0.01)
        try:
            with _pid_file(pid_path, kind):
                yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def _pid_file(pid_path: Path, kind: str) -> Iterator[None]:
    try:
        pid_path.write_text(f"{os.getpid()} {kind} {time.time():.0f}\n", encoding="utf-8")
    except OSError:
        pass
    try:
        yield
    finally:
        try:
            pid_path.unlink()
        except OSError:
            pass


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Without flock (Windows): the lock file is created exclusively and holds the pid; a file
    left by a process that no longer exists is stale and taken over."""
    deadline = time.monotonic() + LOCK_RETRY_SECONDS
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                holder = int(path.read_text(encoding="utf-8").split()[0])
            except (OSError, ValueError, IndexError):
                holder = 0
            if holder and not pid_alive(holder):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise AlreadyRunning("another bodh run holds the lock") from None
            time.sleep(0.01)
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
    finally:
        os.close(fd)
    try:
        yield
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def running_process(data_dir: Path) -> dict | None:
    """The process holding the run lock, from the pid file: {pid, kind, started} or None."""
    rec = _pid_record(data_dir)
    if rec is None or not run_in_progress(data_dir):
        return None
    pid, kind, started = rec
    return {"pid": pid, "kind": kind, "started": started}


def stop_process(data_dir: Path, wait: float = 20.0) -> str:
    """Interrupt the running bodh process (Ctrl-C), then insist. The run fails open:
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
    """A hard wall clock limit for a run, the backstop behind the run's own deadline; the
    next run picks up whatever is left. Where there is no SIGALRM (Windows) the deadline
    alone bounds the run."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _alarm(signum, frame):  # noqa: ANN001
        raise RunTimeout(f"run exceeded {seconds}s")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@contextmanager
def graceful_sigterm() -> Iterator[None]:
    """SIGTERM (launchctl bootout, `kill`, the panel's Stop after 25 s) ends a run the way
    Ctrl-C does: the run row is finished, the pid file removed, nothing half-cut is left that
    the next run cannot finish. Only the main thread may install handlers; elsewhere this is
    a no-op."""
    import threading

    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGTERM"):
        yield
        return

    def _term(signum, frame):  # noqa: ANN001
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, _term)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


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
            log.warning("heartbeat: could not reach %s (%s)", _host_only(self.url) + suffix, exc)

    def ok(self, summary: str) -> None:
        self._post("", summary)

    def fail(self, error: str) -> None:
        self._post("/fail", error)

    def start(self) -> None:
        self._post("/start", "")
