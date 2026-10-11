"""The panels: one function per section of the page, each a pure function of its slice of the
document, each with its empty state; `render(doc, mode)` puts them in page order. The console
page, the static export and the Mac app's web view are this render with three flags."""

from __future__ import annotations

from . import icons
from .document import Article, DashboardDocument, Verdicts, pct, usd
from .templates import (
    band_chip,
    banner,
    bars,
    button,
    chip,
    empty,
    esc,
    grades_grid,
    href,
    list_item,
    meter,
    page,
    section,
    sparkline,
    table,
    tile,
)

SECTIONS = (
    ("today", "What happened today"),
    ("list", "What to read"),
    ("verdicts", "Is it judging well for you"),
    ("learning", "What it is learning"),
    ("feeds", "Your feeds"),
    ("money", "What it costs and how it runs"),
)


def render(
    doc: DashboardDocument, mode: str = "console", theme: str | None = None, generation: int | None = None
) -> str:
    """The whole page. `console`: levers, verdict menus, the log, the live layer; `export`:
    none of those, the document embedded, the snapshot stamp; `app`: the console without
    the page chrome the Mac window supplies. `generation` is the console's change counter,
    stamped so the page knows whether a tick means a refetch."""
    chrome = mode != "app"
    body = [
        '<a class="skip" href="#main">Skip to the content</a>' if chrome else "",
        '<div class="frame">',
        header(doc, mode) if chrome else "",
        nav() if chrome else "",
        '<main id="main">',
        state(doc, mode),
        today(doc),
        article_list(doc, mode),
        verdicts(doc, mode),
        learning(doc),
        feeds(doc),
        money(doc, mode),
        footer(doc, mode) if chrome else "",
        "</main></div>",
    ]
    if mode == "export":
        embedded = doc.to_json().replace("</", "<\\/")
        body.append(f'<script type="application/json" id="bodh-document">{embedded}</script>')
    head = f'<meta name="bodh-generation" content="{generation}">\n' if generation is not None else ""
    return page("bodh", "\n".join(part for part in body if part), mode=mode, theme=theme, extra_head=head)


def header(doc: DashboardDocument, mode: str) -> str:
    stamp = (
        f'<span class="stamp" id="stamp" data-as-of="{esc(doc.as_of)}">as of {esc(doc.as_of_text)}</span>'
        if mode != "export"
        else f'<span class="stamp">{esc(doc.footer.exported_text or "")}</span>'
    )
    return (
        '<header class="header"><h1 class="name">bodh</h1>'
        f'<span class="asof" title="{esc(doc.as_of)}">{esc(doc.timezone)}</span>{stamp}</header>'
    )


def nav() -> str:
    items = "".join(
        f'<li><a href="#{anchor}" data-section="{anchor}">{esc(title)}</a></li>' for anchor, title in SECTIONS
    )
    return f'<nav class="nav" aria-label="Sections"><ul>{items}</ul></nav>'


def state(doc: DashboardDocument, mode: str) -> str:
    st = doc.state
    bar = ""
    if st.running and st.progress_total:
        bar = meter(st.progress_done or 0, st.progress_total, st.phase or "progress")
    levers = ""
    if mode != "export":
        # every lever is in the page, hidden when the state does not offer it, so a tick can
        # show Stop when a run starts without a refetch
        parts = [
            button(
                "Run now", "run", "primary", 'data-action="run"' + ("" if "run" in st.levers else " hidden")
            ),
            button("Stop", "stop", "", 'data-action="stop"' + ("" if "stop" in st.levers else " hidden")),
            button(
                "Pause",
                "pause",
                "",
                'data-action="schedule_pause"' + ("" if "pause" in st.levers else " hidden"),
            ),
        ]
        levers = f'<div class="levers js-only">{"".join(parts)}</div>'
    attention = ""
    if st.attention is not None:
        a = st.attention
        link = (a.link_text, a.link_url) if a.link_text and a.link_url else None
        attention = banner(a.level, a.word, a.text, link, a.dismissable).replace(
            '<div class="banner', f'<div data-key="{esc(a.key)}" class="banner', 1
        )
    mode_chip = chip(st.mode_label, "accent")
    more = _more_actions(doc) if mode != "export" else ""
    return (
        f'<section class="section state-section" id="state" aria-label="State" style="padding-top: var(--space-3)">'
        f'<div class="state"><span class="line" id="state-line">{esc(st.sentence)}</span>{mode_chip}{levers}</div>'
        f'<div id="state-meter">{bar}</div><div id="attention">{attention}</div>{more}</section>'
    )


