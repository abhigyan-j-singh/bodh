"""The inline icon set: 16 px SVG paths, stroke 1.5, currentColor, so an icon takes the text
colour of where it sits. The paths live once in a sprite at the top of the body; each use is
a small `<use>`. Decorative icons are hidden from the accessibility tree; an icon that
carries meaning gets a label."""

from __future__ import annotations

from html import escape

_PATHS = {
    "run": '<path d="M5 3.5v9l7-4.5z" fill="currentColor" stroke="none"/>',
    "pause": '<path d="M5.5 3.5v9M10.5 3.5v9"/>',
    "stop": '<rect x="4" y="4" width="8" height="8" rx="1" fill="currentColor" stroke="none"/>',
    "star": '<path d="m8 2.5 1.7 3.6 3.9.5-2.9 2.7.8 3.9L8 11.3l-3.5 1.9.8-3.9L2.4 6.6l3.9-.5z"/>',
    "link": '<path d="M9.5 3.5h3v3M12.5 3.5 7 9M6 4.5H4.5a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V10"/>',
    "chevron": '<path d="m6 3.5 4.5 4.5L6 12.5"/>',
    "chevron-down": '<path d="m3.5 6 4.5 4.5L12.5 6"/>',
    "warning": '<path d="M8 2.5 14 13H2zM8 6.5v3M8 11.2v.3"/>',
    "check": '<path d="m3.5 8.5 3 3 6-7"/>',
    "close": '<path d="m4 4 8 8M12 4l-8 8"/>',
    "circle": '<circle cx="8" cy="8" r="4.5"/>',
}


def icon(name: str, label: str | None = None) -> str:
    """One icon. With `label`, a screen reader reads it; without, it is decorative."""
    assert name in _PATHS, name
    aria = f' role="img" aria-label="{escape(label, quote=True)}"' if label else ' aria-hidden="true"'
    return f'<svg class="icon icon-{name}" width="16" height="16"{aria}><use href="#icon-{name}"/></svg>'


def sprite() -> str:
    """Every path once, hidden, for the `<use>` references: the first thing in the body."""
    symbols = "".join(
        f'<symbol id="icon-{name}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round">{path}</symbol>'
        for name, path in _PATHS.items()
    )
    return f'<svg hidden aria-hidden="true">{symbols}</svg>'


NAMES = tuple(_PATHS)
