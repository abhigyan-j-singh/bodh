"""`bodh schedule install|uninstall`: the four jobs (the run every 30 minutes, the Sunday
rewrite, the monthly report, the console kept alive) as launchd agents on a Mac, systemd user
units on Linux, or one Task Scheduler entry on Windows that runs `bodh serve` at logon. Every
entry runs this very interpreter (`python -m bodh.cli ...`), so pipx, uv, a venv and a checkout
all schedule the same way. `dry_run` returns what would be written and run, and changes nothing."""

from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import BodhError
from .jobs import JOBS, OLD_JOBS

log = logging.getLogger(__name__)

Runner = Callable[[list[str]], tuple[int, str]]
RUN_EVERY_SECONDS = 1800
CONSOLE_PORT = 8766


def kind_for(platform: str = sys.platform) -> str:
    if platform == "darwin":
        return "launchd"
    if platform.startswith("linux"):
        return "systemd"
    if platform == "win32":
        return "taskschd"
    raise BodhError(f"no scheduler known for {platform}; `bodh serve` runs the schedule in one process")


def command(*args: str) -> list[str]:
    """The program every job runs: this interpreter, the package, the command."""
    return [sys.executable, "-m", "bodh.cli", *args]


def run_command(args: list[str]) -> tuple[int, str]:
    try:
        res = subprocess.run(args, capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return res.returncode, (res.stdout + res.stderr).strip()


def install(
    root: Path,
    kind: str | None = None,
    dry_run: bool = False,
    runner: Runner = run_command,
    home: Path | None = None,
) -> list[str]:
    """Write the entries for this machine and load them; returns one line per thing done."""
    kind = kind or kind_for()
    home = home or Path.home()
    if kind == "launchd":
        return _launchd_install(root, dry_run, runner, home)
    if kind == "systemd":
        return _systemd_install(root, dry_run, runner, home)
    if kind == "taskschd":
        return _taskschd_install(root, dry_run, runner)
    raise BodhError(f"unknown scheduler {kind!r}: launchd, systemd or taskschd")


def uninstall(
    root: Path,
    kind: str | None = None,
    dry_run: bool = False,
    runner: Runner = run_command,
    home: Path | None = None,
) -> list[str]:
    kind = kind or kind_for()
    home = home or Path.home()
    if kind == "launchd":
        return _launchd_uninstall(dry_run, runner, home)
    if kind == "systemd":
        return _systemd_uninstall(dry_run, runner, home)
    if kind == "taskschd":
        return _taskschd_uninstall(dry_run, runner)
    raise BodhError(f"unknown scheduler {kind!r}: launchd, systemd or taskschd")


def _do(runner: Runner, args: list[str], dry_run: bool, lines: list[str], ok_text: str) -> bool:
    if dry_run:
        lines.append("would run: " + " ".join(args))
        return True
    code, out = runner(args)
    if code != 0:
        lines.append(f"failed ({code}): {' '.join(args)}" + (f": {out}" if out else ""))
        return False
    lines.append(ok_text)
    return True


def _write(path: Path, text: str, dry_run: bool, lines: list[str]) -> None:
    if dry_run:
        lines.append(f"would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    lines.append(f"wrote {path}")


# -- launchd ---------------------------------------------------------------------------


def launchd_plist(label: str, root: Path, home: Path) -> dict[str, Any]:
    """One job's plist as a dict: the same keys the templates in launchd/ carry."""
    logs = home / "Library" / "Logs" / "bodh"
    name = label.rsplit(".", 1)[-1]
    args = {
        "run": command("run"),
        "weekly": command("weekly"),
        "report": command("report"),
        "console": command("console", "--port", str(CONSOLE_PORT)),
    }[name]
    plist: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": args,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "BODH_HOME": str(root),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(home),
        },
        "StandardOutPath": str(logs / f"{name}.out.log"),
        "StandardErrorPath": str(logs / f"{name}.err.log"),
        "ProcessType": "Background",
    }
    if name == "run":
        plist.update(
            {"StartInterval": RUN_EVERY_SECONDS, "RunAtLoad": True, "KeepAlive": False, "LowPriorityIO": True}
        )
    elif name == "weekly":
        plist.update({"StartCalendarInterval": {"Weekday": 0, "Hour": 7, "Minute": 0}, "RunAtLoad": False})
    elif name == "report":
        plist.update({"StartCalendarInterval": {"Day": 1, "Hour": 7, "Minute": 30}, "RunAtLoad": False})
    else:
        plist.update({"KeepAlive": True, "RunAtLoad": True, "ThrottleInterval": 10})
    return plist