def _more_actions(doc: DashboardDocument) -> str:
    """The console's other levers, folded away: each button names its action and carries the
    action the script posts; the ones that write to BazQux confirm first, with the numbers."""
    cutting = doc.state.mode == "autocut"
    keep, days = doc.today.target, 14

    def act(label: str, action: str, confirm: bool = False, kind: str = "") -> str:
        extra = ' data-confirm="1"' if confirm else ""
        return button(label, None, kind, f'data-action="{action}"{extra}')

    groups = [
        (
            "Run",
            act("Run, scoring only", "run_shadow")
            + act("Run with a sync", "run_sync")
            + act("Resume the schedule", "schedule_resume"),
        ),
        (
            "Cut",
            (
                act("Stop cutting", "autocut_off")
                if cutting
                else act("Cut from the next run on", "autocut_on", True)
            )
            + f'<label>keep <input type="number" min="0" step="1" value="{keep}" data-param="keep" aria-label="articles to keep"></label>'
            + act("Prune: what would go", "prune_dry")
            + act("Prune", "prune", True)
            + act("Undo the last cut", "undo", True)
            + f'<label>older than <input type="number" min="1" step="1" value="{days}" data-param="days" aria-label="days"> days</label>'
            + act("Clear old: what would go", "clear_old_dry")
            + act("Clear old", "clear_old", True),
        ),
        (
            "Judge",
            '<label><input type="checkbox" data-param="all_ages"> all ages</label>'
            + act("Catch up", "catch_up", True),
        ),
        (
            "Housekeeping",
            act("Rewrite the profile", "weekly")
            + act("Feed report", "report")
            + act("Doctor", "doctor")
            + act("Write the export", "dashboard"),
        ),
    ]
    rows = "".join(f'<div class="group"><span>{esc(name)}</span>{inner}</div>' for name, inner in groups)
    return (
        '<p class="notice" id="notice" role="status" aria-live="polite"></p>'
        f'<details class="more js-only"><summary>{icons.icon("chevron")}More actions</summary><div class="actions">{rows}</div></details>'
        '<pre class="log output" id="output" hidden></pre>'
    )


def today(doc: DashboardDocument) -> str:
    t = doc.today
    tiles = "".join(
        tile(x.label, x.text, x.delta_text, x.direction, sparkline(x.series, x.series_label)).replace(
            '<div class="tile">', f'<div class="tile" data-tile="{esc(x.key)}">', 1
        )
        for x in t.tiles
    )
    hero = (
        f'<div class="hero"><span class="figure num" id="hero-unread">{t.unread:,}</span>'
        f'<span class="label">unread · target {t.target:,}</span></div>'
    )
    return section("today", "What happened today", t.lead, hero + f'<div class="tiles">{tiles}</div>')


