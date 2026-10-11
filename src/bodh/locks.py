"""The run lock, the pid file, the run timeout and the signals: one run at a time, stoppable
from another terminal or the console, and never left half-done."""

from __future__ import annotations

import logging
import os
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from .errors import BodhError, LockedError

try:
    import fcntl

    HAVE_FLOCK = True
except ImportError:  # Windows: no flock; the lock below falls back to an exclusive file
    HAVE_FLOCK = False

log = logging.getLogger(__name__)

LOCK_RETRY_SECONDS = 0.1  # a status probe may hold the lock for an instant; a run waits this long for it


class RunTimeout(BodhError):
    """The run passed its hard limit; the next one picks up what is left."""


AlreadyRunning = LockedError  # the name before October 2026


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
    if not HAVE_FLOCK:
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
    lock is touched, so the console's polling cannot refuse a launchd run its lock);
    the lock itself is probed only when the pid file names a live process, to tell a reused
    pid from a run."""
    rec = _pid_record(data_dir)
    if rec is None or not pid_alive(rec[0]):
        return False
    return _lock_held_by_someone(data_dir / "bodh.lock")


@contextmanager
def run_lock(data_dir: Path, kind: str = "run") -> Iterator[None]:
    """One run at a time. launchd fires every 30 minutes regardless; a slow run must not overlap.
    While held, data/bodh.pid names the process, so `bodh stop` and the console can end it,
    and `bodh progress` can say how long it has been running."""
    path = data_dir / "bodh.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    pid_path = data_dir / "bodh.pid"
    if not HAVE_FLOCK:
        with _exclusive_file_lock(path), _pid_file(pid_path, kind):
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
                    raise AlreadyRunning("another run is in progress; `bodh stop` it or wait") from None
                time.sleep(0.01)
        try:
            with _pid_file(pid_path, kind):
                yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def _pid_file(pid_path: Path, kind: str) -> Iterator[None]:
    with suppress(OSError):
        pid_path.write_text(f"{os.getpid()} {kind} {time.time():.0f}\n", encoding="utf-8")
    try:
        yield
    finally:
        with suppress(OSError):
            pid_path.unlink()


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
                with suppress(OSError):
                    path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise AlreadyRunning("another run is in progress; `bodh stop` it or wait") from None
            time.sleep(0.01)
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
    finally:
        os.close(fd)
    try:
        yield
    finally:
        with suppress(OSError):
            path.unlink()


def running_process(data_dir: Path) -> dict[str, Any] | None:
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
    with suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    return f"sent SIGTERM to {proc['kind']} (pid {pid}) after {wait:.0f}s without a clean stop"


@contextmanager
def time_limit(seconds: int) -> Iterator[None]:
    """A hard wall clock limit for a run, the backstop behind the run's own deadline; the
    next run picks up whatever is left. Where there is no SIGALRM (Windows) the deadline
    alone bounds the run."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _alarm(signum: int, frame: Any) -> None:
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
    """SIGTERM (launchctl bootout, `kill`, the console's Stop after 25 s) ends a run the way
    Ctrl-C does: the run row is finished, the pid file removed, nothing half-cut is left that
    the next run cannot finish. Only the main thread may install handlers; elsewhere this is
    a no-op."""
    import threading

    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGTERM"):
        yield
        return

    def _term(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, _term)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
