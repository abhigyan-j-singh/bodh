"""The README's and the docs' pictures of the page, from the seeded fixture the visual tests
use, so they are reproducible and never stale: the first screen at 1280 px light and dark, the
phone at 390 px, each panel at 1280 px light, and the style guide, into `docs/images/`.
Dev only: needs Playwright and its Chromium (`pip install playwright && playwright install
chromium`; `BODH_CHROMIUM=/path/to/chrome` names one already installed).

    python scripts/screenshots.py            # writes docs/images/*.png
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from visual.world import FIXED, freeze, make_world  # noqa: E402

OUT = ROOT / "docs" / "images"


class _Patch:
    """`setattr` the way a pytest monkeypatch does, for `freeze`."""

    def setattr(self, target: object, name: str, value: object) -> None:
        setattr(target, name, value)


def main() -> int:
    from playwright.sync_api import sync_playwright

    from bodh.console import Console
    from bodh.web.panels import SECTIONS

    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        cfg, conn = make_world(Path(tmp) / "world", "idle")
        freeze(_Patch())
        script = Path(tmp) / "fake_bodh.py"
        script.write_text("import sys; print('bodh', *sys.argv[1:])\n", encoding="utf-8")
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
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("BODH_CHROMIUM") or None)
            shots = [
                ("page-1280-light", 1280, "light", "/"),
                ("page-1280-dark", 1280, "dark", "/"),
                ("page-390-light", 390, "light", "/"),
                ("page-390-dark", 390, "dark", "/"),
            ]
            for name, width, theme, path in shots:
                ctx = browser.new_context(
                    viewport={"width": width, "height": 900 if width > 390 else 844},
                    color_scheme=theme,
                    reduced_motion="reduce",
                )
                page = ctx.new_page()
                page.clock.install(time=FIXED)
                page.goto(base + path)
                page.wait_for_function("document.documentElement.classList.contains('js')")
                page.clock.run_for(1500)
                page.screenshot(path=str(OUT / f"{name}.png"))
                if width == 1280 and theme == "light":
                    page.evaluate(
                        "document.querySelector('.nav').style.visibility = 'hidden'"
                    )  # sticky: it would land mid-panel
                    for anchor, _title in SECTIONS:
                        page.locator(f"#{anchor}").screenshot(path=str(OUT / f"panel-{anchor}.png"))
                    page.goto(base + "/design")
                    page.wait_for_selector(".swatches")
                    page.screenshot(path=str(OUT / "design-1280-light.png"), full_page=True)
                ctx.close()
            browser.close()
        conn.close()
    for path in sorted(OUT.glob("*.png")):
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
