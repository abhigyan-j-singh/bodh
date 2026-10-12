"""The page in a browser: screenshots of every state at three widths in light and dark with
Reduce Motion on, compared with the baselines under `tests/visual/baseline/` at a 0.1 %
pixel tolerance (`BODH_VISUAL_UPDATE=1` rewrites them; a changed baseline is reviewed like
code); the keyboard order against the reading order; and the live layer end to end against a
console. Runs with `BODH_BROWSER=1` and Playwright's Chromium installed (or `BODH_CHROMIUM` naming
one); skipped otherwise."""

from __future__ import annotations

import base64
import itertools
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from bodh import locks
from bodh.console import Console

from .world import FIXED, STATES, freeze, make_world

pytestmark = pytest.mark.skipif(
    not os.environ.get("BODH_BROWSER"), reason="BODH_BROWSER=1 runs the browser tests (Playwright's Chromium)"
)

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "baseline"
OUT = HERE / "out"
TOLERANCE = 0.001  # the share of pixels that may differ
WIDTHS = {"idle": (390, 768, 1280), "running": (390, 1280), "attention": (390, 1280), "empty": (390, 1280)}
THEMES = {
    "idle": ("light", "dark"),
    "running": ("light",),
    "attention": ("light", "dark"),
    "empty": ("light",),
}

FAKE = "import sys; print('fake bodh', *sys.argv[1:])\n"


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        b = p.chromium.launch(
            executable_path=os.environ.get("BODH_CHROMIUM") or None
        )  # a Chromium Playwright did not install
        yield b
        b.close()


def serve(cfg, conn, tmp_path: Path) -> tuple[Console, str]:
    script = tmp_path / "fake_bodh.py"
    script.write_text(FAKE, encoding="utf-8")
    console = Console(cfg, conn, command=[sys.executable, str(script)], host="127.0.0.1", port=0)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        console.port = sock.getsockname()[1]
    threading.Thread(target=console.serve, daemon=True).start()
    base = f"http://127.0.0.1:{console.port}"
    for _ in range(50):
        try:
            with urllib.request.urlopen(base + "/api/tick", timeout=5):
                break
        except OSError:
            time.sleep(0.1)
    return console, base


def diff_share(page, a: bytes, b: bytes) -> float:
    """The share of pixels that differ by more than a little in any channel, counted in the
    browser (two images on canvases), so the test needs no image library."""
    return float(
        page.evaluate(
            """async ([a, b]) => {
              const load = (src) => new Promise((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = no; i.src = src; });
              const [ia, ib] = await Promise.all([load(a), load(b)]);
              const w = Math.max(ia.width, ib.width), h = Math.max(ia.height, ib.height);
              const draw = (img) => { const c = document.createElement('canvas'); c.width = w; c.height = h; const g = c.getContext('2d'); g.drawImage(img, 0, 0); return g.getImageData(0, 0, w, h).data; };
              const da = draw(ia), db = draw(ib);
              let bad = 0;
              for (let i = 0; i < da.length; i += 4) {
                if (Math.abs(da[i] - db[i]) > 8 || Math.abs(da[i + 1] - db[i + 1]) > 8 || Math.abs(da[i + 2] - db[i + 2]) > 8) bad++;
              }
              return bad / (w * h);
            }""",
            [
                "data:image/png;base64," + base64.b64encode(a).decode(),
                "data:image/png;base64," + base64.b64encode(b).decode(),
            ],
        )
    )


def open_page(browser, base: str, width: int, theme: str, path: str = "/"):
    ctx = browser.new_context(
        viewport={"width": width, "height": 900},
        color_scheme=theme,
        reduced_motion="reduce",
        device_scale_factor=1,
    )
    page = ctx.new_page()
    page.clock.install(time=FIXED)
    page.goto(base + path)
    page.wait_for_function("document.documentElement.classList.contains('js')")
    page.clock.run_for(1500)  # the stamp ticks once
    return ctx, page


def check(page, name: str, full_page: bool = False) -> None:
    """Screenshot `name` against its baseline. The first screen by default (what a visitor
    sees first and what carries the design); the whole page only where asked, since a
    full-page baseline is a megabyte the repository keeps for ever."""
    OUT.mkdir(exist_ok=True)
    shot = page.screenshot(full_page=full_page)
    (OUT / f"{name}.png").write_bytes(shot)
    baseline = BASELINE / f"{name}.png"
    if os.environ.get("BODH_VISUAL_UPDATE") or not baseline.exists():
        BASELINE.mkdir(exist_ok=True)
        baseline.write_bytes(shot)
        return
    share = diff_share(page, baseline.read_bytes(), shot)
    assert share <= TOLERANCE, (
        f"{name}: {share:.2%} of the pixels differ from the baseline (see tests/visual/out/)"
    )


