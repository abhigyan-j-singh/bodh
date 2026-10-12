"""`bodh serve`: the schedule and the console in one process, for Docker, Windows, and any
machine without launchd or systemd. A run every `every` minutes, the weekly rewrite on Sunday
mornings, the report on the first of the month, each as a child `bodh` process (a crash in one
cannot take the server down), and the console answering on its port in a thread. Ctrl-C stops it;
what a run had done stays, as always.

`bodh serve --app` is the same process as the Mac app's engine: a free port on 127.0.0.1, a
per-launch token read from a file the app wrote, the port announced as JSON on the first line
of stdout and in data/serve.json, the console serving the app's pages and the service's routes,
and an exit when stdin closes (the app holds the pipe, so a dead app never leaves an orphan)
or on SIGTERM. A pause (`POST /api/pause`, kv `serve.paused_until`) holds every job until it
ends or is lifted."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import store
from .config import Config

log = logging.getLogger(__name__)

WEEKLY_HOUR = 7  # Sunday, like the launchd job
REPORT_DAY, REPORT_HOUR = 1, 7
TICK_SECONDS = 30
LAST_RUN_KEY = "serve.last_run_at"
LAST_WEEKLY_KEY = "serve.last_weekly"  # the ISO week it ran in
LAST_REPORT_KEY = "serve.last_report"  # the month it ran in

Spawn = Callable[[list[str]], int]


def run_child(args: list[str]) -> int:
    """One bodh command as a child, waited for; its exit code. When the server is stopped
    while the child runs (Ctrl-C, SIGTERM, the app quitting), the child gets Ctrl-C too and
    twenty seconds to finish cleanly, then SIGTERM; the run fails open either way."""
    import signal

    try:
        proc = subprocess.Popen([sys.executable, "-m", "bodh.cli", *args])
    except OSError as exc:
        log.error("serve: could not start bodh %s: %s", " ".join(args), exc)
        return 1
    try:
        return proc.wait()
    except KeyboardInterrupt:
        with contextlib.suppress(OSError):
            proc.send_signal(getattr(signal, "SIGINT", signal.SIGTERM))
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        raise


class Scheduler:
    """What is due, decided from the clock and three kv marks; `tick` is the whole logic, so a
    test drives it with a clock of its own. A pause (`serve.paused_until`) holds everything."""

    def __init__(self, cfg: Config, conn: Any, every_minutes: int, spawn: Spawn = run_child) -> None:
        self.cfg = cfg
        self.conn = conn
        self.every = max(1, every_minutes) * 60
        self.spawn = spawn
        self.tz = ZoneInfo(cfg.timezone)

    def paused(self) -> bool:
        from .service import paused_until

        return paused_until(self.conn) is not None

    def due(self, now: float) -> list[str]:
        """The jobs due at `now` (epoch seconds), in the order they run; none while paused."""
        if self.paused():
            return []
        jobs: list[str] = []
        local = datetime.fromtimestamp(now, self.tz)
        last_run = float(store.kv_get(self.conn, LAST_RUN_KEY) or 0)
        if now - last_run >= self.every:
            jobs.append("run")
        week = f"{local.isocalendar()[0]}-W{local.isocalendar()[1]:02d}"
        if (
            local.weekday() == 6
            and local.hour >= WEEKLY_HOUR
            and store.kv_get(self.conn, LAST_WEEKLY_KEY) != week
        ):
            jobs.append("weekly")
        month = local.strftime("%Y-%m")
        if (
            local.day >= REPORT_DAY
            and local.hour >= REPORT_HOUR
            and store.kv_get(self.conn, LAST_REPORT_KEY) != month
        ):
            jobs.append("report")
        return jobs

    def tick(self, now: float | None = None) -> list[tuple[str, int]]:
        """Run what is due; returns (job, exit code) per job started."""
        now = time.time() if now is None else now
        local = datetime.fromtimestamp(now, self.tz)
        done: list[tuple[str, int]] = []
        for job in self.due(now):
            if job == "run":
                store.kv_set(
                    self.conn, LAST_RUN_KEY, str(now)
                )  # before, so a hung run is not retried every tick
            elif job == "weekly":
                store.kv_set(
                    self.conn, LAST_WEEKLY_KEY, f"{local.isocalendar()[0]}-W{local.isocalendar()[1]:02d}"
                )
            else:
                store.kv_set(self.conn, LAST_REPORT_KEY, local.strftime("%Y-%m"))
            self.conn.commit()
            log.info("serve: starting bodh %s", job)
            code = self.spawn([job])
            log.info("serve: bodh %s exited %d", job, code)
            done.append((job, code))
        return done

    def loop(self, stop: threading.Event, tick_seconds: float = TICK_SECONDS) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # the loop outlives any one failure
                log.exception("serve: a tick failed")
            stop.wait(tick_seconds)


def announce(cfg: Config, port: int, token_file: Path | None) -> dict[str, Any]:
    """What `--app` tells the app: the port, the pid and the start time, on stdout's first
    line and in data/serve.json (beside console.json and the pid file)."""
    info = {
        "port": port,
        "pid": os.getpid(),
        "started": time.time(),
        "url": f"http://127.0.0.1:{port}",
        "token_file": str(token_file) if token_file else None,
    }
    data_dir = cfg.root / cfg.data_dir
    with contextlib.suppress(OSError):
        (data_dir / "serve.json").write_text(json.dumps(info), encoding="utf-8")
    return info


def watch_stdin(stop: threading.Event) -> threading.Thread:
    """A thread that sets `stop` when stdin closes: the app holds the pipe for the engine's
    life, so the end of the pipe is the end of the app."""

    def _watch() -> None:
        try:
            while True:
                chunk = sys.stdin.buffer.read(1) if hasattr(sys.stdin, "buffer") else sys.stdin.read(1)
                if not chunk:
                    break
        except (OSError, ValueError):
            pass
        log.info("serve: stdin closed; stopping")
        stop.set()

    thread = threading.Thread(target=_watch, name="bodh-stdin", daemon=True)
    thread.start()
    return thread


def serve(
    cfg: Config,
    conn: Any,
    every_minutes: int = 30,
    host: str = "127.0.0.1",
    port: int = 8766,
    console: bool = True,
    stop: threading.Event | None = None,
    spawn: Spawn = run_child,
    tick_seconds: float = TICK_SECONDS,
    token: str | None = None,
    app_mode: bool = False,
    token_file: Path | None = None,
    on_ready: Callable[[dict[str, Any]], None] | None = None,
    exit_on_stdin_close: bool = False,
) -> None:
    """Blocks until `stop` is set, Ctrl-C, SIGTERM, or (with `exit_on_stdin_close`) the end of
    stdin. The console runs in a daemon thread (it ends with the process); the schedule runs
    here. `on_ready` is told the port once the console listens (`--app` prints it)."""
    from .locks import graceful_sigterm

    stop = stop or threading.Event()
    server = None
    scheduler = Scheduler(cfg, store.connect(cfg.db_path), every_minutes, spawn=spawn)
    _record_state(scheduler.conn, every_minutes)  # before the port is announced: the status sees it at once
    if console:
        from .console import Console

        server = Console(cfg, conn, host=host, port=port, token=token, app_mode=app_mode)
        listening = threading.Event()

        def ready(bound: int) -> None:
            info = announce(cfg, bound, token_file)
            if on_ready is not None:
                on_ready(info)
            listening.set()

        thread = threading.Thread(target=server.serve, kwargs={"ready": ready}, name="bodh-console", daemon=True)
        thread.start()
        listening.wait(10)
        log.info("serve: console on http://%s:%d", host, server.port)
    if exit_on_stdin_close:
        watch_stdin(stop)
    try:
        with graceful_sigterm():
            scheduler.loop(stop, tick_seconds)
    except KeyboardInterrupt:
        log.info("serve: stopped")
    finally:
        _clear_state(scheduler.conn)
        if server is not None and server.server is not None:
            with contextlib.suppress(Exception):
                server.server.shutdown()
        with contextlib.suppress(OSError):
            (cfg.root / cfg.data_dir / "serve.json").unlink()


def _record_state(conn: Any, every_minutes: int) -> None:
    from .service import SERVE_KEY

    store.kv_set(
        conn,
        SERVE_KEY,
        json.dumps({"pid": os.getpid(), "every_seconds": every_minutes * 60, "started": time.time()}),
    )
    conn.commit()


def _clear_state(conn: Any) -> None:
    from .service import SERVE_KEY

    with contextlib.suppress(Exception):
        store.kv_delete(conn, SERVE_KEY)
        conn.commit()