def _launchd_install(root: Path, dry_run: bool, runner: Runner, home: Path) -> list[str]:
    lines: list[str] = []
    agents = home / "Library" / "LaunchAgents"
    domain = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/501"
    if not dry_run:
        (home / "Library" / "Logs" / "bodh").mkdir(parents=True, exist_ok=True)
    for old in OLD_JOBS:  # an install from before the rename
        if (agents / f"{old}.plist").exists():
            _do(
                runner,
                ["launchctl", "bootout", f"{domain}/{old}"],
                dry_run,
                lines,
                f"unloaded the old job {old}",
            )
            if not dry_run:
                (agents / f"{old}.plist").unlink(missing_ok=True)
    for label in JOBS.values():
        path = agents / f"{label}.plist"
        _do(runner, ["launchctl", "bootout", f"{domain}/{label}"], dry_run, [], "")  # whatever was there
        plist = launchd_plist(label, root, home)
        _write(path, plistlib.dumps(plist, sort_keys=False).decode(), dry_run, lines)
        if dry_run:
            lines.append("  it runs: " + " ".join(plist["ProgramArguments"]))
        _do(runner, ["launchctl", "bootstrap", domain, str(path)], dry_run, lines, f"loaded {label}")
    lines.append(f"the jobs run while you are logged in; see them with: launchctl print {domain}/bodh.run")
    return lines


def _launchd_uninstall(dry_run: bool, runner: Runner, home: Path) -> list[str]:
    lines: list[str] = []
    agents = home / "Library" / "LaunchAgents"
    domain = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/501"
    for label in [*JOBS.values(), *OLD_JOBS]:
        path = agents / f"{label}.plist"
        if not path.exists():
            continue
        _do(runner, ["launchctl", "bootout", f"{domain}/{label}"], dry_run, lines, f"unloaded {label}")
        if dry_run:
            lines.append(f"would remove {path}")
        else:
            path.unlink(missing_ok=True)
            lines.append(f"removed {path}")
    if not lines:
        lines.append("no bodh jobs installed")
    return lines


# -- systemd (user units) -------------------------------------------------------------------


def systemd_units(root: Path) -> dict[str, str]:
    """File name -> contents for the user units: three timers with their services, and the console."""
    env = f"Environment=BODH_HOME={root}\n"
    units: dict[str, str] = {}

    def service(name: str, args: list[str], extra: str = "") -> str:
        exe = " ".join(args)
        return (
            f"[Unit]\nDescription=bodh {name}\nAfter=network-online.target\n\n[Service]\nType=simple\n"
            f"WorkingDirectory={root}\n{env}ExecStart={exe}\n{extra}"
        )

    units["bodh-run.service"] = service("run", command("run"))
    units["bodh-run.timer"] = (
        "[Unit]\nDescription=bodh: one run every 30 minutes\n\n[Timer]\nOnBootSec=2min\n"
        f"OnUnitActiveSec={RUN_EVERY_SECONDS // 60}min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
    )
    units["bodh-weekly.service"] = service("weekly", command("weekly"))
    units["bodh-weekly.timer"] = (
        "[Unit]\nDescription=bodh: the Sunday profile rewrite\n\n[Timer]\nOnCalendar=Sun *-*-* 07:00:00\n"
        "Persistent=true\n\n[Install]\nWantedBy=timers.target\n"
    )
    units["bodh-report.service"] = service("report", command("report"))
    units["bodh-report.timer"] = (
        "[Unit]\nDescription=bodh: the monthly feed report\n\n[Timer]\nOnCalendar=*-*-01 07:30:00\n"
        "Persistent=true\n\n[Install]\nWantedBy=timers.target\n"
    )
    units["bodh-console.service"] = service(
        "console",
        command("console", "--port", str(CONSOLE_PORT)),
        "Restart=always\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n",
    )
    return units


