"""Write the dashboard document's JSON schema to docs/dashboard.schema.json from the pydantic
model, so the file cannot drift from the code: `python scripts/gen_schema.py` writes it,
`python scripts/gen_schema.py --check` exits 1 when the file is stale (the test suite checks the
same)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bodh.web.document import json_schema

OUT = Path(__file__).resolve().parents[1] / "docs" / "dashboard.schema.json"


def text() -> str:
    schema = json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://github.com/abhigyan-j-singh/bodh/blob/main/docs/dashboard.schema.json"
    return json.dumps(schema, indent=1, sort_keys=True) + "\n"


def main() -> int:
    fresh = text()
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != fresh:
            print(f"{OUT} is stale; run python scripts/gen_schema.py", file=sys.stderr)
            return 1
        print(f"{OUT} is current")
        return 0
    OUT.write_text(fresh, encoding="utf-8")
    print(f"wrote {OUT} ({len(fresh):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
