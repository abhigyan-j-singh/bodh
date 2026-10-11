"""The console: the dashboard page with the levers, served on the Mac (`bodh console`, kept
alive by launchd) and reachable from the phone over Tailscale. The page is the document
rendered by `web.panels`; `/api/events` streams ticks (the state and today's tiles every two
seconds while a run is in progress, a heartbeat between) and the page refetches itself when
the console's generation moves. Actions start the same commands you would type (`bodh run`,
`prune`, `undo`, `catch-up` …) as child processes and stream their output; a verdict is
recorded in process through the API; Stop interrupts whatever run holds the lock. No
framework, one file."""

from __future__ import annotations

import contextlib
import json
import logging
import mimetypes
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import jobs, locks, logs, progress
from .config import Config, load_config
from .errors import BodhError
from .web import document, panels

log = logging.getLogger(__name__)

ACTIONS = {
    # action: (arguments after `bodh`, runs in the background?)
    "run": (["run"], True),
    "run_shadow": (["run", "--shadow"], True),
    "run_sync": (["run", "--sync"], True),
    "prune_dry": (["prune", "--dry-run"], False),
    "prune": (["prune", "--yes"], True),
    "undo": (["undo"], False),
    "clear_old_dry": (["clear-old", "--dry-run"], False),
    "clear_old": (["clear-old", "--yes"], True),
    "catch_up": (["catch-up", "--yes"], True),
    "weekly": (["weekly"], True),
    "report": (["report"], False),
    "dashboard": (["dashboard"], False),
    "doctor": (["doctor"], False),
}


