"""The design tokens: every colour, size and distance on the page, defined once here and
emitted as CSS custom properties. Light and dark are two value sets of the same names. The
colours were chosen in OKLCH and are validated by `scripts/validate_palette.py` (the report is
in docs/design.md); nothing in a template or a script names a value that is not here."""

from __future__ import annotations

from typing import Any

BANDS = (90, 70, 50, 30, 0)  # best first, as the page orders them

# -- colour, by role -----------------------------------------------------------------------

LIGHT: dict[str, str] = {
    "surface-1": "#fbfaf8",  # the page: warm paper, not pure white
    "surface-2": "#f3f1ee",  # cards, table stripes
    "surface-3": "#eae7e2",  # a pressed control, a skeleton
    "text-primary": "#1c1a17",
    "text-secondary": "#5c5750",
    "text-muted": "#6f6962",
    "line": "#dedbd6",  # hairlines
    "line-strong": "#88827a",  # lines that must read on their own (3:1)
    "accent": "#047460",  # one hue: links, the current section, the progress bar, the selected filter
    "accent-wash": "#e6f1ee",  # the accent at about 10% over the page, for selection and hover
    "accent-track": "#cfe4de",  # the meter's track: a lighter step of the accent
    # status, for an icon, a border or a bar beside a word (3:1 as a mark; the word is text-primary
    # on the wash): chosen so a protanope or a deuteranope still tells the three apart
    "good": "#318f5a",
    "warning": "#ab7302",
    "critical": "#90101a",
    "good-wash": "#e4f2e8",
    "warning-wash": "#f7eedb",
    "critical-wash": "#f8e4e3",
    "series-muted": "#88827a",  # context series in an emphasis chart
    # the band scale: one hue (indigo), light to dark, more is better reads as darker
    "band-90": "#1c2d72",
    "band-70": "#425ba7",
    "band-50": "#718bd1",
    "band-30": "#a8bdf2",
    "band-0": "#eaf0ff",
    # the text-safe companion of each step: a chip's background, text-primary on it
    "band-90-wash": "#c0d2ff",
    "band-70-wash": "#cedcff",
    "band-50-wash": "#dce6ff",
    "band-30-wash": "#e8eeff",
    "band-0-wash": "#f3f7ff",
    "focus": "#047460",
}

DARK: dict[str, str] = {
    "surface-1": "#161513",
    "surface-2": "#1f1e1b",
    "surface-3": "#2a2825",
    "text-primary": "#f0ede8",
    "text-secondary": "#b3aea6",
    "text-muted": "#8f8a82",
    "line": "#33312e",
    "line-strong": "#6e6961",
    "accent": "#6ccbb3",
    "accent-wash": "#1c2a27",
    "accent-track": "#274741",
    "good": "#80dba2",
    "warning": "#f0b135",
    "critical": "#ff7f77",
    "good-wash": "#1c2f22",
    "warning-wash": "#332a14",
    "critical-wash": "#3a1f1e",
    "series-muted": "#6e6961",
    "band-90": "#dae4ff",
    "band-70": "#95b0f9",
    "band-50": "#637fce",
    "band-30": "#3a5199",
    "band-0": "#222e55",
    "band-90-wash": "#3a4971",
    "band-70-wash": "#323e60",
    "band-50-wash": "#2a344f",
    "band-30-wash": "#232b3f",
    "band-0-wash": "#1b2130",
    "focus": "#6ccbb3",
}

# -- everything that is not a colour --------------------------------------------------------

TYPE = {
    "font": "-apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Roboto, sans-serif",
    "font-mono": "ui-monospace, 'SF Mono', Menlo, Consolas, monospace",
    "size-13": "13px",  # table body, captions
    "size-15": "15px",  # body
    "size-17": "17px",  # section lead
    "size-22": "22px",  # section title
    "size-48": "48px",  # the hero figure
    "leading-prose": "1.45",
    "leading-heading": "1.25",
    "weight-regular": "400",
    "weight-semibold": "600",
}

SPACE = {
    "space-1": "4px",
    "space-2": "8px",
    "space-3": "16px",
    "space-4": "24px",
    "space-5": "32px",
    "space-6": "48px",
}
SHAPE = {"radius-tile": "8px", "radius-control": "4px", "icon": "16px", "row": "36px", "row-touch": "44px"}
LAYOUT = {"column-prose": "720px", "column-wide": "1100px", "gutter": "16px"}
BREAKPOINTS = {"phone": 390, "tablet": 768, "desktop": 1100}  # px; media queries read these
MOTION = {"duration": "150ms", "duration-wash": "300ms", "easing": "ease-out"}


def all_tokens() -> dict[str, Any]:
    """Every token with its light value, for the docs and the style guide."""
    return {**LIGHT, **TYPE, **SPACE, **SHAPE, **LAYOUT, **MOTION}


def _declarations(values: dict[str, str], indent: str = "  ") -> str:
    return "\n".join(f"{indent}--{name}: {value};" for name, value in values.items())


def css() -> str:
    """The `tokens` layer: light values on :root, dark under the OS preference (unless a light
    stamp wins) and under an explicit `data-theme="dark"` the Mac app may set."""
    light = _declarations({**LIGHT, **TYPE, **SPACE, **SHAPE, **LAYOUT, **MOTION})
    dark = _declarations(DARK, indent="    ")
    return (
        "@layer tokens {\n"
        f":root {{\n{light}\n  color-scheme: light dark;\n}}\n"
        "@media (prefers-color-scheme: dark) {\n"
        f'  :root:not([data-theme="light"]) {{\n{dark}\n  }}\n'
        "}\n"
        f':root[data-theme="dark"] {{\n{dark}\n}}\n'
        "@media (prefers-reduced-motion: reduce) {\n"
        "  :root {\n    --duration: 0ms;\n    --duration-wash: 0ms;\n  }\n"
        "}\n"
        "}\n"
    )


def band_token(band: int) -> str:
    """The token name for a band threshold: `band-70`."""
    return f"band-{band}"
