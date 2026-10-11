"""What the repository promises an installer: the package metadata, before the templates and the
ignores join this file in Phase 4."""

from __future__ import annotations

import re
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_metadata_is_complete():
    """pyproject.toml names the project, its licence, its author and where it lives; the
    version comes from the package itself (one place), and both commands are declared."""
    from bodh import __version__

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert project["name"] == "bodh"
    assert "version" in project["dynamic"] and data["tool"]["hatch"]["version"]["path"] == "src/bodh/__init__.py"
    assert re.fullmatch(r"\d+\.\d+\.\d+(\.dev\d+)?", __version__)
    assert project["license"] == "MIT" and (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    assert project["authors"] == [{"name": "Abhigyan J Singh"}]
    assert project["requires-python"] == ">=3.10"
    for key in ("Homepage", "Repository", "Issues", "Changelog"):
        assert project["urls"][key].startswith("https://github.com/abhigyan-j-singh/bodh")
    assert project["scripts"] == {"bodh": "bodh.cli:app", "triage": "bodh.cli:triage_alias"}
    assert "rss" in project["keywords"] and "bazqux" in project["keywords"]
    for c in ("Programming Language :: Python :: 3.10", "Operating System :: MacOS",
              "Operating System :: POSIX :: Linux", "Operating System :: Microsoft :: Windows"):
        assert c in project["classifiers"], c
