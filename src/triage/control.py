"""The whole system in one view, and the levers: what is running, what launchd will do
next, what the last runs did, the pile, the spend; start a run, stop it, pause or resume
the schedule, flip enforce, prune, undo. Used by `triage status`-style commands and by
the control room (`triage panel`)."""

from __future__ import annotations

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

import yaml

from . import ops, progress, store
from .config import Config

log = logging.getLogger(__name__)

JOBS = {
    "run": "com.antriksh.triage",
    "weekly": "com.antriksh.triage.weekly",
    "report": "com.antriksh.triage.report",
    "panel": "com.antriksh.triage.panel",
}


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


def job_state(label: str) -> dict:
    """installed, loaded, state ('running', 'waiting', 'not loaded', 'not installed', 'unknown'),
    last exit code, pid, next fire interval, as launchctl reports them."""
    out = {"label": label, "installed": False, "loaded": False, "state": "not installed", "last_exit": None,
           "pid": None, "mode": "", "plist": None, "interval": None}
    path, mode = _plist_path(label)
    if path is None:
        return out
    out.update(installed=True, mode=mode, plist=str(path))
    if not launchctl_available():
        out["state"] = "unknown (no launchctl here)"
        return out
    try:
        res = subprocess.run(["launchctl", "print", f"{_domain(mode)}/{label}"], capture_output=True, text=True,
                             timeout=10)
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
            proc = ops.running_process(data_dir)
            if proc is not None:
                return (f"not kicked: {proc['kind']} (pid {proc['pid']}) is running; wait for it or `triage stop` "
                        "it first")
    names = list(JOBS) if which == "all" else [which]
    lines = [_schedule_one(action, name) for name in names]
    if action == "pause" and which == "all":
        lines.append("the control room is paused too; `triage schedule resume` brings everything back")
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
    if action == "pause":
        res = subprocess.run(["launchctl", "bootout", f"{dom}/{label}"], capture_output=True, text=True, timeout=20)
        return f"paused {label}" if res.returncode == 0 else f"{label} already paused ({res.stderr.strip() or 'not loaded'})"
    if action == "resume":
        res = subprocess.run(["launchctl", "bootstrap", dom, str(path)], capture_output=True, text=True, timeout=20)
        return f"resumed {label}" if res.returncode == 0 else f"could not load {label}: {res.stderr.strip() or res.stdout.strip()}"
    if action == "kick":
        res = subprocess.run(["launchctl", "kickstart", "-k", f"{dom}/{label}"], capture_output=True, text=True, timeout=20)
        return f"started {label} now" if res.returncode == 0 else f"could not start {label}: {res.stderr.strip()}"
    return f"unknown action {action}"


# -- the control room's own state ------------------------------------------------

