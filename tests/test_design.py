"""The design system: every colour, size and distance comes from a token; the palette passes
the validator's checks in light and in dark; the style guide renders every token and
component; the stylesheet names only tokens; the icons are a closed set."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from bodh.web import colour, icons, templates, tokens

ROOT = Path(__file__).resolve().parents[1]


def test_the_palette_passes_every_check():
    res = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_palette.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout
    assert "every check passed" in res.stdout and "FAIL" not in res.stdout


def test_light_and_dark_name_the_same_tokens():
    assert set(tokens.LIGHT) == set(tokens.DARK)
    for name, value in {**tokens.LIGHT, **tokens.DARK}.items():
        assert re.fullmatch(r"#[0-9a-f]{6}", value), (name, value)


def test_the_css_names_only_tokens():
    """A value in the stylesheet is a token reference, a structural number or a keyword: no
    hex colour, no size in px that is not a hairline or a chart mark, no font name."""
    css = templates.STYLESHEET.read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", css), "a hex colour outside tokens.py"
    assert "font-family: -apple" not in css and "Helvetica" not in css
    for match in re.finditer(r"var\(--([a-z0-9-]+)\)", css):
        name = match.group(1)
        assert name in tokens.all_tokens(), f"--{name} is not a token"
    emitted = tokens.css()
    assert (
        "--band-90: #1c2d72;" in emitted
        and '[data-theme="dark"]' in emitted
        and "prefers-color-scheme: dark" in emitted
    )
    assert "prefers-reduced-motion" in emitted


def test_the_style_guide_shows_every_token_and_component():
    page = templates.design_page()
    for name in tokens.LIGHT:
        assert f"--{name}" in page, name
    for name in icons.NAMES:
        assert f"icon-{name}" in page, name
    for part in (
        "chip band-90",
        'class="tile"',
        'class="meter"',
        'class="bar"',
        "grid-table",
        "list-row",
        "banner warning",
        "banner critical",
    ):
        assert part in page, part
    assert "<style>" in page and "<script>" in page
    assert (
        'src="http' not in page and page.count("<link") == 1 and 'href="data:image/svg+xml' in page
    )  # no request to any host
    assert 'lang="en"' in page and '<meta name="viewport"' in page and "Skip to the content" in page


def test_colour_maths_round_trips_and_contrasts():
    assert colour.rgb_to_hex(colour.hex_to_rgb("#047460")) == "#047460"
    assert round(colour.contrast("#000000", "#ffffff"), 1) == 21.0
    lum, chroma, hue = colour.oklch("#047460")
    assert 0.45 < lum < 0.55 and chroma > 0.08 and 160 < hue < 190
    assert colour.delta_e("#047460", "#047460") == 0.0
    assert colour.simulate("#ff0000", "protan") != "#ff0000"
    assert re.fullmatch(
        r"#[0-9a-f]{6}", colour.oklch_to_hex(0.5, 0.3, 30)
    )  # out of gamut: chroma lowered, still a colour


def test_the_helpers_escape_and_the_sparkline_is_labelled():
    assert templates.esc('<a href="x">') == "&lt;a href=&quot;x&quot;&gt;"
    assert (
        templates.href("javascript:alert(1)") == "#"
        and templates.href(" https://example.org/a ") == "https://example.org/a"
    )
    spark = templates.sparkline([1, 2, 3, 2, 5, 4, 6], "judged, last seven days")
    assert 'role="img"' in spark and "last seven days" in spark and 'class="today"' in spark
    assert templates.sparkline([1], "one") == ""
    m = templates.meter(240, 1200, "judging")
    assert 'role="meter"' in m and 'aria-valuenow="240"' in m and "width:20.0%" in m
    chip = templates.band_chip(92.4, 90)
    assert ">92<" in chip and "band-90" in chip
    grid = templates.grades_grid([("90 and up", [10, 0, 2, 0])], ("great", "good", "meh", "bad"), "c")
    assert (
        'class="cell w5" data-tip="10 great in 90 and up">10<' in grid
        and 'class="cell" data-tip="0 good' in grid
    )
    assert 'class="cell w1" data-tip="2 meh in 90 and up">2<' in grid
    assert icons.icon("warning", "Warning").count("aria-label") == 1 and 'aria-hidden="true"' in icons.icon(
        "run"
    )