@pytest.mark.parametrize("state", STATES)
def test_the_page_in_every_state_matches_its_baselines(browser, state, tmp_path, monkeypatch):
    cfg, conn = make_world(tmp_path / state, state)
    freeze(monkeypatch)
    if state == "running":
        monkeypatch.setattr(
            locks,
            "running_process",
            lambda data_dir: {"pid": 1, "kind": "run", "started": FIXED.timestamp() - 300},
        )
    console, base = serve(cfg, conn, tmp_path)
    try:
        for width in WIDTHS[state]:
            for theme in THEMES[state]:
                ctx, page = open_page(browser, base, width, theme)
                try:
                    if state == "running":
                        assert "Judging 240 of 1,200" in page.locator("#state-line").text_content()
                        assert page.locator("button[data-action=stop]").is_visible()
                    if state == "attention":
                        assert page.locator("#attention .banner.critical").count() == 1
                    if state == "empty":
                        assert "Nothing yet today" in page.locator("#today .lead").text_content()
                    check(page, f"page-{state}-{width}-{theme}")
                    if state == "idle" and theme == "light" and width == 1280:
                        check(page, f"page-{state}-{width}-{theme}-full", full_page=True)
                finally:
                    ctx.close()
    finally:
        conn.close()


def test_the_stale_state_and_the_panels(browser, tmp_path, monkeypatch):
    cfg, conn = make_world(tmp_path / "idle", "idle")
    freeze(monkeypatch)
    console, base = serve(cfg, conn, tmp_path)
    try:
        ctx, page = open_page(browser, base, 1280, "light")
        try:
            for anchor, _title in __import__("bodh.web.panels", fromlist=["SECTIONS"]).SECTIONS:
                OUT.mkdir(exist_ok=True)
                shot = page.locator(f"#{anchor}").screenshot()
                (OUT / f"panel-{anchor}.png").write_bytes(shot)
            # the style guide
            page.goto(base + "/design")
            page.wait_for_selector(".swatches")
            check(page, "design-1280-light", full_page=True)
        finally:
            ctx.close()
        # stale: the events and the ticks stop answering; two missed polling cycles later the
        # stamp turns and the state line says how old the numbers are, the render untouched
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900}, color_scheme="light", reduced_motion="reduce"
        )
        page = ctx.new_page()
        page.route("**/api/events", lambda route: route.abort())
        page.route("**/api/tick", lambda route: route.abort())
        page.clock.install(time=FIXED)
        page.goto(base + "/")
        page.wait_for_function("document.documentElement.classList.contains('js')")
        page.wait_for_function(
            "document.querySelector('#live-note').textContent.includes('unavailable')", timeout=30_000
        )
        assert not page.locator("#stamp").evaluate("el => el.classList.contains('stale')")
        page.clock.run_for(62_000)
        assert page.locator("#stamp").evaluate("el => el.classList.contains('stale')")
        assert page.locator("#state-line").text_content().startswith("The console is not answering")
        assert page.locator("#hero-unread").is_visible()
        check(page, "page-stale-1280-light")
        ctx.close()
    finally:
        conn.close()


def test_keyboard_order_follows_the_reading_order(browser, tmp_path, monkeypatch):
    cfg, conn = make_world(tmp_path / "small", "idle", articles=60)
    freeze(monkeypatch)
    console, base = serve(cfg, conn, tmp_path)
    try:
        ctx, page = open_page(browser, base, 1280, "light")
        try:
            page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
            seen: list[tuple[str, float, float, str]] = []
            for _ in range(600):
                page.keyboard.press("Tab")
                info = page.evaluate(
                    """() => { const e = document.activeElement; if (!e || e === document.body) return null;
                       const r = e.getBoundingClientRect(); const cls = e.getAttribute('class') || ''; const row = e.closest('.list-row');
                       return [e.tagName.toUpperCase() + (e.id ? '#' + e.id : '') + (cls ? '.' + cls.split(' ')[0] : ''), r.top + window.scrollY, r.left, row ? row.dataset.id : '']; }"""
                )
                if info is None or (seen and tuple(info) == seen[0]):
                    break
                seen.append(tuple(info))
            assert len(seen) > 60, seen
            tags = [s[0] for s in seen]
            assert tags[0].startswith("A.skip"), tags[:3]
            # every interactive part is reached: the levers, the filters, a verdict button, a sort header, a chart mark
            for part in ("BUTTON.button", "A", "SELECT", "INPUT.filter", "RECT.col", "SUMMARY", "DIV.bar"):
                assert any(t.startswith(part) for t in tags), part
            # the order is the reading order: down the page, within a line left to right (a 24 px slack for
            # inline wrapping); a list row is one unit (its verdict menu sits top right and comes after its content)
            drops = [
                (a, b)
                for a, b in itertools.pairwise(seen)
                if b[1] < a[1] - 24 and not (a[3] and a[3] == b[3])
            ]
            assert not drops, drops[:5]
        finally:
            ctx.close()
    finally:
        conn.close()


