"""The whole system in one view, and the levers: what is running, what launchd will do
next, what the last runs did, the unread, the spend; start a run, stop it, pause or resume
the schedule, autocut on or off, prune, undo. Used by `bodh status` and by the console
(`bodh console`)."""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from . import locks, logs, progress, store
from .config import Config

log = logging.getLogger(__name__)

JOBS = {
    "run": "bodh.run",
    "weekly": "bodh.weekly",
    "report": "bodh.report",
    "console": "bodh.console",
}
# the labels an install from before the rename used; the installer boots them out
OLD_JOBS = [
    "com.antriksh.triage",
    "com.antriksh.triage.weekly",
    "com.antriksh.triage.report",
    "com.antriksh.triage.panel",
]


# -- launchd ------------------------------------------------------------------


def launchctl_available() -> bool:
    return shutil.which("launchctl") is not None


def _plist_path(label: str) -> tuple[Path | None, str]:
    """Where the job's plist lives: (path, 'agent' | 'daemon' | '')."""
    agent = Path(os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist"))
    daemon = Path(f"/Library/LaunchDaemons/{label}.plist")
    if agent.exists():
        return agent, "agent"
    if daemon.exists():
        return daemon, "daemon"
    return None, ""


def _domain(mode: str) -> str:
    return "system" if mode == "daemon" else f"gui/{os.getuid()}"


def job_state(label: str) -> dict[str, Any]:
    """installed, loaded, state ('running', 'waiting', 'not loaded', 'not installed', 'unknown'),
    last exit code, pid, next fire interval, as launchctl reports them."""
    out = {
        "label": label,
        "installed": False,
        "loaded": False,
        "state": "not installed",
        "last_exit": None,
        "pid": None,
        "mode": "",
        "plist": None,
        "interval": None,
    }
    path, mode = _plist_path(label)
    if path is None:
        return out
    out.update(installed=True, mode=mode, plist=str(path))
    if not launchctl_available():
        out["state"] = "unknown (no launchctl here)"
        return out
    try:
        res = subprocess.run(
            ["launchctl", "print", f"{_domain(mode)}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        out["state"] = f"unknown ({exc})"
        return out
    if res.returncode != 0:
        out["state"] = "not loaded"
        return out
    out["loaded"] = True
    text = res.stdout
    m = re.search(r"^\s*state = (\w+)", text, re.M)
    out["state"] = m.group(1) if m else "loaded"
    m = re.search(r"^\s*last exit code = (-?\d+|\(never exited\))", text, re.M)
    if m:
        out["last_exit"] = None if "never" in m.group(1) else int(m.group(1))
    m = re.search(r"^\s*pid = (\d+)", text, re.M)
    if m:
        out["pid"] = int(m.group(1))
    m = re.search(r"^\s*run interval = (\d+) seconds", text, re.M)
    if m:
        out["interval"] = int(m.group(1))
    return out


def schedule(action: str, which: str = "all", data_dir: Path | None = None) -> str:
    """'pause' unloads the jobs (the plists stay; nothing fires until 'resume'); 'resume' loads
    them; 'kick' starts a run now. ``which`` is a job name or 'all' (pause and resume only).
    A kick is refused while a run holds the lock (``data_dir`` given): launchctl's kickstart
    -k would kill it, and the new one would exit at once on the lock anyway."""
    if action == "kick":
        which = "run" if which == "all" else which
        if data_dir is not None and which == "run":
            proc = locks.running_process(data_dir)
            if proc is not None:
                return (
                    f"not kicked: {proc['kind']} (pid {proc['pid']}) is running; wait for it or `bodh stop` "
                    "it first"
                )
    names = list(JOBS) if which == "all" else [which]
    lines = [_schedule_one(action, name) for name in names]
    if action == "pause" and which == "all":
        lines.append("the console is paused too; `bodh schedule resume` brings everything back")
    return "\n".join(lines)


def _schedule_one(action: str, which: str) -> str:
    label = JOBS[which]
    path, mode = _plist_path(label)
    if path is None:
        return f"{label} is not installed; run ./install-launchd.sh first"
    if not launchctl_available():
        return "launchctl is not available on this machine"
    if mode == "daemon":
        return f"{label} runs as a daemon; pause it with: sudo launchctl bootout system/{label}"
    dom = _domain(mode)
    commands = {
        "pause": (["launchctl", "bootout", f"{dom}/{label}"], f"paused {label}", f"{label} already paused"),
        "resume": (["launchctl", "bootstrap", dom, str(path)], f"resumed {label}", f"could not load {label}"),
        "kick": (
            ["launchctl", "kickstart", "-k", f"{dom}/{label}"],
            f"started {label} now",
            f"could not start {label}",
        ),
    }
    if action not in commands:
        return f"unknown action {action}"
    argv, done, failed = commands[action]
    res = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    if res.returncode == 0:
        return done
    why = res.stderr.strip() or res.stdout.strip() or ("not loaded" if action == "pause" else "")
    return f"{failed} ({why})" if action == "pause" else f"{failed}: {why}"


# -- the console's own state ------------------------------------------------


def console_state(cfg: Config) -> dict[str, Any]:
    """Is the console answering? From data/console.json (written by `bodh console`) and a
    one-second connection attempt; the last line of data/console.log explains a failure."""
    data_dir = cfg.root / cfg.data_dir
    info: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        info = json.loads((data_dir / "console.json").read_text(encoding="utf-8"))
    host, port = info.get("host", "127.0.0.1"), int(info.get("port", 8766))
    try:
        with socket.create_connection((host, port), timeout=1.0):
            up = True
    except OSError:
        up = False
    note = ""
    try:
        lines = (data_dir / "console.log").read_text(encoding="utf-8").splitlines()
        note = lines[-1] if lines else ""
    except OSError:
        pass
    return {"up": up, "url": f"http://{host}:{port}", "pid": info.get("pid"), "note": note}


def stray_processes(root: Path | None = None) -> list[tuple[int, str]]:
    """bodh commands of this checkout still alive on this machine (run, prune, catch-up,
    console …), except this one. Anchored to the checkout's path: a stop --all from one checkout must
    not end another's console, or an editor whose arguments mention the path."""
    try:
        res = subprocess.run(
            ["pgrep", "-fl", stray_pattern(root)], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in res.stdout.splitlines():
        pid_s, _, cmd = line.partition(" ")
        if pid_s.isdigit() and int(pid_s) != os.getpid():
            out.append((int(pid_s), cmd.strip()))
    return out


def stray_pattern(root: Path | None) -> str:
    """The pgrep pattern for this checkout's own commands, anchored at the start of the
    command line so a path merely mentioned in another program's arguments does not match."""
    # a console script runs as `<venv>/bin/python <venv>/bin/bodh …` (its shebang), so the
    # interpreter may come first
    if root is not None:
        return (
            rf"^(\S+/python\S* )?{re.escape(str(root))}/\.venv/bin/(bodh|triage)( |$)"
            r"|^\S*python\S* -m bodh\.cli( |$)"
        )
    return r"^(\S+/python\S* )?\S*/\.venv/bin/(bodh|triage)( |$)|^\S*python\S* -m bodh\.cli( |$)"


def stop_all(cfg: Config) -> list[str]:
    """Stop everything: interrupt the run that holds the lock (so it finishes its run row and
    pings the heartbeat), then pause every launchd job, then end any other bodh process of
    this checkout (the console included). Nothing is lost: judged articles stay judged,
    half-written cuts are finished by the next run."""
    notes = [locks.stop_process(cfg.root / cfg.data_dir)]
    notes.append(schedule("pause", "all"))
    for pid, cmd in stray_processes(cfg.root):
        try:
            os.kill(pid, signal.SIGTERM)
            notes.append(f"ended pid {pid}: {cmd[:80]}")
        except OSError as exc:
            notes.append(f"could not end pid {pid}: {exc}")
    time.sleep(1.0)
    for pid, cmd in stray_processes(cfg.root):
        try:
            os.kill(pid, signal.SIGKILL)
            notes.append(f"killed pid {pid}: {cmd[:80]}")
        except OSError:
            pass
    with contextlib.suppress(OSError):
        (cfg.root / cfg.data_dir / "console.json").unlink()
    notes.append("everything is stopped; `bodh schedule resume` starts the jobs again")
    return notes


# -- config levers --------------------------------------------------------------


def set_autocut(root: Path, on: bool) -> tuple[bool, str]:
    """Flip `autocut.enabled` in config.yaml in place, keeping every other line (and every
    comment) as it is. The key is found under its section whatever its spelling (true, True,
    yes, "false" …), added there when missing, and the result is read back: the edit counts
    only if the loaded configuration differs in that one value and nothing else. A file that
    still uses the old layout (`cut: enforce:`) is edited under the old names, so the file
    keeps loading as it did. Returns (ok, message)."""
    path = root / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8")
        before = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError) as exc:
        return False, f"config.yaml could not be read: {exc}"
    new_text, section_key, why = _flagged_config(text, before, on)
    if new_text is None:
        return False, why
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return False, f"config.yaml could not be written: {exc}"
    state = (
        "true: autocut cuts below the cut-off from the next run"
        if on
        else "false: scoring-only mode, nothing is cut"
    )
    return True, f"{section_key} is now {state}"


def _flagged_config(text: str, before: object, on: bool) -> tuple[str | None, str, str]:
    """config.yaml's text with the flag set and checked, as (new text, "section.key", "") or
    (None, "", why it was refused)."""
    if not isinstance(before, dict):
        return None, "", "config.yaml does not hold a mapping of sections; edit it by hand"
    section, key = "autocut", "enabled"
    if "autocut" not in before and isinstance(before.get("cut"), dict) and "enforce" in before["cut"]:
        section, key = "cut", "enforce"  # the layout before October 2026, read for one release
    new_text = _edit_flag(text, section, key, on)
    try:
        after = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as exc:
        return None, "", f"the edit would break config.yaml ({exc}); edit it by hand"
    expected = copy.deepcopy(before)
    expected.setdefault(section, {})
    if not isinstance(expected[section], dict):
        return None, "", f"the `{section}:` section is not a mapping; edit config.yaml by hand"
    expected[section][key] = on
    if after != expected:
        return None, "", f"could not find a place for {section}.{key} in config.yaml; edit it by hand"
    return new_text, f"{section}.{key}", ""


_SECTION_RE = re.compile(r"^(\w[\w-]*):\s*(#.*)?$")  # a top-level `name:` line


def _edit_flag(text: str, section: str, key: str, on: bool) -> str:
    """The text of config.yaml with `section.key` set, by lines: the `key:` line directly under
    `section:` is rewritten (its trailing comment kept), inserted after `section:` when there
    is none, or a `section:` block is appended when there is none."""
    value = "true" if on else "false"
    key_re = re.compile(rf"^(?P<indent>\s+)(?P<key>['\"]?{re.escape(key)}['\"]?:)(?P<rest>.*)$")
    lines = text.splitlines(keepends=True)
    section_at = None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line.rstrip("\n"))
        if m and m.group(1) == section:
            section_at = i
            break
        if line.startswith(f"{section}:"):
            return (
                text  # `section: { ... }` on one line: not a shape to edit blind; the caller's check refuses
            )
    if section_at is None:
        tail = "" if not text or text.endswith("\n") else "\n"
        return text + f"{tail}\n{section}:\n  {key}: {value}\n"
    end = len(lines)
    for i in range(section_at + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#") and not lines[i][0].isspace():
            end = i  # the next top-level key
            break
    child_indent: int | None = None
    for i in range(section_at + 1, end):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(lines[i]) - len(lines[i].lstrip())
        if child_indent is None:
            child_indent = indent
        m = key_re.match(lines[i].rstrip("\n"))
        if m and indent == child_indent:
            rest = m.group("rest")
            comment = ""
            hash_at = rest.find(" #")
            if hash_at >= 0:
                comment = rest[hash_at:]
            lines[i] = f"{m.group('indent')}{key}: {value}{comment}\n"
            return "".join(lines)
    pad = " " * (child_indent if child_indent else 2)
    lines.insert(section_at + 1, f"{pad}{key}: {value}\n")
    return "".join(lines)


# -- the whole picture ------------------------------------------------------------


def status(cfg: Config, conn: sqlite3.Connection, jobs: dict[str, Any] | None = None) -> dict[str, Any]:
    """The whole picture. ``jobs`` lets a caller pass launchd's answers from a cache."""
    data_dir = cfg.root / cfg.data_dir
    proc = locks.running_process(data_dir)
    snap = progress.snapshot(conn, cfg)
    untagged, unmarked = store.cut_pending(conn)
    runs = [dict(r) for r in store.last_runs(conn, 15)]
    if jobs is None:
        jobs = {name: job_state(label) for name, label in JOBS.items()}
    now = time.time()
    return {
        "now": store.utcnow_iso(),
        "panel": console_state(cfg),
        "mode": "autocut" if cfg.autocut.enabled else "scoring-only",
        "safety_tripped": store.kv_get(conn, store.SAFETY_KEY),
        "batch_mode": cfg.models.batch,
        "running": None
        if proc is None
        else {**proc, "elapsed": now - proc["started"], "phase": snap.get("phase") or ""},
        "jobs": jobs,
        "launchctl": launchctl_available(),
        "next_run_in": _next_run_in(proc, jobs, runs, now),
        "config": _config_summary(cfg),
        "pile": {
            "unread": snap["unread"],
            "judged": snap["judged_unread"],
            "pending": snap["pending"],
            "inflight": snap["inflight"],
            "too_old": snap["too_old"],
            "stand_in": snap.get("stand_in", 0),
            "rejudge": snap.get("rejudge", 0),
            "low_conf": snap["low_conf"],
        },
        "cuts_pending": {"untagged": len(untagged), "unmarked": len(unmarked)},
        "spend": {
            "today": store.spent_since(conn, store.local_midnight_iso(cfg.timezone)),
            "week": snap["week"]["spend"],
            "all": snap["all_time"]["spend"],
            "per_1000": snap["per_1000"],
            "cache_hit": snap["cache_hit"],
        },
        "today": snap["today"],
        "rate_per_min": snap.get("rate_per_min", 0.0),
        "eta_min": snap.get("eta_min"),
        "runs": runs,
        "last_ok": next((r for r in runs if r["ok"] and r["kind"] == "run"), None),
        "last_error": next((r for r in runs if not r["ok"] and (r["error"] or r["finished_at"])), None),
        "batches": snap["batches"],
        "log": progress.log_tail(logs.log_file(data_dir), 10),  # local only: the console never leaves the Mac
    }


def _next_run_in(
    proc: dict[str, Any] | None, jobs: dict[str, Any], runs: list[dict[str, Any]], now: float
) -> float | None:
    """Seconds until launchd fires the run job again, from the last timer run (a prune or an
    undo is not the timer); None while a run is going or the job is not loaded."""
    if proc is not None or not jobs["run"]["loaded"] or jobs["run"]["state"] == "running":
        return None
    interval = jobs["run"]["interval"] or 1800
    last_timer_run = next((r for r in runs if r["kind"] == "run"), None)
    last_start = _parse_ts(last_timer_run["started_at"]) if last_timer_run else None
    return max(0.0, interval - (now - last_start)) if last_start else None


def _config_summary(cfg: Config) -> dict[str, Any]:
    return {
        "judge": cfg.models.judge,
        "reviewer": cfg.models.reviewer,
        "profiler": cfg.models.profiler,
        "budget": cfg.models.daily_budget_usd,
        "target": cfg.prune.keep,
        "floor": cfg.autocut.cutoff,
        "max_age_days": cfg.limits.max_age_days,
        "folders": {k: v.model_dump() for k, v in cfg.folders.items()},
        "root": str(cfg.root),
        "heartbeat": bool(os.environ.get("HEALTHCHECK_URL")),
    }


def _parse_ts(iso: str | None) -> float | None:
    if not iso:
        return None
    from datetime import datetime, timezone

    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.timestamp()


def render_status_text(st: dict[str, Any]) -> str:
    """The whole picture for the terminal (`bodh status`)."""
    lines = [*_status_state_lines(st), *_status_schedule_lines(st), *_status_work_lines(st)]
    if st["last_error"]:
        e = st["last_error"]
        lines.append(
            f"last problem: {(e['started_at'] or '')[:16]} {e['kind']}: {(e['error'] or e['summary'] or '')[:140]}"
        )
    lines += ["", "last runs:"]
    for r in st["runs"][:10]:
        ok = "ok " if r["ok"] else ("…  " if not r["finished_at"] else "ERR")
        lines.append(
            f"  {(r['started_at'] or '')[:16]} {ok} {store.run_kind_name(r['kind']):<10} {(r['summary'] or r['error'] or '')[:150]}"
        )
    return "\n".join(lines)


def _status_state_lines(st: dict[str, Any]) -> list[str]:
    r = st["running"]
    if r:
        first = f"● {r['kind']} RUNNING for {r['elapsed'] / 60:.1f} min (pid {r['pid']}) · {r['phase'] or 'working'}"
    else:
        nxt = st["next_run_in"]
        when = (
            f"next scheduled run in about {nxt / 60:.0f} min"
            if nxt is not None
            else "no scheduled run pending"
        )
        first = f"○ idle · {when}"
    mode = " (nothing is cut)" if st["mode"] == "scoring-only" else " (autocut cuts below the cut-off)"
    lines = [
        first,
        f"mode: {st['mode']}{mode} · {'batch' if st['batch_mode'] else 'synchronous'} judging · judge {st['config']['judge']}",
    ]
    if st.get("safety_tripped"):
        lines.append(
            f"SAFETY LIMIT TRIPPED in run {st['safety_tripped']}: autocut cuts nothing until `bodh safety reset`"
        )
    return lines


def _status_schedule_lines(st: dict[str, Any]) -> list[str]:
    j = st["jobs"]

    def jstate(name: str) -> str:
        s = j[name]
        if not s["installed"]:
            return "not installed"
        extra = f", last exit {s['last_exit']}" if s["last_exit"] not in (None, 0) else ""
        return f"{s['state']}{extra}"

    lines = [
        f"schedule: run {jstate('run')} · weekly {jstate('weekly')} · report {jstate('report')} · console {jstate('console')}"
    ]
    pn = st.get("panel") or {}
    if pn.get("up"):
        lines.append(f"console: up at {pn['url']}")
    else:
        why = f" · last: {pn['note']}" if pn.get("note") else ""
        lines.append(
            f"console: not answering at {pn.get('url', 'http://127.0.0.1:8766')}{why} · "
            "start it with `bodh console` (foreground) or ./install-launchd.sh"
        )
    return lines


def _status_work_lines(st: dict[str, Any]) -> list[str]:
    p = st["pile"]
    lines = [
        f"unread {p['unread']:,} · judged {p['judged']:,} · with the model {p['inflight']:,} · pending {p['pending']:,}"
        + (f" · {p['too_old']:,} older than the window" if p["too_old"] else "")
        + (f" · {p['rejudge']:,} await the judge's second reading" if p["rejudge"] else "")
        + f" · prune keeps {st['config']['target']} · cut-off {st['config']['floor']}"
    ]
    c = st["cuts_pending"]
    if c["untagged"] or c["unmarked"]:
        lines.append(
            f"cuts not yet written to BazQux: {c['untagged']:,} to tag, {c['unmarked']:,} to mark read "
            "(the next run finishes them)"
        )
    s = st["spend"]
    per = f"${s['per_1000']:.2f}/1,000" if s["per_1000"] is not None else "–"
    cache = f"{s['cache_hit']:.0%}" if s["cache_hit"] is not None else "–"
    lines.append(
        f"spend: today ${s['today']:.2f} of ${st['config']['budget']:.2f} · 7 days ${s['week']:.2f} · "
        f"all time ${s['all']:.2f} · {per} · cache hits {cache}"
    )
    t = st["today"]
    lines.append(f"today: arrived {t['arrived']:,} · judged {t['judged']:,} · cut {t['cut']:,}")
    return lines
