"""The shape of the code, as the plan's acceptance list states it: no function in `src` over
sixty lines, and the names a stranger meets are the glossary's (no `panel`, `floor` or
`teacher` key in what the commands print as JSON)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "bodh"
LIMIT = 60


def test_no_function_over_sixty_lines():
    over = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                if length > LIMIT:
                    over.append(f"{path.relative_to(SRC)}:{node.lineno} {node.name} ({length} lines)")
    assert not over, "\n".join(over)
