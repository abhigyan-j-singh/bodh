"""`bodh serve`: the schedule and the console in one process, for Docker, Windows, and any
machine without launchd or systemd. A run every `every` minutes, the weekly rewrite on Sunday
mornings, the report on the first of the month, each as a child `bodh` process (a crash in one
cannot take the server down), and the console answering on its port in a thread. Ctrl-C stops it;
what a run had done stays, as always."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
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
    """One bodh command as a child, waited for; its exit code."""
    try:
        return subprocess.run([sys.executable, "-m", "bodh.cli", *args], check=False).returncode
    except OSError as exc:
        log.error("serve: could not start bodh %s: %s", " ".join(args), exc)
        return 1


class Scheduler:
    """What is due, decided from the clock and three kv marks; `tick` is the whole logic, so a
    test drives it with a clock of its own."""

    def __init__(self, cfg: Config, conn: Any, every_minutes: int, spawn: Spawn = run_child) -> None:
        self.cfg = cfg
        self.conn = conn
        self.every = max(1, every_minutes) * 60
        self.spawn = spawn
        self.tz = ZoneInfo(cfg.timezone)

    def due(self, now: float) -> list[str]:
        """The jobs due at `now` (epoch seconds), in the order they run."""
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
) -> None:
    """Blocks until `stop` is set or Ctrl-C. The console runs in a daemon thread (it ends with
    the process); the schedule runs here."""
    stop = stop or threading.Event()
    if console:
        from .console import Console

        server = Console(cfg, conn, host=host, port=port)
        thread = threading.Thread(target=server.serve, name="bodh-console", daemon=True)
        thread.start()
        log.info("serve: console on http://%s:%d", host, port)
    scheduler = Scheduler(cfg, store.connect(cfg.db_path), every_minutes, spawn=spawn)
    try:
        scheduler.loop(stop, tick_seconds)
    except KeyboardInterrupt:
        log.info("serve: stopped")