class Child:
    """One background command at a time, its output kept for the page."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.args: list[str] = []
        self.shown: list[str] = []  # the arguments after `bodh`, as the page names the command
        self.started = 0.0
        self.ended = 0.0
        self.code: int | None = None
        self.output: deque[str] = deque(maxlen=400)
        self.lock = threading.Lock()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list[str], cwd: Path, env: dict[str, Any], shown: list[str] | None = None) -> None:
        with self.lock:
            if self.alive():
                raise RuntimeError("a command is already running")
            self.proc = subprocess.Popen(
                args,
                cwd=str(cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self.args, self.started, self.ended, self.code = args, time.time(), 0.0, None
            self.shown = list(shown) if shown is not None else args[1:]
            self.output.clear()
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                for piece in raw.replace("\r", "\n").split("\n"):
                    if piece.strip():
                        self.output.append(piece.rstrip()[:300])
        finally:
            proc.stdout.close()  # one descriptor per command would otherwise stay open for the console's life
        proc.wait()
        with self.lock:
            self.code, self.ended = proc.returncode, time.time()

    def stop(self) -> str:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return "no command is running here"
        proc.send_signal(2)  # SIGINT: the same as Ctrl-C in the terminal
        try:
            proc.wait(timeout=25)
            return "stopped"
        except subprocess.TimeoutExpired:
            proc.terminate()
            return "sent SIGTERM after 25s"

    def state(self) -> dict[str, Any]:
        alive = self.alive()
        return {
            "running": alive,
            "args": list(self.shown),
            "started": self.started,
            "elapsed": (time.time() - self.started)
            if alive
            else (self.ended - self.started if self.started else 0),
            "code": self.code,
            "output": list(self.output),
        }


STATUS_CACHE_SECONDS = 2.0  # `/api/status`, for scripts and the Shortcut; several callers share one query
JOBS_CACHE_SECONDS = 10.0  # four `launchctl print` subprocesses per status were the dearest part
TICK_CACHE_SECONDS = 1.0  # several pages open (the Mac, the phone) share one tick
TICK_RUNNING_S = 2.0  # progress every two seconds while a run is in progress
TICK_IDLE_S = 5.0  # between runs the tick only watches for a change
HEARTBEAT_S = 15.0  # the page counts a heartbeat as contact; two missed ones mark it stale


class Console:
    def __init__(
        self,
        cfg: Config,
        conn: sqlite3.Connection,
        command: list[str] | None = None,
        host: str = "127.0.0.1",
        port: int = 8766,
    ) -> None:
        self.cfg = cfg
        self.conn = conn
        self.command = command or [sys.executable, "-m", "bodh.cli"]
        self.host, self.port = host, port
        self.child = Child()
        self.db_lock = threading.Lock()
        self.env = {**os.environ, "BODH_HOME": str(cfg.root), "PYTHONUNBUFFERED": "1"}
        self._status_cache: tuple[float, dict[str, Any]] | None = None
        self._jobs_cache: tuple[float, dict[str, Any]] | None = None
        self._config_mtime: float | None = None
        self.cache = document.Cache()
        self.generation = 1  # moves when the page must refetch: a run, an action, a verdict, a reload
        self._tick_cache: tuple[float, dict[str, Any]] | None = None
        self._seen: dict[str, Any] = {}  # what the last tick saw: running, the attention, the child
        self.changed = threading.Condition()  # wakes the event streams when the generation moves

    # -- the page and its document ---------------------------------------------
    def page(self, mode: str = "console", theme: str | None = None) -> str:
        self._reload_config()
        with self.db_lock:
            doc = self.cache.get(self.cfg, self.conn, mode)
        return panels.render(doc, mode, theme, generation=self.generation)

    def document_json(self, mode: str = "console") -> str:
        self._reload_config()
        with self.db_lock:
            return self.cache.get(self.cfg, self.conn, mode).to_json()

    def list_rows(self, band: int, offset: int) -> dict[str, Any]:
        """One more page of the list, rendered as the page renders rows."""
        self._reload_config()
        with self.db_lock:
            rows, total = document.list_rows(self.cfg, self.conn, document.local_now(self.cfg), band, offset)
        verdicts = (*document.GRADES, "basic", "heavy", "more", "less")
        return {
            "rows": panels.list_rows_html(rows, verdicts),
            "total": total,
            "offset": offset,
            "count": len(rows),
        }

    def bump(self) -> None:
        """The page must refetch: the next tick says so, at once, and the document is rebuilt."""
        self.generation += 1
        self.cache.invalidate()
        self._tick_cache = None
        with self.changed:
            self.changed.notify_all()

    def tick(self) -> dict[str, Any]:
        """The live object the events stream and `/api/tick` carry: the generation, the state,
        today's tiles while a run is in progress, the child's output. A change the console did
        not make itself (a run starting or ending, the attention changing) is noticed here."""
        self._reload_config()
        now = time.monotonic()
        with self.db_lock:
            if self._tick_cache is not None and now - self._tick_cache[0] < TICK_CACHE_SECONDS:
                return self._tick_cache[1]
            live = document.live(self.cfg, self.conn)
            self._notice(live)
            out: dict[str, Any] = {"schema": 1, "generation": self.generation, **live}
            child = self.child.state()
            if child["args"]:
                out["child"] = {**child, "output": child["output"][-80:]}
            self._tick_cache = (now, out)
            return out

    def _notice(self, live: dict[str, Any]) -> None:
        state = live["state"]
        attention = state["attention"]["key"] if state.get("attention") else None
        seen = {"running": state["running"], "attention": attention, "child_ended": self.child.ended}
        if self._seen and seen != self._seen:
            self.generation += 1
            self.cache.invalidate()
        self._seen = seen

    # -- data -----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        self._reload_config()
        now = time.monotonic()
        with self.db_lock:
            if self._status_cache is None or now - self._status_cache[0] > STATUS_CACHE_SECONDS:
                st = jobs.status(self.cfg, self.conn, jobs=self._jobs())
                self._status_cache = (now, st)
            st = dict(self._status_cache[1])
        st["child"] = self.child.state()
        return st

    def _jobs(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._jobs_cache is None or now - self._jobs_cache[0] > JOBS_CACHE_SECONDS:
            self._jobs_cache = (now, {name: jobs.job_state(label) for name, label in jobs.JOBS.items()})
        return self._jobs_cache[1]

    def forget(self) -> None:
        """After an action: the next status, tick and page are fresh."""
        self._status_cache = None
        self._jobs_cache = None
        self.bump()

    def host_allowed(self, host_header: str | None) -> bool:
        """The page is served on 127.0.0.1 and reached from the phone through tailscale serve;
        a request whose Host is anything else comes from a page that rebound a DNS name to
        this machine, and gets nothing (the X-Bodh header alone does not stop that)."""
        host = (host_header or "").strip().lower()
        if host.startswith("["):
            host = host.split("]", 1)[0] + "]"
        else:
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return host in {"127.0.0.1", "localhost", "[::1]", "::1", self.host.lower()} or host.endswith(
            ".ts.net"
        )

    def _reload_config(self) -> None:
        """config.yaml edited by hand (or by `bodh autocut` in a terminal) shows on the next
        refresh; a file that does not parse leaves the last good configuration in place."""
        path = self.cfg.root / "config.yaml"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime == getattr(self, "_config_mtime", None):
            return
        try:
            self.cfg = load_config(self.cfg.root)
            self._config_mtime = mtime
            self.forget()
        except Exception as exc:  # a half-saved or broken config.yaml: the page keeps the last good one
            log.warning("console: config.yaml not reloaded: %s", exc)

    def run_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """One button of the page: the levers the console works itself (stop, the schedule,
        autocut) or a bodh command run as a child process."""
        if action == "stop":
            msg = (
                self.child.stop()
                if self.child.alive()
                else locks.stop_process(self.cfg.root / self.cfg.data_dir)
            )
            return {"ok": True, "message": msg}
        if action in ("schedule_pause", "schedule_resume", "schedule_kick"):
            msg = jobs.schedule(
                action.split("_", 1)[1], params.get("job", "run"), self.cfg.root / self.cfg.data_dir
            )
            return {"ok": not msg.startswith("not kicked"), "message": msg}
        if action in (
            "autocut_on",
            "autocut_off",
            "enforce_on",
            "enforce_off",
        ):  # enforce_*: the names before October 2026
            # the file is the truth: the page shows what the next run will read, never a wish
            ok, msg = jobs.set_autocut(self.cfg.root, action.endswith("_on"))
            if ok:
                self._config_mtime = None
                self._reload_config()
            return {"ok": ok, "message": msg}
        if action not in ACTIONS:
            return {"ok": False, "message": f"unknown action {action}"}
        args, background = ACTIONS[action]
        args = list(args) + _extra_args(action, params)
        return self._start_command(args) if background else self._run_command(args)

    def mark(self, article_id: str, verdict: str) -> dict[str, Any]:
        """A verdict from the page, recorded in process the way `bodh mark` records it."""
        from .api import App

        with self.db_lock:
            app = App(cfg=self.cfg, conn=self.conn, log_to_file=False)
            try:
                message = app.mark(article_id, verdict)
            except BodhError as exc:
                return {"ok": False, "message": str(exc)}
        self.bump()
        return {"ok": True, "message": message}

    def plan(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """The confirmation for an action that writes to BazQux: the numbers, the mode, the
        reversibility and the cost of time, from the API; the button names the action."""
        from .api import App

        try:
            with self.db_lock:
                app = App(cfg=self.cfg, conn=self.conn, log_to_file=False)
                sentence, label = _plan_words(app, self.cfg, action, params)
        except BodhError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "sentence": sentence, "button": label}

    def _start_command(self, args: list[str]) -> dict[str, Any]:
        """A long command (a run, a catch-up) as a child whose output streams into the page."""
        if self.child.alive():
            return {"ok": False, "message": "a command is already running; stop it first"}
        if locks.running_process(self.cfg.root / self.cfg.data_dir):
            return {"ok": False, "message": "a run holds the lock (started elsewhere); stop it or wait"}
        self.child.start(self.command + args, self.cfg.root, self.env, shown=args)
        return {"ok": True, "message": f"started: bodh {' '.join(args)}"}

    def _run_command(self, args: list[str]) -> dict[str, Any]:
        """A short command, waited for; its output goes back with the answer."""
        try:
            res = subprocess.run(
                self.command + args,
                cwd=str(self.cfg.root),
                env=self.env,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "message": f"bodh {' '.join(args)} took longer than 10 minutes"}
        out = (res.stdout + ("\n" + res.stderr if res.stderr.strip() else "")).strip()
        return {
            "ok": res.returncode == 0,
            "message": f"bodh {' '.join(args)} → exit {res.returncode}",
            "output": out[-6000:],
        }

    # -- server ---------------------------------------------------------------

    def serve(self) -> None:
        """Listen until stopped; console.json beside the data says where, for `bodh status`."""
        data_dir = self.cfg.root / self.cfg.data_dir
        try:
            server = ThreadingHTTPServer((self.host, self.port), _handler_for(self))
        except OSError as exc:
            console_log(data_dir, f"cannot listen on {self.host}:{self.port}: {exc}")
            raise
        server.daemon_threads = True
        self.url = f"http://{self.host}:{self.port}"
        info = data_dir / "console.json"
        with contextlib.suppress(OSError):
            info.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "host": self.host,
                        "port": self.port,
                        "url": self.url,
                        "started": time.time(),
                    }
                ),
                encoding="utf-8",
            )
        console_log(data_dir, f"serving on {self.url} (pid {os.getpid()})")
        log.info("console: serving on %s", self.url)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            console_log(data_dir, "stopped")
            with contextlib.suppress(OSError):
                info.unlink()


def _handler_for(console: Console) -> type[_Handler]:
    """The request handler bound to one console (a subclass, so the server can build one per
    request without a closure)."""
    return type("Handler", (_Handler,), {"console": console})


class _Handler(BaseHTTPRequestHandler):
    """The page, the status and log APIs, the dashboard's files, and the actions (POST, from our
    own page only). `console` is set on the subclass `_handler_for` makes."""

    console: Console

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("console: " + fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        console = self.console
        if not console.host_allowed(self.headers.get("Host")):
            self._send(403, b'{"error":"forbidden: unexpected Host"}')
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            mode = "app" if query.get("mode", [""])[0] == "app" else "console"
            self._send(
                200,
                console.page(mode, query.get("theme", [None])[0]).encode("utf-8"),
                "text/html; charset=utf-8",
            )
        elif url.path == "/design":
            from .web.templates import design_page

            self._send(200, design_page().encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/api/dashboard":
            mode = "export" if query.get("mode", [""])[0] == "export" else "console"
            self._send(200, console.document_json(mode).encode("utf-8"))
        elif url.path == "/api/tick":
            self._send(200, json.dumps(console.tick(), default=str).encode("utf-8"))
        elif url.path == "/api/events":
            self._events()
        elif url.path == "/list":
            band = int(query.get("band", [str(document.LIST_BAND)])[0])
            offset = max(0, int(query.get("offset", ["0"])[0]))
            self._send(200, json.dumps(console.list_rows(band, offset)).encode("utf-8"))
        elif url.path == "/api/plan":
            params = {k: v[0] for k, v in query.items()}
            self._send(
                200, json.dumps(console.plan(params.get("action", ""), params), default=str).encode("utf-8")
            )
        elif url.path == "/api/status":
            self._send(200, _status_body(console))
        elif url.path == "/api/log":
            n = int(query.get("n", ["120"])[0])
            lines = progress.log_tail(logs.log_file(console.cfg.root / console.cfg.data_dir), n)
            self._send(200, json.dumps({"lines": lines}).encode("utf-8"))
        elif url.path.startswith("/dashboard/"):
            self._static(url.path[len("/dashboard/") :] or "index.html")
        else:
            self._send(404, b'{"error":"not found"}')

    def _events(self) -> None:
        """Server-sent events: a `tick` whenever it changes, and at least every heartbeat; two
        seconds apart while a run is in progress, five between. Ends when the page goes."""
        console = self.console
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_sent, last_payload = 0.0, ""
        try:
            while True:
                tick = console.tick()
                payload = json.dumps(tick, default=str)
                now = time.monotonic()
                if payload != last_payload or now - last_sent >= HEARTBEAT_S:
                    self.wfile.write(f"event: tick\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                    last_sent, last_payload = now, payload
                busy = tick["state"]["running"] or console.child.alive()
                with console.changed:
                    console.changed.wait(TICK_RUNNING_S if busy else TICK_IDLE_S)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _static(self, rel: str) -> None:
        base = (self.console.cfg.root / self.console.cfg.dashboard_dir).resolve()
        path = (base / rel).resolve()
        if (base not in path.parents and path != base) or not path.is_file():
            self._send(404, b'{"error":"not found"}')
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        text_like = ctype.startswith("text/") or ctype.endswith("json")
        self._send(200, path.read_bytes(), ctype + ("; charset=utf-8" if text_like else ""))

    def do_POST(self) -> None:
        console = self.console
        if not console.host_allowed(self.headers.get("Host")):
            self._send(403, b'{"ok":false,"message":"forbidden: unexpected Host"}')
            return
        if self.headers.get("X-Bodh") not in ("console", "panel"):  # our own page, not a stray form
            self._send(403, b'{"ok":false,"message":"forbidden"}')
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            payload = {}
        path = urlsplit(self.path).path
        if path not in ("/api/action", "/api/mark"):
            self._send(404, b'{"ok":false,"message":"not found"}')
            return
        try:
            if path == "/api/mark":
                result = console.mark(str(payload.get("id", "")), str(payload.get("kind", "")))
            else:
                result = console.run_action(str(payload.get("action", "")), payload)
                console.forget()
        except Exception as exc:  # the page gets the reason as a message; the log keeps the traceback
            log.exception("console: action failed")
            result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        self._send(200, json.dumps(result, default=str).encode("utf-8"))


def _status_body(console: Console) -> bytes:
    try:
        return json.dumps(console.status(), default=str).encode("utf-8")
    except (
        Exception
    ) as exc:  # the page shows the error line instead of its numbers; the log keeps the traceback
        log.exception("console: status failed")
        return json.dumps({"error": str(exc)}).encode("utf-8")


def console_log(data_dir: Path, message: str) -> None:
    """A few lines in data/console.log so a console that will not start can be diagnosed from
    the project folder, without hunting for launchd's logs. Kept short."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / "console.log"
        lines = path.read_text(encoding="utf-8").splitlines()[-200:] if path.exists() else []
        lines.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def _extra_args(action: str, params: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if action in ("prune", "prune_dry"):
        if params.get("keep"):
            out += ["--keep", str(int(params["keep"]))]
        if params.get("below"):
            out += ["--below", str(float(params["below"]))]
    if action in ("clear_old", "clear_old_dry"):
        out += ["--days", str(int(params.get("days") or 14))]
    if action == "catch_up":
        if params.get("model"):
            out += ["--model", str(params["model"])]
        if params.get("all_ages"):
            out += ["--all-ages"]
        if params.get("workers"):
            out += ["--workers", str(int(params["workers"]))]
        if params.get("budget"):
            out += ["--budget", str(float(params["budget"]))]
    if action in ("run", "run_shadow", "run_sync") and params.get("limit"):
        out += ["--limit", str(int(params["limit"]))]
    return out


def _plan_words(app: Any, cfg: Config, action: str, params: dict[str, Any]) -> tuple[str, str]:
    """(the sentence, the button) for one confirmable action."""
    if action == "prune":
        keep = int(params.get("keep") or cfg.prune.keep)
        plan = app.prune_plan(keep=keep)
        return (
            f"Cut {plan.cut:,} of {plan.unread_scored:,} judged unread articles, keeping the {keep:,} best? "
            "They are marked read in BazQux; Undo reverses it; it takes about a minute.",
            f"Cut {plan.cut:,}",
        )
    if action == "undo":
        row = app.conn.execute(
            "SELECT r.id, (SELECT COUNT(*) FROM state WHERE cut_run_id = r.id) AS n FROM runs r "
            "WHERE r.kind IN ('run','prune','clear-old','bankruptcy') AND r.id IN (SELECT DISTINCT cut_run_id FROM state WHERE cut_run_id IS NOT NULL) "
            "ORDER BY r.id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise BodhError("nothing to undo: no run has cut anything")
        return (
            f"Undo run {row['id']} and mark its {row['n']:,} articles unread again in BazQux? It takes a few seconds.",
            f"Undo run {row['id']}",
        )
    if action == "clear_old":
        days = int(params.get("days") or 14)
        n = app.clear_old_count(days)
        return (
            f"Mark {n:,} unread articles older than {days} days read in BazQux? Starred and protected "
            "articles are exempt; Undo reverses it; it takes about a minute.",
            f"Mark {n:,} read",
        )
    if action == "catch_up":
        all_ages = str(params.get("all_ages", "")).lower() in ("true", "1", "on")
        plan = app.catch_up_plan(all_ages=all_ages)
        left = (
            f"{document.usd(plan.budget_left)} of today's budget is left"
            if plan.budget_left > 0
            else "today's budget is spent"
        )
        return (
            f"Judge the {plan.pending:,} waiting articles now with {plan.judge}? About {document.usd(plan.estimate_usd)}; {left}. "
            "It runs until done or the budget is reached.",
            f"Judge {plan.pending:,}",
        )
    if action == "autocut_on":
        return (
            f"Cut from the next run on? Articles under their folder's cut-off ({cfg.autocut.cutoff:g} unless a folder says "
            "otherwise) are marked read in BazQux, within the safety limits; Undo reverses each run's cut.",
            "Cut from the next run",
        )
    raise BodhError(f"{action} needs no confirmation")