def test_the_live_layer_end_to_end(browser, tmp_path, monkeypatch):
    cfg, conn = make_world(tmp_path / "live", "idle", articles=600)
    (cfg.root / ".env").write_text(
        "BAZQUX_EMAIL=a@b.c\nBAZQUX_PASSWORD=x\nANTHROPIC_API_KEY=sk-ant-x\n", encoding="utf-8"
    )
    console, base = serve(cfg, conn, tmp_path)
    errors: list[str] = []
    try:
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light")
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url.replace(base, "")))
        page.goto(base + "/")
        page.wait_for_function("document.documentElement.classList.contains('js')")
        page.wait_for_timeout(500)
        assert requests == ["/", "/api/events"], requests  # one page, then the stream; no other host
        rows = page.locator("#list-rows .list-row").count()
        assert rows and page.locator("#list-rows .verdicts").count() == rows
        # the band filter asks the server; folder and sort are applied here
        with page.expect_response("**/list?band=90*"):
            page.click(".filters [data-band='90']")
        page.wait_for_timeout(200)
        scores = page.eval_on_selector_all(
            "#list-rows .list-row:not([hidden])", "els => els.map(e => Number(e.dataset.score))"
        )
        assert (
            min(scores) >= 90
            and page.locator(".filters [data-band='90']").get_attribute("aria-current") == "true"
        )
        with page.expect_response("**/list?band=50*"):
            page.click(".filters [data-band='50']")
        page.wait_for_timeout(200)
        before = page.locator("#list-rows .list-row").count()
        if page.locator("[data-action=list_more]").is_visible():
            with page.expect_response("**/list?band=50&offset=*"):
                page.click("[data-action=list_more]")
            page.wait_for_timeout(200)
            assert page.locator("#list-rows .list-row").count() > before
        page.select_option(".filters select[data-folder]", "News")
        assert set(
            page.eval_on_selector_all(
                "#list-rows .list-row:not([hidden])", "els => els.map(e => e.dataset.folder)"
            )
        ) <= {"News"}
        page.select_option(".filters select[data-folder]", "")
        page.click(".filters [data-sort='newest']")
        arrived = page.eval_on_selector_all("#list-rows .list-row", "els => els.map(e => e.dataset.arrived)")
        assert arrived == sorted(arrived, reverse=True)
        # the feeds: a filter and a sortable column
        page.click(".feeds-table th button[data-sort='2']")
        assert page.locator(".feeds-table th").nth(2).get_attribute("aria-sort") == "ascending"
        nums = page.eval_on_selector_all(
            ".feeds-table tbody tr td:nth-child(3)",
            "els => els.map(e => Number(e.textContent.replace(/,/g, '')))",
        )
        assert nums == sorted(nums)
        # tooltips on hover and on focus
        page.hover("svg.columns rect.col >> nth=3")
        assert page.locator(".tooltip").is_visible() and "$" in page.locator(".tooltip").text_content()
        page.focus(".bars .bar[data-tip] >> nth=0")
        assert "read of" in page.locator(".tooltip").text_content()
        # a verdict by the keyboard, kept across the refetch the verdict causes
        page.focus("#list-rows .list-row >> nth=0 >> .title a")
        page.keyboard.press("2")
        page.wait_for_function("document.querySelector('#notice').textContent.startsWith('recorded good')")
        page.wait_for_timeout(1500)
        assert (
            page.locator("#list-rows .list-row >> nth=0 >> button[data-verdict=good]").get_attribute(
                "aria-pressed"
            )
            == "true"
        )
        assert json.loads(urllib.request.urlopen(base + "/api/tick").read())["generation"] >= 2
        # a confirmation with the plan's numbers, cancelled
        page.click(".more summary")
        page.click("button[data-action=prune]")
        page.wait_for_selector("dialog[open]")
        assert page.locator("dialog[open] p").text_content().startswith("Cut ") and page.locator(
            "dialog[open] button.primary"
        ).text_content().startswith("Cut ")
        page.click("dialog[open] button.quiet")
        assert page.locator("dialog").count() == 0
        # a command of the console's own: its output streams into the page, then the page refetches
        page.click("button[data-action=report]")
        page.wait_for_function(
            "document.querySelector('#output') && !document.querySelector('#output').hidden"
        )
        assert "fake bodh report" in page.locator("#output").text_content()
        # the banner dismissal is remembered
        page.evaluate("localStorage.clear()")
        ctx.close()
    finally:
        conn.close()
    assert not errors, errors