def article_list(doc: DashboardDocument, mode: str) -> str:
    lst = doc.list
    bands = "".join(
        f'<a href="#list" data-band="{b}"' + (' aria-current="true"' if b == lst.band else "") + f">{b}+</a>"
        for b in lst.bands
    )
    folders = "".join(f'<option value="{esc(f)}">{esc(f)}</option>' for f in lst.folders)
    filters = (
        '<div class="filters js-only"><div class="group"><span>Band</span>' + bands + "</div>"
        '<div class="group"><label><span>Folder</span> <select data-folder><option value="">all</option>'
        + folders
        + "</select></label></div>"
        '<div class="group"><span>Sort</span><a href="#list" data-sort="score" aria-current="true">score</a><a href="#list" data-sort="newest">newest</a></div></div>'
    )
    verdict_words = tuple(lst.verdicts) if mode != "export" else ()
    rows = "".join(
        list_item(
            a.score,
            a.band,
            a.title,
            a.url,
            a.lesson,
            [a.concept, a.feed, a.age_text],
            a.reader_url,
            verdict_words,
            a.id,
        ).replace('<li class="list-row"', f'<li class="list-row" {row_attrs(a)}', 1)
        for a in lst.articles
    )
    inner = filters + (f'<ul class="list" id="list-rows">{rows}</ul>' if rows else empty(lst.empty_text))
    if lst.total > len(lst.articles):
        rest = (
            '<button type="button" class="button quiet js-only" data-action="list_more">Show 50 more</button>'
            '<span class="nojs-only">The rest are in BazQux under the band tags.</span>'
            if mode != "export"
            else "The rest are in BazQux under the band tags."
        )
        inner += f'<p class="list-more secondary"><span id="list-count">Showing {len(lst.articles)} of {lst.total:,}.</span> {rest}</p>'
    return section("list", "What to read", lst.lead, inner)


def row_attrs(a: Article) -> str:
    """The data the script filters and sorts rows by."""
    return f'data-score="{a.score:g}" data-folder="{esc(a.folder or "")}" data-arrived="{esc(a.arrived_at or "")}"'


def list_rows_html(doc_rows: list[Article], verdicts: tuple[str, ...]) -> str:
    """Rows for the console's `/list`: the same markup as the page, appended by the script."""
    return "".join(
        list_item(
            a.score,
            a.band,
            a.title,
            a.url,
            a.lesson,
            [a.concept, a.feed, a.age_text],
            a.reader_url,
            verdicts,
            a.id,
        ).replace('<li class="list-row"', f'<li class="list-row" {row_attrs(a)}', 1)
        for a in doc_rows
    )


def _grid_part(v: Verdicts) -> str:
    if not any(n for r in v.grid for n in r.counts.values()):
        return f"<h3>Your grades</h3>{empty(v.grid_sentence)}"
    grid = grades_grid(
        [(r.label, [r.counts[g] for g in ("great", "good", "meh", "bad")]) for r in v.grid],
        ("great", "good", "meh", "bad"),
        "Your grades against the judge's bands, last 30 days",
    )
    return f"<h3>Your grades</h3>{grid}<p class='secondary small' style='margin-top: var(--space-2)'>{esc(v.grid_sentence)}</p>"


def _disagreements_part(v: Verdicts) -> str:
    rows = [
        [
            f'<a href="{href(d.url)}">{esc(d.title)}</a>',
            band_chip(d.score, d.band),
            esc(d.grade),
            esc(d.concept),
        ]
        for d in v.disagreements
    ]
    body = (
        table(
            [("Article", "text"), ("Score", "num"), ("Your grade", "text"), ("Concept", "text")],
            rows,
            "Where your grade and the judge's score disagree most, last 30 days",
        )
        if rows
        else empty(v.disagreements_empty)
    )
    return "<h3>Where you disagree</h3>" + body


def _calibration_part(v: Verdicts) -> str:
    if not any(r.n for r in v.calibration):
        return f"<h3>How often each band gets read</h3>{empty(v.calibration_empty)}"
    top_share = max((r.read_share or 0 for r in v.calibration), default=0) or 1
    rows = [(r.label, 100 * (r.read_share or 0), 100 * (r.star_share or 0)) for r in v.calibration]
    tips = [
        (
            f"{pct(r.read_share)} read of {r.n:,} at {r.label}",
            f"{pct(r.star_share)} starred of {r.n:,} at {r.label}",
        )
        for r in v.calibration
    ]
    twin = table(
        [("Band", "text"), ("Waited a day", "num"), ("Read", "num"), ("Starred", "num")],
        [[esc(r.label), f"{r.n:,}", esc(pct(r.read_share)), esc(pct(r.star_share))] for r in v.calibration],
        "How often each band gets read, among articles that waited a day",
        cards=False,
    )
    return (
        '<h3>How often each band gets read</h3><figure><div class="chart" data-chart="calibration">'
        f"{bars(rows, 'read', 'starred', 1 / top_share, tips)}</div><figcaption>{esc(v.calibration_sentence)}"
        f"<details><summary>{icons.icon('chevron')}Show as table</summary>{twin}</details></figcaption></figure>"
    )