def _systemd_install(root: Path, dry_run: bool, runner: Runner, home: Path) -> list[str]:
    lines: list[str] = []
    if not dry_run and shutil.which("systemctl") is None:
        raise BodhError("systemctl is not on this machine; `bodh serve` runs the schedule in one process")
    unit_dir = home / ".config" / "systemd" / "user"
    for name, text in systemd_units(root).items():
        _write(unit_dir / name, text, dry_run, lines)
        if dry_run and "ExecStart=" in text:
            lines.append("  " + next(line for line in text.splitlines() if line.startswith("ExecStart=")))
    _do(runner, ["systemctl", "--user", "daemon-reload"], dry_run, lines, "systemd reloaded")
    for unit in ("bodh-run.timer", "bodh-weekly.timer", "bodh-report.timer", "bodh-console.service"):
        _do(runner, ["systemctl", "--user", "enable", "--now", unit], dry_run, lines, f"enabled {unit}")
    lines.append(
        "the timers run while you are logged in; `loginctl enable-linger` keeps them running after logout"
    )
    return lines


def _systemd_uninstall(dry_run: bool, runner: Runner, home: Path) -> list[str]:
    lines: list[str] = []
    unit_dir = home / ".config" / "systemd" / "user"
    present = [name for name in systemd_units(Path("/")) if (unit_dir / name).exists()]
    if not present:
        return ["no bodh units installed"]
    for unit in ("bodh-run.timer", "bodh-weekly.timer", "bodh-report.timer", "bodh-console.service"):
        if unit in present:
            _do(runner, ["systemctl", "--user", "disable", "--now", unit], dry_run, lines, f"disabled {unit}")
    for name in present:
        if dry_run:
            lines.append(f"would remove {unit_dir / name}")
        else:
            (unit_dir / name).unlink(missing_ok=True)
            lines.append(f"removed {unit_dir / name}")
    _do(runner, ["systemctl", "--user", "daemon-reload"], dry_run, lines, "systemd reloaded")
    return lines


# -- Windows Task Scheduler -------------------------------------------------------------


TASK_NAME = "bodh"


def _taskschd_install(root: Path, dry_run: bool, runner: Runner) -> list[str]:
    """One task at logon: `bodh serve`, which runs the schedule and the console in one process."""
    lines: list[str] = []
    exe = " ".join(f'"{a}"' if " " in a else a for a in command("serve"))
    args = [
        "schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "ONLOGON", "/RL", "LIMITED",
        "/TR", f'cmd /c "cd /d "{root}" && {exe}"',
    ]  # fmt: skip
    _do(runner, args, dry_run, lines, f"created the task {TASK_NAME} (bodh serve at logon)")
    _do(runner, ["schtasks", "/Run", "/TN", TASK_NAME], dry_run, lines, f"started {TASK_NAME}")
    lines.append("see it in Task Scheduler, or: schtasks /Query /TN bodh")
    return lines


def _taskschd_uninstall(dry_run: bool, runner: Runner) -> list[str]:
    lines: list[str] = []
    _do(runner, ["schtasks", "/End", "/TN", TASK_NAME], dry_run, [], "")
    _do(
        runner,
        ["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
        dry_run,
        lines,
        f"removed the task {TASK_NAME}",
    )
    return lines
