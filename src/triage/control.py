"""The whole system in one view, and the levers: what is running, what launchd will do
next, what the last runs did, the pile, the spend; start a run, stop it, pause or resume
the schedule, flip enforce, prune, undo. Used by `triage status`-style commands and by
the control room (`triage panel`)."""

from __future__ import annotations

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


def schedule(action: str, which: str = "all") -> str:
    """'pause' unloads the jobs (the plists stay; nothing fires until 'resume'); 'resume' loads
    them; 'kick' starts a run now. ``which`` is a job name or 'all' (pause and resume only)."""
    if action == "kick":
        which = "run" if which == "all" else which
    names = list(JOBS) if which == "all" else [which]
    return "\n".join(_schedule_one(action, name) for name in names)


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


def stray_processes() -> list[tuple[int, str]]:
    """triage commands still alive on this machine (run, prune, blast, panel …), except this one."""
    try:
        res = subprocess.run(["pgrep", "-fl", r"\.venv/bin/triage|-m triage\.cli"], capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in res.stdout.splitlines():
        pid_s, _, cmd = line.partition(" ")
        if pid_s.isdigit() and int(pid_s) != os.getpid():
            out.append((int(pid_s), cmd.strip()))
    return out


def halt(cfg: Config) -> list[str]:
    """Stop everything: pause every launchd job, interrupt the run that holds the lock, end
    any other triage process (the control room included). Nothing is lost: judged articles
    stay judged, half-written cuts are finished by the next run."""
    notes = [schedule("pause", "all")]
    notes.append(ops.stop_process(cfg.root / cfg.data_dir))
    for pid, cmd in stray_processes():
        try:
            os.kill(pid, signal.SIGTERM)
            notes.append(f"ended pid {pid}: {cmd[:80]}")
        except OSError as exc:
            notes.append(f"could not end pid {pid}: {exc}")
    time.sleep(1.0)
    for pid, cmd in stray_processes():
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

def set_enforce(root: Path, on: bool) -> str:
    """Flip `cut.enforce` in config.yaml in place, keeping every other line as it is."""
    path = root / "config.yaml"
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(r"^(\s*enforce:\s*)(true|false)", lambda m: f"{m.group(1)}{'true' if on else 'false'}", text,
                     count=1, flags=re.M)
    if n == 0:
        return "config.yaml has no `enforce:` line under cut"
    path.write_text(new, encoding="utf-8")
    return f"cut.enforce is now {'true: the floor cuts from the next run' if on else 'false: shadow mode, nothing is cut'}"


# -- the whole picture ------------------------------------------------------------

def status(cfg: Config, conn: sqlite3.Connection) -> dict:
    data_dir = cfg.root / cfg.data_dir
    proc = ops.running_process(data_dir)
    snap = progress.snapshot(conn, cfg)
    untagged, unmarked = store.cut_pending(conn)
    runs = [dict(r) for r in store.last_runs(conn, 15)]
    last_ok = next((r for r in runs if r["ok"] and r["kind"] == "run"), None)
    last_err = next((r for r in runs if not r["ok"] and (r["error"] or r["finished_at"])), None)
    jobs = {name: job_state(label) for name, label in JOBS.items()}
    spend_today = store.spent_since(conn, store.local_midnight_iso(cfg.timezone))
    now = time.time()
    next_run = None
    if proc is None and jobs["run"]["loaded"] and jobs["run"]["state"] != "running":
        interval = jobs["run"]["interval"] or 1800
        last_start = _parse_ts(runs[0]["started_at"]) if runs else None
        if last_start:
            next_run = max(0.0, interval - (now - last_start))
    return {
        "now": store.utcnow_iso(),
        "panel": panel_state(cfg),
        "mode": "enforce" if cfg.cut.enforce else "shadow",
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
        "log": snap.get("log", []),
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
