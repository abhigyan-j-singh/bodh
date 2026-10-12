"""Where the example files live: inside the package in a wheel (`bodh/templates/`, put there at
build time from the repository's own copies), at the repository root in a checkout. `bodh init`
and the installer read them from here; there is one copy of each in the repository."""

from __future__ import annotations

from pathlib import Path

from .errors import BodhError

PACKAGE_DIR = Path(__file__).resolve().parent / "templates"
REPO_ROOT = Path(__file__).resolve().parents[2]

# template name -> (path inside the package, path in a checkout)
TEMPLATES = {
    "config.yaml": ("config.example.yaml", "config.example.yaml"),
    ".env": ("env.example", ".env.example"),
    "profile/about.md": ("profile.example/about.md", "profile.example/about.md"),
    "profile/learned.md": ("profile.example/learned.md", "profile.example/learned.md"),
}


def template_path(name: str) -> Path:
    """The example file a project file starts from, by the project file's name."""
    try:
        packaged, checkout = TEMPLATES[name]
    except KeyError:
        raise BodhError(f"no template for {name}") from None
    for candidate in (PACKAGE_DIR / packaged, REPO_ROOT / checkout):
        if candidate.exists():
            return candidate
    raise BodhError(f"the template for {name} is missing from this install ({PACKAGE_DIR / packaged})")


def template_text(name: str) -> str:
    return template_path(name).read_text(encoding="utf-8")
