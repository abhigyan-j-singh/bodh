"""The service: the engine's JSON surface for the Mac app, and for any other client. It
lives inside the console's server (`bodh serve --app`, or `bodh console` with a token) and
adds to the console's routes everything the app needs: health, the reading list, one
article, search, the settings and their schema, the folders, the keys, the student profile,
the live checks of onboarding, the attention states, and the levers (run, stop, pause, mark,
steer, prune, undo, clear-old, catch-up). Every answer is JSON with the versioned `schema`
field (docs/json.md); every request carries the per-launch token when one is set. The
engine stays the only brain: each route calls `bodh.api.App` or starts the command you
would type, and nothing here decides a score or a cut."""

from __future__ import annotations

import json
import logging
import os
import secrets as secretsmod
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from . import __version__, locks, settings, store
from .api import App
from .config import KEYCHAIN_SERVICE, Secrets, keychain_get, keychain_set, load_secrets
from .errors import BodhError, ConfigError

if TYPE_CHECKING:
    from .console import Console

log = logging.getLogger(__name__)

SCHEMA = 1
EVENT_LOG_SIZE = 500  # named events kept for a stream that reconnects with Last-Event-ID
PAUSED_KEY = "serve.paused_until"  # kv: ISO UTC until when the schedule runs nothing
DISMISSED_KEY = "attention.dismissed"  # kv: JSON {key: fingerprint} of the banners the reader waved away
TOKEN_BYTES = 32
PROVIDER_SECRET = {  # provider -> the variable(s) its key lives under, in the order the app asks for them
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "gemini": ["GEMINI_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "openai-compatible": ["OPENAI_COMPATIBLE_BASE_URL", "OPENAI_COMPATIBLE_API_KEY"],
    "bazqux": ["BAZQUX_EMAIL", "BAZQUX_PASSWORD"],
    "healthchecks": ["HEALTHCHECK_URL"],
}
RUN_MODES = {"normal": [], "shadow": ["--shadow"], "sync": ["--sync"]}


def new_token() -> str:
    return secretsmod.token_hex(TOKEN_BYTES)


def read_token_file(path: Path) -> str:
    """The per-launch token the app wrote (32 random bytes, hex, mode 600) before spawning."""
    text = path.read_text(encoding="utf-8").strip()
    if len(text) < 16:
        raise ConfigError(f"the token file {path} is too short to be a token")
    return text


def _ok(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, {"schema": SCHEMA, **payload}


def _refused(message: str, code: int = 400, **extra: Any) -> tuple[int, dict[str, Any]]:
    return code, {"schema": SCHEMA, "ok": False, "message": message, **extra}


def _bool(value: Any) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


class Service:
    """The routes beyond the console's page, bound to one console (its configuration, its
    database, its child process and its change counter) and one token."""

    def __init__(self, console: Console, token: str | None = None, app_mode: bool = False) -> None:
        self.console = console
        self.token = token
        self.app_mode = app_mode
        self.started = time.time()
        self._seq = 0
        self._events: deque[tuple[int, str, dict[str, Any]]] = deque(maxlen=EVENT_LOG_SIZE)
        self._lock = threading.Lock()
        self._last_attention: list[str] | None = None
        self._last_running: bool | None = None
        self.reader_transport: Any = None  # a test's httpx transport for the reader checks

    # -- auth -------------------------------------------------------------------------

    def authorised(self, headers: Any, query: dict[str, list[str]]) -> bool:
        """With a token set, every request must carry it: `Authorization: Bearer` from a client,
        `?token=` from the page's own event source (which cannot set a header)."""
        if not self.token:
            return True
        header = headers.get("Authorization") or ""
        given = header[7:].strip() if header.startswith("Bearer ") else (query.get("token") or [""])[0]
        return bool(given) and secretsmod.compare_digest(given, self.token)

    # -- events -----------------------------------------------------------------------

    def emit(self, name: str, payload: dict[str, Any]) -> None:
        """A named event for every open stream; `Last-Event-ID` replays the ones missed."""
        with self._lock:
            self._seq += 1
            self._events.append((self._seq, name, payload))
        with self.console.changed:  # the streams wake; the generation is the console's own affair
            self.console.changed.notify_all()

    def events_since(self, seq: int) -> list[tuple[int, str, dict[str, Any]]]:
        with self._lock:
            return [e for e in self._events if e[0] > seq]

    @property
    def seq(self) -> int:
        return self._seq

    def notice(self, live: dict[str, Any]) -> None:
        """Called by the console on every tick: a run starting or ending and the attention
        changing become named events (the tick itself carries the phase and the counts)."""
        state = live["state"]
        running = bool(state["running"])
        if self._last_running is not None and running != self._last_running:
            if running:
                self.emit("run", {"state": "started", "kind": state.get("kind")})
            else:
                last = self._last_run_row()
                self.emit("run", {"state": "finished", **(last or {})})
        self._last_running = running
        keys = [a["key"] for a in self.attention_list()]
        if self._last_attention is not None and keys != self._last_attention:
            self.emit("attention", {"attention": self.attention_list()})
        self._last_attention = keys

    def _last_run_row(self) -> dict[str, Any] | None:
        rows = store.last_runs(self.console.conn, 1)
        if not rows:
            return None
        r = dict(rows[0])
        return {
            "run_id": r["id"],
            "kind": r["kind"],
            "ok": bool(r["ok"]),
            "summary": r.get("summary"),
            "error": r.get("error"),
        }

    # -- helpers ----------------------------------------------------------------------

    @property
    def cfg(self) -> Any:
        return self.console.cfg

    @property
    def conn(self) -> sqlite3.Connection:
        return self.console.conn

    def _app(self, secrets: Secrets | None = None) -> App:
        return App(
            cfg=self.cfg, conn=self.conn, secrets=secrets or load_secrets(self.cfg.root), log_to_file=False
        )

    def _with_app(self, fn: Any) -> Any:
        with self.console.db_lock:
            return fn(self._app())

    # -- GET --------------------------------------------------------------------------

    def get(self, path: str, query: dict[str, list[str]]) -> tuple[int, Any] | None:
        """The answer to a GET the service owns, or None when the console serves the path."""
        q = {k: v[0] for k, v in query.items()}
        self.console.refresh()
        routes: dict[str, Any] = {
            "/api/health": lambda: _ok(self.health()),
            "/api/list": lambda: _ok(self.reading_list(q)),
            "/api/why": lambda: self.why(q.get("ref", "")),
            "/api/find": lambda: _ok(self.find(q)),
            "/api/settings": lambda: _ok(self.settings_values()),
            "/api/settings/schema": lambda: (200, settings.schema_json(self.cfg)),
            "/api/folders": lambda: _ok(self.folders(live=_bool(q.get("live", "")))),
            "/api/keys": lambda: _ok(self.keys()),
            "/api/profile": lambda: _ok(self.profile()),
            "/api/profile/diff": lambda: _ok(self.profile_diff()),
            "/api/attention": lambda: _ok({"attention": self.attention_list()}),
            "/api/tags": lambda: _ok({"tags": [t.to_dict() for t in self._with_app(lambda a: a.tags())]}),
        }
        handler = routes.get(path)
        if handler is None and path.startswith("/api/why/"):
            ref = unquote(path[len("/api/why/") :])
            handler = lambda: self.why(ref)  # noqa: E731
        if handler is None:
            return None
        try:
            result: tuple[int, Any] = handler()
        except settings.SettingsError as exc:
            return _refused(str(exc), 422, errors=exc.errors)
        except BodhError as exc:
            return _refused(str(exc))
        return result

    def health(self) -> dict[str, Any]:
        cfg = self.cfg
        runs = [dict(r) for r in store.last_runs(self.conn, 1)]
        last = runs[0] if runs else None
        secrets = load_secrets(cfg.root)
        missing = [k for k in self.keys()["keys"] if k["required"] and not k["set"]]
        return {
            "engine": "bodh",
            "version": __version__,
            "pid": os.getpid(),
            "port": self.console.port,
            "app_mode": self.app_mode,
            "uptime_s": time.time() - self.started,
            "schema_version": int(store.kv_get(self.conn, store.SCHEMA_VERSION_KEY) or 0),
            "root": str(cfg.root),
            "data_dir": str(cfg.root / cfg.data_dir),
            "config_ok": True,
            "about_is_example": cfg.about_is_example(),
            "reader_configured": secrets.is_set("bazqux_email") and secrets.is_set("bazqux_password"),
            "keys_missing": [k["provider"] for k in missing],
            "mode": "autocut" if cfg.autocut.enabled else "scoring-only",
            "running": locks.running_process(cfg.root / cfg.data_dir),
            "last_run": {
                "id": last["id"],
                "kind": last["kind"],
                "ok": bool(last["ok"]),
                "finished_at": last.get("finished_at"),
                "summary": last.get("summary"),
            }
            if last
            else None,
            "next_run_in_s": next_run_in(self.conn, cfg),
            "paused_until": paused_until(self.conn),
            "setup_needed": _setup_needed(cfg, secrets),
        }

    def reading_list(self, q: dict[str, str]) -> dict[str, Any]:
        from .api import LIST_BAND, LIST_LIMIT

        band = float(q.get("band", LIST_BAND))
        folder = q.get("folder") or None
        limit = max(1, min(500, int(q.get("limit", LIST_LIMIT))))
        rows = self._with_app(lambda a: a.reading_list(band=band, folder=folder, limit=limit))
        return {"band": band, "folder": folder, "articles": [r.to_dict() for r in rows]}

    def why(self, ref: str) -> tuple[int, Any]:
        if not ref:
            return _refused("say which article: /api/why/<id> or ?ref=<id or url>")
        view = self._with_app(lambda a: a.why(ref))
        if view is None:
            return _refused("unknown article; it must have been fetched by a run first", 404)
        return _ok({"article": view.to_dict()})

    def find(self, q: dict[str, str]) -> dict[str, Any]:
        from .api import FIND_LIMIT

        words = q.get("q", "")
        unread = _bool(q.get("unread", ""))
        limit = max(1, min(500, int(q.get("limit", FIND_LIMIT))))
        hits = self._with_app(lambda a: a.find(words, unread=unread, limit=limit))
        return {"query": words, "unread": unread, "hits": [h.to_dict() for h in hits]}

    def settings_values(self) -> dict[str, Any]:
        cfg = self.cfg
        return {
            "settings": settings.values(cfg),
            "sources": settings.sources(cfg),
            "path": str(cfg.root / "config.yaml"),
            "old_keys": cfg.old_keys,
            "version": settings.SCHEMA_VERSION,
        }

    def folders(self, live: bool = False) -> dict[str, Any]:
        """The folders bodh knows (from the articles of the last thirty days) with each one's
        policy; `live` asks BazQux for the account's own list and the feeds in each."""
        cfg = self.cfg
        since = store.days_ago_iso(30)
        rows = self.conn.execute(
            "SELECT COALESCE(folder, '') AS folder, COUNT(*) AS n FROM articles WHERE fetched_at >= ? GROUP BY folder",
            (since,),
        ).fetchall()
        counts = {str(r["folder"]): int(r["n"]) for r in rows}
        names = set(counts) | set(cfg.folders)
        feeds: dict[str, int] = {}
        in_account: set[str] | None = None
        if live:
            secrets = load_secrets(cfg.root)
            from .readers.bazqux import BazQuxClient

            client = BazQuxClient(
                secrets.value("bazqux_email"),
                secrets.value("bazqux_password"),
                transport=self.reader_transport,
            )
            subs = client.subscriptions()
            in_account = set()
            for s in subs:
                for f in s.folders:
                    in_account.add(f)
                    feeds[f] = feeds.get(f, 0) + 1
            names |= in_account
        out = []
        for name in sorted(n for n in names if n):
            policy = cfg.folders.get(name)
            out.append(
                {
                    "name": name,
                    "articles_per_day": round(counts.get(name, 0) / 30, 1),
                    "feeds": feeds.get(name),
                    "judge": policy.judge if policy else True,
                    "protected": policy.protected if policy else False,
                    "cutoff": policy.cutoff if policy else None,
                    "cutoff_in_effect": cfg.cutoff_for(name),
                    "in_config": policy is not None,
                    "in_account": (name in in_account) if in_account is not None else None,
                }
            )
        return {"folders": out, "live": live, "default_cutoff": cfg.autocut.cutoff}

    def keys(self) -> dict[str, Any]:
        """Set or not, per provider and for the reader, with where each value came from; never
        a value, never a prefix."""
        root = self.cfg.root
        env_path = root / ".env"
        env_lines: dict[str, str] = {}
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    env_lines[k.strip()] = v.strip()
        used = {
            m
            for role in ("judge", "reviewer", "profiler")
            for m in [_provider_of(getattr(self.cfg.models, role))]
        }
        used.add(_provider_of(self.cfg.catch_up.model))
        out = []
        for provider, variables in PROVIDER_SECRET.items():
            states = []
            for var in variables:
                source = _source_of(var, env_lines)
                states.append({"variable": var, "set": source is not None, "source": source})
            required_vars = variables if provider != "openai-compatible" else variables[:1]
            is_set = all(s["set"] for s in states if s["variable"] in required_vars)
            out.append(
                {
                    "provider": provider,
                    "set": is_set,
                    "required": provider == "bazqux" or provider in used,
                    "variables": states,
                }
            )
        return {"keys": out, "env_file": env_path.exists(), "keychain_service": KEYCHAIN_SERVICE}

    def profile(self) -> dict[str, Any]:
        cfg = self.cfg
        about, learned = cfg.read_about(), cfg.read_learned()
        versions = store.learned_versions(self.conn, 1)
        newest = versions[0] if versions else None
        return {
            "about": about,
            "learned": learned,
            "about_is_example": cfg.about_is_example(),
            "about_path": str(cfg.about_path),
            "learned_path": str(cfg.learned_path),
            "version": int(newest["version"]) if newest else None,
            "rewritten_at": newest["created_at"] if newest else None,
            "questions": [{"key": k, "question": q} for k, q in _questions()],
        }

    def profile_diff(self) -> dict[str, Any]:
        from .web import document

        prof = document.profile_part(self.cfg, self.conn, document.local_now(self.cfg))
        return {
            "version": prof.version,
            "date_text": prof.date_text,
            "diff": [d.model_dump() for d in prof.diff],
        }

    def attention_list(self) -> list[dict[str, Any]]:
        from .web import document

        now = document.local_now(self.cfg)
        return [a.model_dump() for a in document.attentions(self.cfg, self.conn, now)]

    # -- POST, PUT, DELETE ---------------------------------------------------------------

    def post(self, method: str, path: str, payload: dict[str, Any]) -> tuple[int, Any] | None:
        """The answer to a POST, PUT or DELETE the service owns, or None when the console does."""
        self.console.refresh()
        try:
            handler = self._route(method, path)
            if handler is None:
                return None
            result: tuple[int, Any] = handler(payload)
            self.console.forget()
            return result
        except settings.SettingsError as exc:
            return _refused(str(exc), 422, errors=exc.errors)
        except BodhError as exc:
            return _refused(str(exc))

    def _route(self, method: str, path: str) -> Any:
        routes: dict[tuple[str, str], Any] = {
            ("POST", "/api/run"): self.run,
            ("POST", "/api/stop"): self.stop,
            ("POST", "/api/pause"): self.pause,
            ("POST", "/api/steer"): self.steer,
            ("POST", "/api/prune"): self.prune,
            ("POST", "/api/undo"): self.undo,
            ("POST", "/api/clear_old"): self.clear_old,
            ("POST", "/api/catch_up"): self.catch_up,
            ("POST", "/api/safety/reset"): self.safety_reset,
            ("PUT", "/api/settings"): self.put_settings,
            ("POST", "/api/settings"): self.put_settings,
            ("PUT", "/api/profile"): self.put_profile,
            ("POST", "/api/profile"): self.put_profile,
            ("POST", "/api/keys/import-env"): self.import_env,
            ("POST", "/api/check/reader"): self.check_reader,
            ("POST", "/api/check/provider"): self.check_provider,
            ("POST", "/api/attention/dismiss"): self.dismiss,
        }
        if (method, path) in routes:
            return routes[(method, path)]
        if path.startswith("/api/keys/") and method in ("PUT", "POST", "DELETE"):
            provider = unquote(path[len("/api/keys/") :])
            if method == "DELETE":
                return lambda payload: self.delete_key(provider)
            return lambda payload: self.put_key(provider, payload)
        return None

    def run(self, payload: dict[str, Any]) -> tuple[int, Any]:
        mode = str(payload.get("mode") or "normal")
        if mode not in RUN_MODES:
            return _refused("mode must be normal, shadow or sync")
        args = ["run", *RUN_MODES[mode]]
        if payload.get("limit"):
            args += ["--limit", str(int(payload["limit"]))]
        res = self.console.run_action_args(args)
        return (200 if res["ok"] else 409), {"schema": SCHEMA, **res}

    def stop(self, payload: dict[str, Any]) -> tuple[int, Any]:
        res = self.console.run_action("stop", payload)
        return _ok(res)

    def pause(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """`{until: ISO}`, `{minutes: N}`, `{until: "tomorrow"}` or `{until: null}` (resume)."""
        until = payload.get("until")
        minutes = payload.get("minutes")
        now = datetime.now(timezone.utc)
        if minutes:
            when: datetime | None = now + timedelta(minutes=float(minutes))
        elif until in (None, "", "now", "resume"):
            when = None
        elif until == "tomorrow":
            from zoneinfo import ZoneInfo

            local = now.astimezone(ZoneInfo(self.cfg.timezone))
            tomorrow = (local + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
            when = tomorrow.astimezone(timezone.utc)
        else:
            try:
                when = datetime.fromisoformat(str(until))
            except ValueError:
                return _refused("until must be an ISO 8601 time, 'tomorrow', or null to resume")
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        set_paused_until(self.conn, when)
        self.emit("settings", {"what": "pause", "paused_until": when.isoformat() if when else None})
        return _ok({"ok": True, "paused_until": when.isoformat() if when else None})

    def steer(self, payload: dict[str, Any]) -> tuple[int, Any]:
        direction, phrase = str(payload.get("direction", "")), str(payload.get("phrase", "")).strip()
        if not phrase:
            return _refused("say what to steer towards or away from (phrase)")
        message = self._with_app(lambda a: a.steer(direction, phrase))
        return _ok({"ok": True, "message": message})

    def prune(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """A dry run returns the plan; the cut itself needs `confirm: true` and starts `bodh
        prune --yes` with the same arguments, its output in the child."""
        keep = payload.get("keep")
        fraction = payload.get("fraction")
        below = payload.get("below")
        if payload.get("dry_run", True) and not payload.get("confirm"):
            plan = self._with_app(
                lambda a: a.prune_plan(
                    keep=int(keep) if keep is not None else None,
                    fraction=float(fraction) if fraction is not None else None,
                    below=float(below) if below is not None else None,
                )
            )
            words = self.console.plan("prune", {"keep": keep or self.cfg.prune.keep})
            return _ok(
                {"ok": True, "plan": plan.to_dict(), **{k: words.get(k) for k in ("sentence", "button")}}
            )
        args = ["prune", "--yes"]
        if keep is not None:
            args += ["--keep", str(int(keep))]
        if fraction is not None:
            args += ["--fraction", str(float(fraction))]
        if below is not None:
            args += ["--below", str(float(below))]
        res = self.console.run_action_args(args)
        return (200 if res["ok"] else 409), {"schema": SCHEMA, **res}

    def undo(self, payload: dict[str, Any]) -> tuple[int, Any]:
        args = ["undo"]
        if payload.get("run_id"):
            args += ["--run", str(int(payload["run_id"]))]
        res = self.console.run_action_args(args, background=False)
        return _ok(res)

    def clear_old(self, payload: dict[str, Any]) -> tuple[int, Any]:
        days = int(payload.get("days") or 14)
        if payload.get("dry_run", True) and not payload.get("confirm"):
            n = self._with_app(lambda a: a.clear_old_count(days))
            words = self.console.plan("clear_old", {"days": days})
            return _ok(
                {"ok": True, "days": days, "count": n, **{k: words.get(k) for k in ("sentence", "button")}}
            )
        res = self.console.run_action_args(["clear-old", "--yes", "--days", str(days)])
        return (200 if res["ok"] else 409), {"schema": SCHEMA, **res}

    def catch_up(self, payload: dict[str, Any]) -> tuple[int, Any]:
        if payload.get("dry_run", True) and not payload.get("confirm"):
            plan = self._with_app(
                lambda a: a.catch_up_plan(
                    model=payload.get("model") or None,
                    all_ages=_bool(payload.get("all_ages", "")),
                    budget=float(payload["budget"]) if payload.get("budget") else None,
                )
            )
            words = self.console.plan("catch_up", payload)
            return _ok(
                {
                    "ok": True,
                    "plan": {
                        "pending": plan.pending,
                        "judge": plan.judge,
                        "quick": plan.quick,
                        "estimate_usd": plan.estimate_usd,
                        "budget_left": plan.budget_left,
                        "budget_needed": plan.budget_needed,
                        "all_ages": plan.all_ages,
                    },
                    **{k: words.get(k) for k in ("sentence", "button")},
                }
            )
        from .console import _extra_args

        args = ["catch-up", "--yes", *_extra_args("catch_up", payload)]
        res = self.console.run_action_args(args)
        return (200 if res["ok"] else 409), {"schema": SCHEMA, **res}

    def safety_reset(self, payload: dict[str, Any]) -> tuple[int, Any]:
        res = self.console.run_action_args(["safety", "reset"], background=False)
        return _ok(res)

    def put_settings(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """`{changes: {dotted key: value}}`, or `{settings: {...the whole object...}}` of which
        only the keys that differ are written. The file keeps its comments; the models refuse a
        bad value with the key named and nothing written."""
        changes: dict[str, Any] = dict(payload.get("changes") or {})
        whole = payload.get("settings")
        if isinstance(whole, dict):
            current = settings.values(self.cfg)
            for key, value in whole.items():
                if key in current and current[key] != value:
                    changes[key] = value
        if not changes:
            return _ok({"ok": True, "changed": [], **self.settings_values()})
        cfg, notes = settings.apply(self.cfg.root, changes)
        self.console.cfg = cfg
        self.console.config_changed()  # the console re-reads what was just written
        self.emit("settings", {"what": "config", "changed": sorted(changes), "notes": notes})
        return _ok({"ok": True, "changed": sorted(changes), "notes": notes, **self.settings_values()})

    def put_profile(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """`{about: text}` writes profile/about.md (or `{answers: {who, knows, goal, decisions,
        never}}`, the five questions, in the example's shape); `{learned: text}` writes a new
        version of the learned profile, like a steer does."""
        from . import init as initmod
        from . import profiler

        cfg = self.cfg
        written = []
        answers = payload.get("answers")
        if isinstance(answers, dict):
            missing = [k for k, _ in _questions() if not str(answers.get(k, "")).strip()]
            if missing:
                return _refused(f"every question needs an answer; missing: {', '.join(missing)}")
            text = initmod.about_text({k: str(v) for k, v in answers.items()})
            cfg.about_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.about_path.write_text(text, encoding="utf-8")
            written.append("about")
        elif isinstance(payload.get("about"), str):
            text = str(payload["about"]).strip()
            if not text:
                return _refused("about must not be empty")
            cfg.about_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.about_path.write_text(text + "\n", encoding="utf-8")
            written.append("about")
        if isinstance(payload.get("learned"), str):
            text = str(payload["learned"]).strip()
            if not text:
                return _refused("learned must not be empty")
            with self.console.db_lock:
                profiler.write_version(cfg, self.conn, text, "edited in the app")
            written.append("learned")
        if not written:
            return _refused("say what to write: about, answers or learned")
        self.emit("settings", {"what": "profile", "changed": written})
        return _ok({"ok": True, "written": written, **self.profile()})

    def put_key(self, provider: str, payload: dict[str, Any]) -> tuple[int, Any]:
        """Store a provider's key (`{key}`), a local service's base URL (`{base_url, key?}`) or
        the reader's sign-in (`{email, password}`) in the keychain, through the engine, so the
        same program that reads the item created it (the Keychain then never asks). `{where:
        "env"}` writes `.env` instead. The values are never logged or echoed."""
        if provider not in PROVIDER_SECRET:
            return _refused(f"unknown provider {provider!r}; one of {', '.join(PROVIDER_SECRET)}", 404)
        where = str(payload.get("where") or "keychain")
        values: dict[str, str] = {}
        if provider == "bazqux":
            values = {
                "BAZQUX_EMAIL": str(payload.get("email", "")),
                "BAZQUX_PASSWORD": str(payload.get("password", "")),
            }
        elif provider == "openai-compatible":
            values = {"OPENAI_COMPATIBLE_BASE_URL": str(payload.get("base_url") or payload.get("key", ""))}
            if payload.get("api_key") or (payload.get("base_url") and payload.get("key")):
                values["OPENAI_COMPATIBLE_API_KEY"] = str(payload.get("api_key") or payload.get("key"))
        elif provider == "healthchecks":
            values = {"HEALTHCHECK_URL": str(payload.get("url") or payload.get("key", ""))}
        else:
            values = {PROVIDER_SECRET[provider][0]: str(payload.get("key", ""))}
        if any(not v.strip() for v in values.values()):
            return _refused("a value is empty")
        for var, value in values.items():
            if where == "env":
                from .config import write_env_value

                write_env_value(self.cfg.root, var, value.strip())
            else:
                keychain_set(var, value.strip())
                _drop_env_value(self.cfg.root, var)  # one place for each secret
        self.emit("settings", {"what": "keys", "provider": provider})
        return _ok({"ok": True, "provider": provider, "where": where, **self.keys()})

    def delete_key(self, provider: str) -> tuple[int, Any]:
        if provider not in PROVIDER_SECRET:
            return _refused(f"unknown provider {provider!r}", 404)
        removed = []
        for var in PROVIDER_SECRET[provider]:
            if _keychain_delete(var):
                removed.append(f"{var}: keychain")
            if _drop_env_value(self.cfg.root, var):
                removed.append(f"{var}: .env")
        self.emit("settings", {"what": "keys", "provider": provider})
        return _ok({"ok": True, "provider": provider, "removed": removed, **self.keys()})

    def import_env(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """Every value of `.env` into the keychain; `{delete: true}` removes the file after."""
        path = self.cfg.root / ".env"
        if not path.exists():
            return _refused("there is no .env to import", 404)
        moved = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            var, value = line.split("=", 1)
            var, value = var.strip(), value.strip().strip("'\"")
            if var and value:
                keychain_set(var, value)
                moved.append(var)
        if _bool(payload.get("delete", "")):
            path.unlink()
        self.emit("settings", {"what": "keys", "provider": "all"})
        return _ok({"ok": True, "imported": moved, "env_deleted": not path.exists(), **self.keys()})

    def check_reader(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """Sign in to BazQux once with the given email and password (or the stored ones) and
        say what was found; a failure is an answer, not an error."""
        from .readers.bazqux import BazQuxClient

        secrets = load_secrets(self.cfg.root)
        email = str(payload.get("email") or secrets.value("bazqux_email"))
        password = str(payload.get("password") or secrets.value("bazqux_password"))
        if not email or not password:
            return _ok({"ok": False, "message": "the email and the password are needed"})
        try:
            client = BazQuxClient(email, password, transport=self.reader_transport)
            client.user_info()
            subs = client.subscriptions()
            folders = sorted({f for s in subs for f in s.folders})
            unread = len(client.unread_ids())
            tags = client.tags()
            stream = self.cfg.protect.must_read_stream
        except Exception as exc:  # the check reports; the message carries the reason, never the password
            return _ok({"ok": False, "message": _scrub(str(exc), [password])})
        return _ok(
            {
                "ok": True,
                "message": f"signed in: {len(subs)} feeds in {len(folders)} folders, {unread} unread",
                "email": email,
                "feeds": len(subs),
                "folders": folders,
                "unread": unread,
                "must_read_stream": stream,
                "must_read_stream_present": f"user/-/label/{stream}" in tags,
            }
        )

    def check_provider(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """One five-token completion through the provider with the given key (or the stored
        one); the cost is stated in cents."""
        from . import init as initmod
        from .providers.base import PROVIDERS, REQUIRED_SECRET, ModelRef

        provider = str(payload.get("provider") or "anthropic")
        if provider not in PROVIDERS:
            return _refused(f"unknown provider {provider!r}; one of {', '.join(PROVIDERS)}")
        secrets = load_secrets(self.cfg.root)
        key = str(payload.get("key") or secrets.value(REQUIRED_SECRET[provider].lower()))
        if not key:
            return _ok({"ok": False, "message": "no key given and none stored"})
        model = str(payload.get("model") or "")
        if not model:
            judge = self.cfg.models.judge
            model = judge if ModelRef.parse(judge).provider == provider else initmod.default_judge(provider)
        if not model:
            return _ok({"ok": False, "message": f"name a model for {provider} (provider/model-id)"})
        try:
            line = initmod.check_model(provider, key, model)
        except Exception as exc:  # the check reports; the key is scrubbed from the reason
            return _ok(
                {"ok": False, "message": _scrub(str(exc), [key]), "provider": provider, "model": model}
            )
        p_in, p_out = self.cfg.models.price(model)
        cents = (30 * p_in + 5 * p_out) / 1_000_000 * 100
        return _ok(
            {"ok": True, "message": line, "provider": provider, "model": model, "cents": round(cents, 4)}
        )

    def dismiss(self, payload: dict[str, Any]) -> tuple[int, Any]:
        key = str(payload.get("key", ""))
        items = self.attention_list()
        found = next((a for a in items if a["key"] == key), None)
        if found is None:
            return _refused(f"no banner {key!r} to dismiss", 404)
        dismiss_attention(self.conn, key, str(found.get("fingerprint") or ""))
        self.emit("attention", {"attention": self.attention_list()})
        return _ok({"ok": True, "attention": self.attention_list()})


# -- shared helpers (the scheduler, the status and the dashboard read these too) -----------


def paused_until(conn: sqlite3.Connection) -> str | None:
    """ISO UTC until when the schedule is paused, or None; a pause in the past is over."""
    value = store.kv_get(conn, PAUSED_KEY)
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return value if when > datetime.now(timezone.utc) else None


def set_paused_until(conn: sqlite3.Connection, when: datetime | None) -> None:
    if when is None:
        store.kv_delete(conn, PAUSED_KEY)
    else:
        store.kv_set(conn, PAUSED_KEY, when.astimezone(timezone.utc).isoformat())
    conn.commit()


SERVE_KEY = "serve.state"  # kv: JSON {pid, every_seconds, started} written by `bodh serve`


def serve_state(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """What `bodh serve` wrote about itself, if its process is still alive."""
    raw = store.kv_get(conn, SERVE_KEY)
    if not raw:
        return None
    try:
        st = json.loads(raw)
    except ValueError:
        return None
    return st if st.get("pid") and locks.pid_alive(int(st["pid"])) else None


def next_run_in(conn: sqlite3.Connection, cfg: Any) -> float | None:
    """Seconds until `bodh serve` starts the next run, from its own clock; None when it is not
    running here, a run is in progress, or the schedule is paused."""
    from .serve import LAST_RUN_KEY

    st = serve_state(conn)
    if st is None or locks.running_process(cfg.root / cfg.data_dir) is not None:
        return None
    if paused_until(conn):
        return None
    last = float(store.kv_get(conn, LAST_RUN_KEY) or 0)
    every = float(st.get("every_seconds") or 1800)
    return max(0.0, every - (time.time() - last))


def dismissed(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return dict(json.loads(store.kv_get(conn, DISMISSED_KEY) or "{}"))
    except ValueError:
        return {}


def dismiss_attention(conn: sqlite3.Connection, key: str, fingerprint: str) -> None:
    items = dismissed(conn)
    items[key] = fingerprint
    store.kv_set(conn, DISMISSED_KEY, json.dumps(items))
    conn.commit()


def _setup_needed(cfg: Any, secrets: Secrets) -> list[str]:
    """What onboarding still has to do, in order; empty when the engine can run."""
    out = []
    if not (secrets.is_set("bazqux_email") and secrets.is_set("bazqux_password")):
        out.append("reader")
    from .llm import secret_field
    from .providers.base import ModelRef

    for role in ("judge", "reviewer", "profiler"):
        provider = ModelRef.parse(getattr(cfg.models, role)).provider
        if not secrets.is_set(secret_field(provider)) and "key" not in out:
            out.append("key")
    if cfg.about_is_example() or not cfg.read_about():
        out.append("profile")
    return out


def _questions() -> tuple[tuple[str, str], ...]:
    from .init import QUESTIONS

    return QUESTIONS


def _provider_of(model: str) -> str:
    from .providers.base import ModelRef

    return ModelRef.parse(model).provider


def _source_of(var: str, env_lines: dict[str, str]) -> str | None:
    if env_lines.get(var):
        return ".env"
    if os.environ.get(var):
        return "environment"
    if keychain_get(var):
        return "keychain"
    return None


def _drop_env_value(root: Path, var: str) -> bool:
    path = root / ".env"
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if line.split("=", 1)[0].strip() != var]
    if len(kept) == len(lines):
        return False
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return True


def _keychain_delete(var: str) -> bool:
    try:
        import keyring

        if keyring.get_password(KEYCHAIN_SERVICE, var):
            keyring.delete_password(KEYCHAIN_SERVICE, var)
            return True
    except Exception:  # no keyring, no backend, nothing stored: nothing to remove
        return False
    return False


def _scrub(text: str, values: list[str]) -> str:
    for value in values:
        if value:
            text = text.replace(value, "[secret]")
    return text
