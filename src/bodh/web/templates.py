"""The HTML: small functions that return markup for each part of the page from a typed slice
of the document, each escaping through one `esc`. The page shell (`page`) carries the tokens,
the stylesheet and, in the console, the script; `design_page` is the living style guide the
console serves at /design in development and the visual tests screenshot."""

from __future__ import annotations

import html
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import icons, tokens

HERE = Path(__file__).resolve().parent
STYLESHEET = HERE / "dashboard.css"
SCRIPT = HERE / "dashboard.js"
MODES = ("console", "export", "app")


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def href(url: str | None) -> str:
    """A link target that is http(s), or nothing: an article's URL is data from a feed."""
    text = (url or "").strip()
    return esc(text) if text.startswith(("http://", "https://")) else "#"


def stylesheet() -> str:
    return tokens.css() + STYLESHEET.read_text(encoding="utf-8")


def script() -> str:
    return SCRIPT.read_text(encoding="utf-8") if SCRIPT.exists() else ""


def page(title: str, body: str, mode: str = "export", theme: str | None = None, extra_head: str = "") -> str:
    """The document: tokens and stylesheet inline (no request to any host), the body, and in
    the console and the app the script. `theme` stamps `data-theme` for the Mac app."""
    assert mode in MODES
    stamp = f' data-theme="{esc(theme)}"' if theme in ("light", "dark") else ""
    live = f"<script>{script()}</script>" if mode in ("console", "app") else ""
    return (
        "<!doctype html>\n"
        f'<html lang="en" data-mode="{mode}"{stamp}>\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{esc(title)}</title>\n"
        f"<style>{stylesheet()}</style>\n{extra_head}</head>\n"
        f'<body class="mode-{mode}">\n{body}\n{live}</body>\n</html>\n'
    )


# -- small parts ------------------------------------------------------------------------


def chip(text: str, kind: str = "", icon_name: str | None = None, title: str | None = None) -> str:
    cls = f"chip {kind}".strip()
    icn = icons.icon(icon_name) if icon_name else ""
    ttl = f' title="{esc(title)}"' if title else ""
    return f'<span class="{cls}"{ttl}>{icn}{esc(text)}</span>'


def band_chip(score: float, band: int) -> str:
    """The band as a chip with its number: colour and the printed score together."""
    return chip(f"{score:.0f}", f"band-{band}", title=f"band {band} and above")


def button(label: str, icon_name: str | None = None, kind: str = "", attrs: str = "") -> str:
    icn = icons.icon(icon_name) if icon_name else ""
    cls = f"button {kind}".strip()
    sep = " " if attrs else ""
    return f'<button type="button" class="{cls}"{sep}{attrs}>{icn}{esc(label)}</button>'


def section(anchor: str, title: str, lead: str, inner: str) -> str:
    return (
        f'<section class="section" id="{esc(anchor)}" aria-labelledby="{esc(anchor)}-title">\n'
        f'<h2 id="{esc(anchor)}-title">{esc(title)}</h2>\n'
        f'<p class="lead">{esc(lead)}</p>\n{inner}\n</section>'
    )


def empty(text: str) -> str:
    return f'<p class="empty">{esc(text)}</p>'


