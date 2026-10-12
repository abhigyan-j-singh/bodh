"""`bodh upgrade` and `bodh version --check`: which channel this install came through (pipx, uv,
Homebrew, a one-file executable, a checkout, plain pip), the command that upgrades it, and the
newest version on PyPI."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import __version__

log = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/bodh/json"
PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass
class Channel:
    name: str
    command: list[str] | None  # None: nothing to run, the note says what to do
    note: str

    @property
    def text(self) -> str:
        return " ".join(self.command) if self.command else self.note


def channel(executable: str = sys.executable, frozen: bool = getattr(sys, "frozen", False)) -> Channel:
    """The install channel, from where the interpreter and the package live."""
    exe = executable.replace("\\", "/")
    package = str(PACKAGE_DIR).replace("\\", "/")
    if frozen:
        return Channel("binary", None, "a one-file executable: download the new one from the releases page")
    if "/pipx/venvs/" in exe or "/pipx/venvs/" in package:
        return Channel("pipx", ["pipx", "upgrade", "bodh"], "installed with pipx")
    if "/uv/tools/" in exe or "/uv/tools/" in package:
        return Channel("uv", ["uv", "tool", "upgrade", "bodh"], "installed with uv")
    if "/Cellar/" in exe or "/Cellar/" in package or "/homebrew/" in package.lower():
        return Channel("brew", ["brew", "upgrade", "bodh"], "installed with Homebrew")
    if (PACKAGE_DIR.parents[1] / ".git").exists() and PACKAGE_DIR.parent.name == "src":
        root = PACKAGE_DIR.parents[1]
        return Channel(
            "checkout",
            ["git", "-C", str(root), "pull", "--ff-only"],
            "a checkout: pull, then `./install.sh` (or `pip install -e .`) if dependencies changed",
        )
    return Channel("pip", [executable, "-m", "pip", "install", "--upgrade", "bodh"], "installed with pip")


def newest_on_pypi(timeout: float = 5.0) -> str | None:
    """The newest release on PyPI, or None when PyPI cannot be reached or bodh is not there."""
    try:
        response = httpx.get(PYPI_URL, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.debug("upgrade: pypi unreachable: %s", exc)
        return None
    if response.status_code != 200:
        return None
    try:
        version = response.json()["info"]["version"]
    except (ValueError, KeyError, TypeError):
        return None
    return str(version)


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version)[:3])


def is_newer(candidate: str, current: str = __version__) -> bool:
    return _key(candidate) > _key(current)


def check_text(newest: str | None, current: str = __version__) -> str:
    """The line `bodh version --check` prints after the version."""
    if newest is None:
        return "newest on PyPI: unknown (not published, or PyPI unreachable)"
    if is_newer(newest, current):
        return f"newest on PyPI: {newest} (`bodh upgrade` shows how to get it)"
    return f"newest on PyPI: {newest}; this is the newest"


def run(ch: Channel) -> int:
    """Run the channel's upgrade command, inheriting the terminal; its exit code."""
    if ch.command is None:
        return 2
    try:
        return subprocess.run(ch.command, check=False).returncode
    except OSError as exc:
        log.error("upgrade: %s: %s", " ".join(ch.command), exc)
        return 1