def panel_state(cfg: Config) -> dict:
    """Is the control room answering? From data/panel.json (written by `triage panel`) and a
    one-second connection attempt; the last line of data/panel.log explains a failure."""
    data_dir = cfg.root / cfg.data_dir
    info: dict = {}
    try:
        info = json.loads((data_dir / "panel.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    host, port = info.get("host", "127.0.0.1"), int(info.get("port", 8766))
    try:
        with socket.create_connection((host, port), timeout=1.0):
            up = True
    except OSError:
        up = False
    note = ""
    try:
        lines = (data_dir / "panel.log").read_text(encoding="utf-8").splitlines()
        note = lines[-1] if lines else ""
    except OSError:
        pass
    return {"up": up, "url": f"http://{host}:{port}", "pid": info.get("pid"), "note": note}


def stray_processes(root: Path | None = None) -> list[tuple[int, str]]:
    """triage commands of this checkout still alive on this machine (run, prune, blast,
    panel …), except this one. Anchored to the checkout's path: a halt from one checkout must
    not end another's control room, or an editor whose arguments mention the path."""
    try:
        res = subprocess.run(["pgrep", "-fl", stray_pattern(root)], capture_output=True, text=True, timeout=10)
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
    # a console script runs as `<venv>/bin/python <venv>/bin/triage …` (its shebang), so the
    # interpreter may come first
    if root is not None:
        return (rf"^(\S+/python\S* )?{re.escape(str(root))}/\.venv/bin/triage( |$)"
                r"|^\S*python\S* -m triage\.cli( |$)")
    return r"^(\S+/python\S* )?\S*/\.venv/bin/triage( |$)|^\S*python\S* -m triage\.cli( |$)"


def halt(cfg: Config) -> list[str]:
    """Stop everything: interrupt the run that holds the lock (so it finishes its run row and
    pings the heartbeat), then pause every launchd job, then end any other triage process of
    this checkout (the control room included). Nothing is lost: judged articles stay judged,
    half-written cuts are finished by the next run."""
    notes = [ops.stop_process(cfg.root / cfg.data_dir)]
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
    try:
        (cfg.root / cfg.data_dir / "panel.json").unlink()
    except OSError:
        pass
    notes.append("everything is stopped; `triage schedule resume` starts the jobs again")
    return notes


# -- config levers --------------------------------------------------------------

def set_enforce(root: Path, on: bool) -> tuple[bool, str]:
    """Flip `cut.enforce` in config.yaml in place, keeping every other line (and every
    comment) as it is. The key is found under the `cut:` section whatever its spelling
    (true, True, yes, "false" …), added there when missing, and the result is read back:
    the edit counts only if the loaded configuration differs in that one value and nothing
    else. Returns (ok, message)."""
    path = root / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8")
        before = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError) as exc:
        return False, f"config.yaml could not be read: {exc}"
    if not isinstance(before, dict):
        return False, "config.yaml does not hold a mapping of sections; edit it by hand"
    new_text = _edit_cut_enforce(text, on)
    try:
        after = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as exc:
        return False, f"the edit would break config.yaml ({exc}); edit it by hand"
    expected = copy.deepcopy(before)
    expected.setdefault("cut", {})
    if not isinstance(expected["cut"], dict):
        return False, "the `cut:` section is not a mapping; edit config.yaml by hand"
    expected["cut"]["enforce"] = on
    if after != expected:
        return False, "could not find a place for cut.enforce in config.yaml; edit it by hand"
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return False, f"config.yaml could not be written: {exc}"
    return True, f"cut.enforce is now {'true: the floor cuts from the next run' if on else 'false: shadow mode, nothing is cut'}"


_SECTION_RE = re.compile(r"^(\w[\w-]*):\s*(#.*)?$")                      # a top-level `name:` line
_ENFORCE_RE = re.compile(r"^(?P<indent>\s+)(?P<key>['\"]?enforce['\"]?:)(?P<rest>.*)$")


def _edit_cut_enforce(text: str, on: bool) -> str:
    """The text of config.yaml with cut.enforce set, by lines: the `enforce:` line directly
    under `cut:` is rewritten (its trailing comment kept), inserted after `cut:` when there is
    none, or a `cut:` section is appended when there is none."""
    value = "true" if on else "false"
    lines = text.splitlines(keepends=True)
    cut_at = None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line.rstrip("\n"))
        if m and m.group(1) == "cut":
            cut_at = i
            break
        if line.startswith("cut:"):
            return text  # `cut: { ... }` on one line: not a shape to edit blind; the caller's check refuses
    if cut_at is None:
        tail = "" if not text or text.endswith("\n") else "\n"
        return text + f"{tail}\ncut:\n  enforce: {value}\n"
    end = len(lines)
    for i in range(cut_at + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#") and not lines[i][0].isspace():
            end = i  # the next top-level key
            break
    child_indent: int | None = None
    for i in range(cut_at + 1, end):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(lines[i]) - len(lines[i].lstrip())
        if child_indent is None:
            child_indent = indent
        m = _ENFORCE_RE.match(lines[i].rstrip("\n"))
        if m and indent == child_indent:
            rest = m.group("rest")
            comment = ""
            hash_at = rest.find(" #")
            if hash_at >= 0:
                comment = rest[hash_at:]
            lines[i] = f"{m.group('indent')}enforce: {value}{comment}\n"
            return "".join(lines)
    indent = " " * (child_indent if child_indent else 2)
    lines.insert(cut_at + 1, f"{indent}enforce: {value}\n")
    return "".join(lines)


# -- the whole picture ------------------------------------------------------------

def status(cfg: Config, conn: sqlite3.Connection, jobs: dict | None = None) -> dict:
    """The whole picture. ``jobs`` lets a caller pass launchd's answers from a cache."""
    data_dir = cfg.root / cfg.data_dir
    proc = ops.running_process(data_dir)
    snap = progress.snapshot(conn, cfg)
    untagged, unmarked = store.cut_pending(conn)
    runs = [dict(r) for r in store.last_runs(conn, 15)]
    last_ok = next((r for r in runs if r["ok"] and r["kind"] == "run"), None)
    last_err = next((r for r in runs if not r["ok"] and (r["error"] or r["finished_at"])), None)
    if jobs is None:
        jobs = {name: job_state(label) for name, label in JOBS.items()}
    spend_today = store.spent_since(conn, store.local_midnight_iso(cfg.timezone))
    now = time.time()
    next_run = None
    if proc is None and jobs["run"]["loaded"] and jobs["run"]["state"] != "running":
        interval = jobs["run"]["interval"] or 1800
        last_timer_run = next((r for r in runs if r["kind"] == "run"), None)  # a prune or an undo is not the timer
        last_start = _parse_ts(last_timer_run["started_at"]) if last_timer_run else None
        if last_start:
            next_run = max(0.0, interval - (now - last_start))
    return {
        "now": store.utcnow_iso(),
        "panel": panel_state(cfg),
        "mode": "enforce" if cfg.cut.enforce else "shadow",
        "breaker_tripped": store.kv_get(conn, "breaker_tripped"),
        "batch_mode": cfg.models.batch,
        "running": None if proc is None else {**proc, "elapsed": now - proc["started"], "phase": snap.get("phase") or ""},
        "jobs": jobs,
        "launchctl": launchctl_available(),
        "next_run_in": next_run,
        "config": {
            "teacher": cfg.models.teacher, "second_look": cfg.models.second_look, "learner": cfg.models.learner,
            "budget": cfg.models.daily_budget_usd, "target": cfg.cut.target_unread, "floor": cfg.cut.default_floor,
            "max_age_days": cfg.scoring.max_age_days, "folders": {k: v.model_dump() for k, v in cfg.cut.folders.items()},
            "root": str(cfg.root), "heartbeat": bool(os.environ.get("HEALTHCHECK_URL")),
        },
        "pile": {
            "unread": snap["unread"], "judged": snap["judged_unread"], "pending": snap["pending"],
            "inflight": snap["inflight"], "too_old": snap["too_old"], "stand_in": snap.get("stand_in", 0),
            "rejudge": snap.get("rejudge", 0), "low_conf": snap["low_conf"],
        },
        "cuts_pending": {"untagged": len(untagged), "unmarked": len(unmarked)},
        "spend": {"today": spend_today, "week": snap["week"]["spend"], "all": snap["all_time"]["spend"],
                  "per_1000": snap["per_1000"], "cache_hit": snap["cache_hit"]},
        "today": snap["today"],
        "rate_per_min": snap.get("rate_per_min", 0.0),
        "eta_min": snap.get("eta_min"),
        "runs": runs,
        "last_ok": last_ok,
        "last_error": last_err,
        "batches": snap["batches"],
        "log": progress.log_tail(ops.log_file(data_dir), 10),  # local only: the control room never leaves the Mac
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


def render_status_text(st: dict) -> str:
    """The whole picture for the terminal (`triage status`)."""
    lines = []
    r = st["running"]
    if r:
        lines.append(f"● {r['kind']} RUNNING for {r['elapsed'] / 60:.1f} min (pid {r['pid']}) · {r['phase'] or 'working'}")
    else:
        nxt = st["next_run_in"]
        when = f"next scheduled run in about {nxt / 60:.0f} min" if nxt is not None else "no scheduled run pending"
        lines.append(f"○ idle · {when}")
    lines.append(f"mode: {st['mode']}{' (nothing is cut)' if st['mode'] == 'shadow' else ' (the floor cuts)'} · "
                 f"{'batch' if st['batch_mode'] else 'synchronous'} judging · teacher {st['config']['teacher']}")
    if st.get("breaker_tripped"):
        lines.append(f"BREAKER TRIPPED in run {st['breaker_tripped']}: the floor cuts nothing until `triage safety reset`")
    j = st["jobs"]
    def jstate(name: str) -> str:
        s = j[name]
        if not s["installed"]:
            return "not installed"
        extra = f", last exit {s['last_exit']}" if s["last_exit"] not in (None, 0) else ""
        return f"{s['state']}{extra}"
    lines.append(f"schedule: run {jstate('run')} · weekly {jstate('weekly')} · report {jstate('report')} · panel {jstate('panel')}")
    pn = st.get("panel") or {}
    if pn.get("up"):
        lines.append(f"control room: up at {pn['url']}")
    else:
        why = f" · last: {pn['note']}" if pn.get("note") else ""
        lines.append(f"control room: not answering at {pn.get('url', 'http://127.0.0.1:8766')}{why} · "
                     "start it with `triage panel` (foreground) or ./install-launchd.sh")
    p = st["pile"]
    lines.append(f"pile: unread {p['unread']:,} · judged {p['judged']:,} · in flight {p['inflight']:,} · pending {p['pending']:,}"
                 + (f" · {p['too_old']:,} older than the window" if p["too_old"] else "")
                 + (f" · {p['rejudge']:,} await the teacher's re-judgment" if p["rejudge"] else "")
                 + f" · target {st['config']['target']} · floor {st['config']['floor']}")
    c = st["cuts_pending"]
    if c["untagged"] or c["unmarked"]:
        lines.append(f"cuts not yet written to BazQux: {c['untagged']:,} to tag, {c['unmarked']:,} to mark read "
                     "(the next run finishes them)")
    s = st["spend"]
    per = f"${s['per_1000']:.2f}/1,000" if s["per_1000"] is not None else "–"
    cache = f"{s['cache_hit']:.0%}" if s["cache_hit"] is not None else "–"
    lines.append(f"spend: today ${s['today']:.2f} of ${st['config']['budget']:.2f} · 7 days ${s['week']:.2f} · "
                 f"all time ${s['all']:.2f} · {per} · cache hits {cache}")
    t = st["today"]
    lines.append(f"today: arrived {t['arrived']:,} · judged {t['judged']:,} · cut {t['cut']:,}")
    if st["last_error"]:
        e = st["last_error"]
        lines.append(f"last problem: {(e['started_at'] or '')[:16]} {e['kind']}: {(e['error'] or e['summary'] or '')[:140]}")
    lines.append("")
    lines.append("last runs:")
    for r in st["runs"][:10]:
        ok = "ok " if r["ok"] else ("…  " if not r["finished_at"] else "ERR")
        lines.append(f"  {(r['started_at'] or '')[:16]} {ok} {r['kind']:<10} {(r['summary'] or r['error'] or '')[:150]}")
    return "\n".join(lines)