def table(
    columns: list[tuple[str, str]],
    rows: list[list[str]],
    caption: str,
    cards: bool = True,
    head_col: int = 0,
    sortable: bool = False,
    extra_class: str = "",
) -> str:
    """A table with a caption and header scopes; under 640 px it becomes stacked cards (each
    cell labelled by its header). `columns` are (label, "text" | "num"). Cells are markup
    already escaped by the caller."""
    cls = ("cards " if cards else "") + extra_class
    head = "".join(
        f'<th scope="col" class="{kind}"'
        + (' aria-sort="none"' if sortable else "")
        + ">"
        + (f'<button type="button" data-sort="{i}">{esc(label)}</button>' if sortable else esc(label))
        + "</th>"
        for i, (label, kind) in enumerate(columns)
    )
    body = []
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            kind = columns[i][1] if i < len(columns) else "text"
            label = columns[i][0] if i < len(columns) else ""
            if i == head_col:
                cells.append(f'<td class="head {kind}">{cell}</td>')
            else:
                cells.append(f'<td class="{kind}" data-label="{esc(label)}">{cell}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (
        f'<table class="{cls.strip()}"><caption>{esc(caption)}</caption>'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def grades_grid(rows: list[tuple[str, list[int]]], grades: tuple[str, ...], caption: str) -> str:
    """Bands as rows, grades as columns, each cell shaded on a sequential wash by its count
    with the number printed (five steps of the band washes, reused as a count scale)."""
    top = max((n for _b, counts in rows for n in counts), default=0)
    head = '<th scope="col">Band</th>' + "".join(f'<th scope="col" class="num">{esc(g)}</th>' for g in grades)
    body = []
    for band, counts in rows:
        cells = []
        for n in counts:
            step = 0 if not n or not top else max(1, min(5, round(5 * n / top)))
            cls = f"cell w{step}" if step else "cell"
            cells.append(f'<td class="{cls}">{n:,}</td>')
        body.append(f'<tr><th scope="row">{esc(band)}</th>{"".join(cells)}</tr>')
    return (
        f'<table class="grid-table"><caption>{esc(caption)}</caption><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def tile(label: str, value: str, delta: str = "", delta_class: str = "", spark: str = "") -> str:
    d = f'<span class="delta {delta_class}">{esc(delta)}</span>' if delta else ""
    return (
        f'<div class="tile"><span class="label">{esc(label)}</span>'
        f'<span class="value num">{esc(value)}</span>{d}{spark}</div>'
    )


def sparkline(values: Sequence[float], label: str) -> str:
    """Seven points: the last in the accent with a ring, the rest muted; no axes. The tile's
    value and delta carry the numbers, so the picture is decorative with a text alternative."""
    if len(values) < 2:
        return ""
    w, h, pad = 72, 20, 3
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pts = []
    for i, v in enumerate(values):
        x = pad + (w - 2 * pad) * i / (len(values) - 1)
        y = h - pad - (h - 2 * pad) * (v - lo) / span
        pts.append((round(x, 1), round(y, 1)))
    path = " ".join(f"{'M' if i == 0 else 'L'}{x} {y}" for i, (x, y) in enumerate(pts))
    tail = f"M{pts[-2][0]} {pts[-2][1]} L{pts[-1][0]} {pts[-1][1]}"
    cx, cy = pts[-1]
    return (
        f'<svg class="spark" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">'
        f'<path class="path" d="{path}"/><path class="tail" d="{tail}"/>'
        f'<circle class="today" cx="{cx}" cy="{cy}" r="3"/></svg>'
    )


def meter(done: int, total: int, label: str) -> str:
    share = 0 if not total else max(0.0, min(100.0, 100.0 * done / total))
    return (
        f'<div class="meter" role="meter" aria-valuemin="0" aria-valuemax="{total}" aria-valuenow="{done}" '
        f'aria-label="{esc(label)}"><span style="width:{share:.1f}%"></span></div>'
    )


def banner(
    level: str, word: str, text: str, link: tuple[str, str] | None = None, dismissable: bool = False
) -> str:
    """One attention banner: a colour, an icon, a word, a sentence; never colour alone."""
    icn = icons.icon("warning" if level in ("warning", "critical") else "check", word)
    more = f' <a href="{href(link[1])}">{esc(link[0])}</a>' if link else ""
    close = (
        f'<button type="button" class="button quiet dismiss" data-dismiss aria-label="Dismiss">{icons.icon("close")}</button>'
        if dismissable
        else ""
    )
    return (
        f'<div class="banner {esc(level)}" role="status">{icn}<div><span class="word">{esc(word)}.</span> '
        f"{esc(text)}{more}</div>{close}</div>"
    )


# -- the style guide ----------------------------------------------------------------------


def design_page() -> str:
    """Every token and component on one screen: what the visual tests screenshot and what a
    contributor looks at before adding a part."""
    body = [
        '<a class="skip" href="#main">Skip to the content</a>',
        '<div class="frame"><header class="header"><h1 class="name">bodh</h1>'
        '<span class="asof">the design system</span></header>',
        '<main id="main">',
    ]
    body.append(_design_colours())
    body.append(_design_type())
    body.append(_design_components())
    body.append(_design_states())
    body.append("</main></div>")
    return page("bodh design", "\n".join(body), mode="console")


def _swatch(name: str) -> str:
    return (
        f'<div class="swatch"><div class="colour" style="background: var(--{name})"></div>'
        f'<div class="name">--{name}</div></div>'
    )


def _design_colours() -> str:
    roles = [k for k in tokens.LIGHT if not k.startswith("band")]
    bands = [f"band-{b}" for b in tokens.BANDS] + [f"band-{b}-wash" for b in tokens.BANDS]
    return section(
        "colour",
        "Colour",
        "Every colour is a role, defined once; light and dark are two value sets of the same names.",
        f'<h3>Roles</h3><div class="swatches">{"".join(_swatch(r) for r in roles)}</div>'
        f'<h3 style="margin-top: var(--space-4)">The band scale and its washes</h3>'
        f'<div class="swatches">{"".join(_swatch(b) for b in bands)}</div>',
    )


def _design_type() -> str:
    scale = "".join(
        f'<div style="font-size: var(--size-{s}); line-height: var(--leading-heading)">{s} px: Bodh keeps your unread list worth reading</div>'
        for s in (13, 15, 17, 22, 48)
    )
    spacing = "".join(
        f'<div style="display:flex;align-items:center;gap:var(--space-2)"><span style="display:inline-block;width:var(--{k});height:12px;background:var(--accent)"></span><span class="small secondary">{k} = {v}</span></div>'
        for k, v in tokens.SPACE.items()
    )
    return section(
        "type",
        "Type and space",
        "The system stack; five sizes on one scale; an 8-point spacing scale with 4 for inner chips.",
        f'<div class="scale">{scale}</div><h3 style="margin-top: var(--space-4)">Spacing</h3><div class="scale">{spacing}</div>'
        f'<h3 style="margin-top: var(--space-4)">Icons</h3><p>'
        + " ".join(f'<span title="{n}">{icons.icon(n)}</span>' for n in icons.NAMES)
        + "</p>",
    )


def bars(rows: list[tuple[str, float, float]], first: str, second: str, scale: float = 1.0) -> str:
    """Two shares per row as horizontal bars in one hue and its lighter step, with a legend and
    direct labels at the bar ends: the calibration chart's form."""
    legend = f'<div class="legend"><span class="one"><i></i>{esc(first)}</span><span class="two"><i></i>{esc(second)}</span></div>'
    body = "".join(
        f'<span class="name">{esc(name)}</span><div class="pair">'
        f'<div class="bar" style="width:{min(100.0, a * scale):.1f}%"><span class="v">{a:.0f}%</span></div>'
        f'<div class="bar second" style="width:{min(100.0, b * scale):.1f}%"><span class="v">{b:.0f}%</span></div></div>'
        for name, a, b in rows
    )
    return f'{legend}<div class="bars">{body}</div>'


def list_item(
    score: float,
    band: int,
    title: str,
    url: str,
    lesson: str,
    meta: list[str],
    reader_url: str | None = None,
    verdicts: tuple[str, ...] = (),
    article_id: str = "",
) -> str:
    """One row of the list: the band chip, the title as the link, the lesson, the meta line
    (concept, feed, age), a quiet link to the reader, and in the console the verdict buttons."""
    parts = [f"<span>{esc(m)}</span>" for m in meta if m]
    if reader_url:
        parts.append(f'<a href="{href(reader_url)}">{icons.icon("link")}BazQux</a>')
    meta_html = "<span>·</span>".join(parts)
    buttons = (
        '<div class="verdicts">'
        + "".join(
            f'<button type="button" class="button quiet small" data-verdict="{esc(v)}" data-id="{esc(article_id)}">{esc(v)}</button>'
            for v in verdicts
        )
        + "</div>"
        if verdicts
        else ""
    )
    return (
        f'<li class="list-row" data-id="{esc(article_id)}">{band_chip(score, band)}'
        f'<div class="title"><a href="{href(url)}">{esc(title)}</a></div>'
        f'<div class="lesson">{esc(lesson)}</div><div class="meta">{meta_html}</div>{buttons}</li>'
    )


def _design_tiles() -> str:
    samples = (
        (
            "Judged today",
            "1,251",
            "+120 vs yesterday",
            "up-good",
            [900, 1100, 950, 1300, 1200, 1131, 1251],
            "judged",
        ),
        ("Cut today", "402", "−31 vs yesterday", "down-good", [500, 420, 460, 380, 410, 433, 402], "cut"),
        ("Read today", "7", "−2 vs yesterday", "down-bad", [5, 9, 4, 8, 6, 9, 7], "read"),
        (
            "Spend today",
            "$0.78",
            "+$0.10 vs yesterday",
            "up-bad",
            [0.6, 0.7, 0.5, 0.9, 0.8, 0.68, 0.78],
            "spend",
        ),
    )
    return (
        '<div class="tiles">'
        + "".join(
            tile(label, value, delta, cls, sparkline(series, f"{what}, last seven days"))
            for label, value, delta, cls, series, what in samples
        )
        + "</div>"
    )


_STATUS_SAMPLES = (
    ("Scoring only", "accent", None),
    ("Cut below 35", "accent", None),
    ("ok", "good", "check"),
    ("budget reached", "warning", "warning"),
    ("failed", "critical", "warning"),
)


def _design_components() -> str:
    chips = " ".join(band_chip(s, b) for s, b in ((94, 90), (78, 70), (55, 50), (41, 30), (12, 0)))
    status = " ".join(chip(t, k, i) for t, k, i in _STATUS_SAMPLES)
    buttons = (
        '<div class="levers">'
        + button("Run now", "run", "primary")
        + button("Pause", "pause")
        + button("Stop", "stop")
        + button("Show as table", kind="quiet")
        + "</div>"
    )
    the_bars = bars(
        [
            ("90 and up", 62, 20),
            ("70 to 89", 41, 9),
            ("50 to 69", 18, 2),
            ("30 to 49", 6, 0),
            ("under 30", 2, 0),
        ],
        "read",
        "starred",
    )
    grid = grades_grid(
        [("90 and up", [14, 6, 1, 0]), ("70 to 89", [6, 9, 3, 1]), ("50 to 69", [1, 2, 7, 2])],
        ("great", "good", "meh", "bad"),
        "Your grades against the judge's bands (a sample)",
    )
    row = (
        '<ul class="list">'
        + list_item(
            92,
            90,
            "Why liquid cooling retrofits cost more than greenfield builds",
            "https://example.org/retrofits",
            "Retrofits pay for manifolds, CDUs and leak detection in a live hall; greenfield sizes the plant once.",
            ["data centres", "The Register", "2 h"],
            "https://bazqux.com/i/1",
            ("great", "good", "meh", "bad"),
            "1",
        )
        + "</ul>"
    )
    return section(
        "components",
        "Components",
        "Chips, tiles, buttons, the meter, bars, the grid, a list row: each built from the tokens, each with its text alternative.",
        f"<h3>Chips</h3><p>{chips}</p><p style='margin-top: var(--space-2)'>{status}</p>"
        f'<h3 style="margin-top: var(--space-4)">Tiles</h3>{_design_tiles()}'
        f'<h3 style="margin-top: var(--space-4)">Buttons</h3>{buttons}'
        f'<h3 style="margin-top: var(--space-4)">The meter</h3>{meter(240, 1200, "judging, 240 of 1,200")}'
        f'<h3 style="margin-top: var(--space-4)">Bars</h3>{the_bars}'
        f'<h3 style="margin-top: var(--space-4)">The grid</h3>{grid}'
        f'<h3 style="margin-top: var(--space-4)">A list row</h3>{row}',
    )


def _design_states() -> str:
    return section(
        "states",
        "States",
        "Loading, refreshing, stale, empty, error and offline: each designed, none left to chance.",
        "<h3>Banners</h3>"
        + banner(
            "warning",
            "Budget reached",
            "Judging pauses until midnight; nothing is cut because of it.",
            dismissable=True,
        )
        + banner(
            "critical", "The last run failed", "BazQux did not answer (403). Check the password in .env."
        )
        + banner(
            "good",
            "Profile rewritten",
            "Sunday's rewrite passed the gate.",
            link=("Show the diff", "#learning"),
        )
        + '<h3 style="margin-top: var(--space-4)">Empty</h3>'
        + empty(
            "Nothing at 70 or above right now. 34 articles are waiting for the judge; the next run is in 22 minutes."
        )
        + '<h3 style="margin-top: var(--space-4)">Stale</h3><p><span class="stamp stale">updated 12 min ago · the engine is not answering</span></p>'
        + '<h3 style="margin-top: var(--space-4)">Error</h3><p class="error">BazQux did not answer (403). Check the password in .env.</p>'
        + '<h3 style="margin-top: var(--space-4)">Loading</h3><p><span class="skeleton">Loading the first render</span></p>'
        + '<h3 style="margin-top: var(--space-4)">Refreshing</h3><p class="refreshing">The previous render, held at reduced opacity while the next arrives; no layout jump.</p>',
    )
