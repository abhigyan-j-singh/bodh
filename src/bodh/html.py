"""One HTML layer: the shared style (colours as variables, light and dark), the page shell,
tables and escaping. The dashboard, the progress section and the console render through
it, so a colour or a font is set once. No framework, no external fonts or scripts; the
console's JavaScript lives in console.py, in one string."""

from __future__ import annotations

import html as _html
from collections.abc import Iterable
from typing import Any


def esc(value: Any) -> str:
    """Text safe inside an element or a quoted attribute; None is the empty string."""
    return _html.escape(str(value if value is not None else ""))


def href(url: str | None) -> str:
    """A link target, escaped, and only ever http(s): escaping does not stop `javascript:`."""
    u = (url or "").strip()
    return esc(u) if u.lower().startswith(("http://", "https://")) else "#"


# the palette and the type, once; dark mode is the same variables with other values
CSS = """
:root{--bg:#fff;--fg:#222;--muted:#777;--head:#555;--line:#eee;--rule:#ddd;--panel:#f6f6f6;--panel-line:#ccc;
--ok:#2a7;--bad:#c33;--warn:#c80;--flight:#e39b5d;--prune:#c9664a;--read:#4a90c9;--star:#e0b800;--pending:#bbb;--flash:#fff3b0}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#ddd;--muted:#999;--head:#aaa;--line:#2a2a2a;--rule:#333;
--panel:#1f1f1f;--panel-line:#444;--flash:#4a4000}}
body{font:15px/1.45 -apple-system,system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:var(--fg);background:var(--bg)}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;border-bottom:1px solid var(--rule);padding-bottom:4px}
h3{font-size:15px;margin:16px 0 6px}
a{color:inherit}table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--head);font-weight:600}.num{text-align:right;font-variant-numeric:tabular-nums}.muted{color:var(--muted)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
pre{white-space:pre-wrap;background:var(--panel);padding:10px;border-radius:6px;font-size:12.5px}
.kpis{display:flex;gap:18px;flex-wrap:wrap;margin:10px 0}.kpi{background:var(--panel);border-radius:8px;padding:8px 12px;min-width:110px}
.kpi b{display:block;font-size:20px}.kpi small{color:var(--muted)}
.dot{display:inline-block;width:10px;height:10px;border-radius:5px;background:var(--pending);margin-right:6px;vertical-align:middle}
.dot.on{background:var(--ok);animation:pulse 1.2s infinite}@keyframes pulse{0%{opacity:1}50%{opacity:.25}100%{opacity:1}}
.flash{background:var(--flash);transition:background 1.5s}
"""


def page(title: str, body: str, css_extra: str = "", head_extra: str = "") -> str:
    """A complete document: the shell, the shared style, any extra style, the body."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)}</title><style>{CSS}{css_extra}</style>{head_extra}</head><body>\n"
        f"{body}\n</body></html>"
    )


def table(columns: Iterable[str | tuple[str, str]], rows: Iterable[Iterable[str]], empty: str = "") -> str:
    """A table from column labels (a `(label, "num")` pair right-aligns the column) and rows of
    cells that are already HTML. `empty` is shown, muted, when there are no rows."""
    cols = [(c, "") if isinstance(c, str) else c for c in columns]
    head = "".join(
        f"<th class='num'>{esc(label)}</th>" if cls == "num" else f"<th>{esc(label)}</th>"
        for label, cls in cols
    )
    body_rows = []
    for row in rows:
        cells = "".join(
            f"<td class='num'>{cell}</td>" if cls == "num" else f"<td>{cell}</td>"
            for (_, cls), cell in zip(cols, row, strict=False)
        )
        body_rows.append(f"<tr>{cells}</tr>")
    if not body_rows and empty:
        body_rows.append(f"<tr><td colspan='{len(cols)}' class='muted'>{esc(empty)}</td></tr>")
    return f"<table><tr>{head}</tr>{''.join(body_rows)}</table>"


def kpi(label: str, value: str, raw: bool = False) -> str:
    """One tile of the KPI strip; `raw` leaves an HTML value alone."""
    return (
        f"<div class='kpi'><span class='muted'>{esc(label)}</span><b>{value if raw else esc(value)}</b></div>"
    )


def section(title: str, inner: str) -> str:
    return f"<h2>{esc(title)}</h2>{inner}"


def muted(text: str) -> str:
    return f"<p class='muted'>{esc(text)}</p>"