def _near_misses_part(v: Verdicts, mode: str) -> str:
    rows = "".join(
        list_item(
            m.score,
            m.band,
            m.title,
            m.url,
            m.lesson,
            [m.concept, f"cut by {m.cut_mode}"],
            None,
            ("rescue",) if mode != "export" else (),
            m.id,
        )
        for m in v.near_misses
    )
    body = f'<ul class="list">{rows}</ul>' if rows else empty(v.near_misses_empty)
    return "<h3>Near misses: the best of what was cut this week</h3>" + body


def verdicts(doc: DashboardDocument, mode: str) -> str:
    v = doc.verdicts
    inner = (
        f'<div class="parts two"><div>{_grid_part(v)}</div><div>{_disagreements_part(v)}</div></div>'
        f'<div class="parts" style="margin-top: var(--space-5)">{_calibration_part(v)}{_near_misses_part(v, mode)}</div>'
    )
    return section("verdicts", "Is it judging well for you", v.lead, inner)


def learning(doc: DashboardDocument) -> str:
    le = doc.learning
    concepts = "".join(
        f'<div class="concept"><h3>{esc(c.concept)} <span class="muted small">{c.count}</span></h3><ul>'
        + "".join(
            f'<li><a href="{href(i.url)}">{esc(i.title)}</a> <span class="secondary">{esc(i.lesson)}</span></li>'
            for i in c.items
        )
        + (f'<li class="muted">and {c.more} more</li>' if c.more else "")
        + "</ul></div>"
        for c in le.concepts
    )
    concepts_part = (
        (
            f'<div class="concepts">{concepts}</div>'
            + (
                f'<p class="muted small">and {le.concepts_more} more {"concept" if le.concepts_more == 1 else "concepts"}</p>'
                if le.concepts_more
                else ""
            )
        )
        if le.concepts
        else empty(le.concepts_empty)
    )
    top = max((c.count for c in le.cloud), default=1)
    cloud = " ".join(
        f'<span class="{"frontier" if c.frontier else ""}" style="font-size:{13 + round(4 * c.count / top)}px" title="{esc("seen at 70 and above this fortnight, not yet read" if c.frontier else "read or starred in the last 90 days")}">{esc(c.concept)}<span class="n">{c.count}</span></span>'
        for c in le.cloud
    )
    cloud_part = '<h3 style="margin-top: var(--space-5)">The concept map</h3>' + (
        f'<p class="cloud">{cloud}</p><p class="muted small">Dotted: the frontier, seen at 70 and above this fortnight and not yet read.</p>'
        if le.cloud
        else empty(le.cloud_empty)
    )
    p = le.profile
    diff = "".join(
        f'<div class="{d.kind}">{"+ " if d.kind == "add" else "− " if d.kind == "del" else "  "}{esc(d.text)}</div>'
        for d in p.diff
    )
    profile_part = (
        f'<h3 style="margin-top: var(--space-5)">The learned profile <span class="muted small">version {p.version if p.version is not None else "–"} · {esc(p.date_text)}</span></h3>'
        f"<details><summary>{icons.icon('chevron')}Show the profile</summary><div class='profile-text'>{esc(p.text) or esc('(empty)')}</div></details>"
        f"<details><summary>{icons.icon('chevron')}Show the Sunday diff</summary>"
        + (f'<div class="diff">{diff}</div>' if diff else empty(p.diff_empty))
        + "</details>"
    )
    return section("learning", "What it is learning", le.lead, concepts_part + cloud_part + profile_part)


