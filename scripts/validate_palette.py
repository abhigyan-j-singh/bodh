"""Validate the design tokens by numbers: the six checks of the data-visualization method,
for the band ramp (ordinal) and the status trio, in light and in dark, plus the contrast of
every text and line role against its surface. Prints the report that docs/design.md records;
exits 1 when a check fails.

    .venv/bin/python scripts/validate_palette.py
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bodh.web.colour import contrast, delta_e, oklch, simulate
from bodh.web.tokens import BANDS, DARK, LIGHT

ADJACENT_NORMAL = 15.0  # OKLab dE x 100 between neighbouring steps of an ordinal ramp
ADJACENT_CVD = 8.0  # the same, as a protanope, a deuteranope or a tritanope sees it
PAIR_CATEGORICAL = 15.0  # between any two of the status colours
LIGHTNESS = (0.30, 0.97)  # OKLCH L of a mark that reads on either surface
CHROMA_FLOOR = 0.03  # a step with less chroma is a grey, not a colour
TEXT_AA = 4.5
NONTEXT = 3.0


def check(name: str, ok: bool, detail: str, failures: list[str], lines: list[str]) -> None:
    lines.append(f"{'pass' if ok else 'FAIL'}  {name}: {detail}")
    if not ok:
        failures.append(name)


def ramp_checks(theme: str, tokens: dict[str, str], failures: list[str], lines: list[str]) -> None:
    surface = tokens["surface-1"]
    steps = [tokens[f"band-{b}"] for b in reversed(BANDS)]  # 0 .. 90
    lines.append(f"\n{theme}: the band ramp {' '.join(steps)}")
    lums = [oklch(h)[0] for h in steps]
    chromas = [oklch(h)[1] for h in steps]
    check(
        "lightness band",
        all(LIGHTNESS[0] <= lum <= LIGHTNESS[1] for lum in lums),
        "L " + " ".join(f"{lum:.2f}" for lum in lums),
        failures,
        lines,
    )
    check(
        "chroma floor (the three mark steps; the two palest are chip washes, their names printed)",
        all(c >= CHROMA_FLOOR for c in chromas[2:]),
        "C " + " ".join(f"{c:.3f}" for c in chromas),
        failures,
        lines,
    )
    ordered = lums == sorted(lums) or lums == sorted(lums, reverse=True)
    check("one direction", ordered, "lightness runs one way, so more reads as more", failures, lines)
    adjacent = [delta_e(a, b) for a, b in itertools.pairwise(steps)]
    check(
        "adjacent separation, normal vision",
        min(adjacent) >= ADJACENT_NORMAL,
        "ΔE " + " ".join(f"{d:.1f}" for d in adjacent) + f" (floor {ADJACENT_NORMAL:.0f})",
        failures,
        lines,
    )
    for kind in ("protan", "deutan", "tritan"):
        sim = [simulate(h, kind) for h in steps]
        seen = [delta_e(a, b) for a, b in itertools.pairwise(sim)]
        check(
            f"adjacent separation, {kind}",
            min(seen) >= ADJACENT_CVD,
            "ΔE " + " ".join(f"{d:.1f}" for d in seen) + f" (floor {ADJACENT_CVD:.0f})",
            failures,
            lines,
        )
    marks = [tokens[f"band-{b}"] for b in (50, 70, 90)]
    ratios = [contrast(h, surface) for h in marks]
    check(
        "mark contrast (the three strongest steps, used as marks)",
        min(ratios) >= NONTEXT,
        " ".join(f"{r:.1f}" for r in ratios) + f" against {surface} (floor {NONTEXT:.0f}:1)",
        failures,
        lines,
    )
    washes = [contrast(tokens["text-primary"], tokens[f"band-{b}-wash"]) for b in BANDS]
    check(
        "chip text on every wash",
        min(washes) >= TEXT_AA,
        " ".join(f"{r:.1f}" for r in washes) + f" (floor {TEXT_AA}:1)",
        failures,
        lines,
    )


def status_checks(theme: str, tokens: dict[str, str], failures: list[str], lines: list[str]) -> None:
    surface = tokens["surface-1"]
    trio = {k: tokens[k] for k in ("good", "warning", "critical")}
    lines.append(f"\n{theme}: the status trio {trio}")
    pairs = [(a, b) for i, a in enumerate(trio) for b in list(trio)[i + 1 :]]
    seps = [delta_e(trio[a], trio[b]) for a, b in pairs]
    check(
        "pairwise separation",
        min(seps) >= PAIR_CATEGORICAL,
        " ".join(f"{a}/{b} {d:.1f}" for (a, b), d in zip(pairs, seps, strict=True)),
        failures,
        lines,
    )
    for kind in ("protan", "deutan"):
        seen = [delta_e(simulate(trio[a], kind), simulate(trio[b], kind)) for a, b in pairs]
        check(
            f"pairwise separation, {kind}",
            min(seen) >= ADJACENT_CVD,
            " ".join(f"{d:.1f}" for d in seen),
            failures,
            lines,
        )
    ratios = {k: contrast(v, surface) for k, v in trio.items()}
    check(
        "as a mark on the page (an icon, a border, a bar; the word beside it is text-primary)",
        min(ratios.values()) >= NONTEXT,
        " ".join(f"{k} {r:.1f}" for k, r in ratios.items()) + f" (floor {NONTEXT:.0f}:1)",
        failures,
        lines,
    )
    washes = {k: contrast(tokens["text-primary"], tokens[f"{k}-wash"]) for k in trio}
    check(
        "text on each wash",
        min(washes.values()) >= TEXT_AA,
        " ".join(f"{k} {r:.1f}" for k, r in washes.items()),
        failures,
        lines,
    )


def role_checks(theme: str, tokens: dict[str, str], failures: list[str], lines: list[str]) -> None:
    lines.append(f"\n{theme}: text and lines")
    for role, floor in (
        ("text-primary", 7.0),
        ("text-secondary", TEXT_AA),
        ("text-muted", TEXT_AA),
        ("accent", TEXT_AA),
    ):
        for surface in ("surface-1", "surface-2"):
            ratio = contrast(tokens[role], tokens[surface])
            check(f"{role} on {surface}", ratio >= floor, f"{ratio:.1f} (floor {floor}:1)", failures, lines)
    for role in ("line-strong", "series-muted", "focus"):
        ratio = contrast(tokens[role], tokens["surface-1"])
        check(
            f"{role} on surface-1", ratio >= NONTEXT, f"{ratio:.1f} (floor {NONTEXT:.0f}:1)", failures, lines
        )
    ratio = contrast(tokens["accent"], tokens["accent-track"])
    check("meter fill on its track", ratio >= NONTEXT, f"{ratio:.1f}", failures, lines)
    ratio = contrast(tokens["text-primary"], tokens["accent-wash"])
    check("text on accent-wash", ratio >= TEXT_AA, f"{ratio:.1f}", failures, lines)


def main() -> int:
    failures: list[str] = []
    lines: list[str] = ["bodh palette validation (OKLab; ΔE × 100; WCAG 2.x contrast)"]
    for theme, tokens in (("light", LIGHT), ("dark", DARK)):
        ramp_checks(theme, tokens, failures, lines)
        status_checks(theme, tokens, failures, lines)
        role_checks(theme, tokens, failures, lines)
    lines.append("")
    lines.append(f"{len(failures)} failure(s)" if failures else "every check passed")
    print("\n".join(lines))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
