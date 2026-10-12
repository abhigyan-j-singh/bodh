"""Write CONFIG.md (every setting, pane by pane, with its default, range and help) from the
configuration models, so the document cannot drift from the code or from the Mac app's
Settings window: `python scripts/gen_config_md.py` writes it, `--check` exits 1 when the file
is stale (the test suite checks the same)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bodh.settings import render_config_md

OUT = Path(__file__).resolve().parents[1] / "CONFIG.md"


def text() -> str:
    return render_config_md()


def main() -> int:
    fresh = text()
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != fresh:
            print(f"{OUT} is stale; run python scripts/gen_config_md.py", file=sys.stderr)
            return 1
        print(f"{OUT} is current")
        return 0
    OUT.write_text(fresh, encoding="utf-8")
    print(f"wrote {OUT} ({len(fresh):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