def feeds(doc: DashboardDocument) -> str:
    f = doc.feeds
    if not f.rows:
        return section("feeds", "Your feeds", f.lead, empty(f.empty_text))
    rows = []
    for r in f.rows:
        cells = [
            esc(r.feed),
            esc(r.folder or "–"),
            f"{r.articles:,}",
            band_chip(r.mean_score, r.mean_band)
            if r.mean_score is not None and r.mean_band is not None
            else "–",
            esc(pct(r.cut_share)),
            esc(pct(r.read_share)),
            esc(pct(r.star_share)),
        ]
        rows.append(cells)
    tbl = table(
        [
            ("Feed", "text"),
            ("Folder", "text"),
            ("Articles", "num"),
            ("Mean score", "num"),
            ("Cut", "num"),
            ("Read", "num"),
            ("Starred", "num"),
        ],
        rows,
        f"Your feeds in the last {f.days} days",
        sortable=True,
        extra_class="feeds-table",
    )
    proposals = "".join(
        f'<p class="secondary small">{esc(r.feed)}: {esc(r.proposal)}</p>' for r in f.rows if r.proposal
    )
    return section(
        "feeds",
        "Your feeds",
        f.lead,
        '<input class="filter js-only" type="search" placeholder="Filter feeds" aria-label="Filter feeds" data-filter="feeds">'
        f'<div class="table-wrap">{tbl}</div>{proposals}',
    )


def money(doc: DashboardDocument, mode: str = "console") -> str:
    m = doc.money
    chart = _columns(m)
    tiles = (
        '<div class="tiles" style="margin-top: var(--space-4)">'
        + tile("Per 1,000 judged, this week", m.per_1000_text)
        + tile("Cache hits, this week", m.cache_hit_text)
        + tile("Spend, 30 days", m.month_text)
        + "</div>"
    )
    rows = [[esc(d.label), esc(usd(d.usd))] for d in m.days]
    twin = table([("Day", "text"), ("Spend", "num")], rows, "Spend per day, last 30 days", cards=False)
    inner = (
        f'<figure><div class="chart" data-chart="spend">{chart}</div><figcaption>{esc(m.chart_title)}'
        f"<details><summary>{icons.icon('chevron')}Show as table</summary>{twin}</details></figcaption></figure>{tiles}"
        if any(d.usd for d in m.days)
        else empty(m.empty_text) + tiles
    )
    return section("money", "What it costs and how it runs", m.lead, inner + machinery(doc, mode))


def _columns(m: object) -> str:
    """The spend chart: one series, hairline grid, the budget as a labelled reference line,
    labels on the maximum and today only; every column is focusable with its readout."""
    from .document import Money

    assert isinstance(m, Money)
    w, h, left, bottom, top = 720, 180, 40, 24, 12
    days = m.days
    top_usd = max(*(d.usd for d in days), 0.01)
    budget_fits = m.budget_usd <= top_usd * 1.6
    peak = (max(top_usd, m.budget_usd) if budget_fits else top_usd) * 1.12
    inner_w, inner_h = w - left - 8, h - top - bottom
    step = inner_w / len(days)
    bar_w = min(20, step - 2)

    def y_of(v: float) -> float:
        return top + inner_h - inner_h * v / peak

    grid = "".join(
        f'<line class="grid" x1="{left}" x2="{w - 8}" y1="{y_of(peak * k / 4):.1f}" y2="{y_of(peak * k / 4):.1f}"/>'
        for k in range(1, 5)
    )
    ticks = "".join(
        f'<text x="{left - 6}" y="{y_of(peak * k / 4) + 4:.1f}" text-anchor="end">${peak * k / 4:.2f}</text>'
        for k in (2, 4)
    )
    top_day = max(days, key=lambda d: d.usd)
    cols = []
    for i, d in enumerate(days):
        x = left + i * step + (step - bar_w) / 2
        y = y_of(d.usd)
        label = ""
        if d is top_day or i == len(days) - 1:
            label = f'<text x="{x + bar_w / 2:.1f}" y="{y - 4:.1f}" text-anchor="middle">${d.usd:.2f}</text>'
        cols.append(
            f'<g class="col-group"><rect class="col" tabindex="0" x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(0.0, top + inner_h - y):.1f}" rx="2" '
            f'data-tip="{esc(usd(d.usd))} · {esc(d.label)}"><title>{esc(usd(d.usd))} · {esc(d.label)}</title></rect>{label}</g>'
        )
    axis_labels = "".join(
        f'<text x="{left + i * step + step / 2:.1f}" y="{h - 6}" text-anchor="middle">{esc(days[i].label.split()[1] + " " + days[i].label.split()[2])}</text>'
        for i in (0, len(days) // 2, len(days) - 1)
    )
    ref = (
        f'<line class="ref" x1="{left}" x2="{w - 8}" y1="{y_of(m.budget_usd):.1f}" y2="{y_of(m.budget_usd):.1f}"/>'
        f'<text x="{w - 8}" y="{y_of(m.budget_usd) - 4:.1f}" text-anchor="end">budget {esc(usd(m.budget_usd))}</text>'
        if budget_fits
        else f'<text x="{w - 8}" y="{top + 8}" text-anchor="end">budget {esc(usd(m.budget_usd))} a day, above the chart</text>'
    )
    return (
        f'<svg class="columns" viewBox="0 0 {w} {h}" role="img" aria-label="Spend per day, last 30 days: {esc(m.chart_title)}">'
        f'{grid}{ticks}<line class="axis" x1="{left}" x2="{w - 8}" y1="{top + inner_h}" y2="{top + inner_h}"/>{"".join(cols)}{ref}{axis_labels}</svg>'
    )


def machinery(doc: DashboardDocument, mode: str) -> str:
    mc = doc.machinery
    batches = (
        table(
            [("Purpose", "text"), ("Articles", "num"), ("Age", "text")],
            [[esc(b.purpose), f"{b.articles:,}", esc(b.age_text)] for b in mc.batches],
            "Batches with the model",
            cards=False,
        )
        if mc.batches
        else empty(mc.batches_empty)
    )
    run_rows = [
        [
            esc(r.started_text),
            esc(r.kind),
            (f"{icons.icon('check')} ok" if r.ok else f"{icons.icon('warning')} failed"),
            esc(r.summary),
        ]
        for r in mc.runs
    ]
    runs = (
        table(
            [("Started", "text"), ("Kind", "text"), ("Result", "text"), ("Summary", "text")],
            run_rows,
            "The last twenty runs",
            extra_class="runs",
        )
        .replace(
            '<td class="text" data-label="Result">' + icons.icon("check"),
            '<td class="text ok" data-label="Result">' + icons.icon("check"),
        )
        .replace(
            '<td class="text" data-label="Result">' + icons.icon("warning"),
            '<td class="text failed" data-label="Result">' + icons.icon("warning"),
        )
        if run_rows
        else empty(mc.runs_empty)
    )
    log = ""
    if mode != "export":
        lines = "\n".join(esc(line) for line in mc.log) or "(the log is empty)"
        log = f'<h3 style="margin-top: var(--space-5)">The log</h3><pre class="log" id="log">{lines}</pre><p class="js-only"><button type="button" class="button quiet" data-action="log_more">Show 200 lines</button></p>'
    return (
        f'<div id="machinery" style="margin-top: var(--space-5)"><h3>Batches with the model</h3>'
        f'<p class="secondary small">{esc(mc.lead)}</p>{batches}<h3 style="margin-top: var(--space-5)">Runs</h3>{runs}{log}</div>'
    )


def footer(doc: DashboardDocument, mode: str) -> str:
    f = doc.footer
    parts = [f"bodh {esc(f.version)}"]
    if f.data_dir and mode != "export":
        parts.append(esc(f.data_dir))
    parts.append(f'<a href="{href(f.docs_url)}">the runbook</a>')
    parts.append(f'<a href="{href(f.repo_url)}">the repository</a>')
    if f.exported_text:
        parts.append(esc(f.exported_text))
    if mode == "console":
        parts.append('<span id="live-note" class="js-only"></span>')
    return '<footer class="footer">' + "".join(f"<span>{p}</span>" for p in parts) + "</footer>"
