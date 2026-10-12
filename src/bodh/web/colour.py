"""Colour arithmetic for the design tokens: sRGB to and from OKLab and OKLCH, WCAG contrast,
and a colour-vision simulation, so the palette is validated by numbers (`scripts/
validate_palette.py`, the report in docs/design.md) rather than by eye. Pure functions, no
dependency."""

from __future__ import annotations

import math

Rgb = tuple[float, float, float]  # 0..1 linear or encoded, as named


def hex_to_rgb(text: str) -> Rgb:
    text = text.lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    return int(text[0:2], 16) / 255, int(text[2:4], 16) / 255, int(text[4:6], 16) / 255


def rgb_to_hex(rgb: Rgb) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb)


def _to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _from_linear(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def luminance(hex_colour: str) -> float:
    r, g, b = (_to_linear(c) for c in hex_to_rgb(hex_colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG 2.x contrast ratio between two colours, 1 to 21."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# -- OKLab -----------------------------------------------------------------------------


def rgb_to_oklab(hex_colour: str) -> tuple[float, float, float]:
    r, g, b = (_to_linear(c) for c in hex_to_rgb(hex_colour))
    l_ = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m_ = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s_ = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (math.copysign(abs(x) ** (1 / 3), x) for x in (l_, m_, s_))
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def oklab_to_rgb(lab: tuple[float, float, float]) -> Rgb:
    """Linear sRGB may fall outside 0..1 for a colour the gamut cannot show; `in_gamut` says."""
    lum, a, b = lab
    l_ = lum + 0.3963377774 * a + 0.2158037573 * b
    m_ = lum - 0.1055613458 * a - 0.0638541728 * b
    s_ = lum - 0.0894841775 * a - 1.2914855480 * b
    l_, m_, s_ = l_**3, m_**3, s_**3
    return (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )


def oklch_to_hex(lum: float, chroma: float, hue_deg: float) -> str:
    """The nearest sRGB colour: chroma is lowered until the colour fits the gamut."""
    for step in range(60):
        c = chroma * (1 - step / 60)
        h = math.radians(hue_deg)
        lin = oklab_to_rgb((lum, c * math.cos(h), c * math.sin(h)))
        if all(-0.0005 <= x <= 1.0005 for x in lin):
            return rgb_to_hex(tuple(_from_linear(x) for x in lin))  # type: ignore[arg-type]
    lin = oklab_to_rgb((lum, 0.0, 0.0))
    return rgb_to_hex(tuple(_from_linear(x) for x in lin))  # type: ignore[arg-type]


def oklch(hex_colour: str) -> tuple[float, float, float]:
    lum, a, b = rgb_to_oklab(hex_colour)
    return lum, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


def delta_e(a: str, b: str) -> float:
    """OKLab distance, scaled by 100 so that 1 is about a just-noticeable step."""
    la, lb = rgb_to_oklab(a), rgb_to_oklab(b)
    return 100 * math.sqrt(sum((x - y) ** 2 for x, y in zip(la, lb, strict=True)))


# -- colour vision ---------------------------------------------------------------------

# Viénot, Brettel and Mollon (1999) in linear sRGB: what a protanope or a deuteranope sees
_PROTAN = ((0.1121, 0.8853, -0.0005), (0.1127, 0.8897, -0.0001), (0.0045, -0.0067, 1.1024))
_DEUTAN = ((0.2920, 0.7054, -0.0003), (0.2934, 0.7089, 0.0000), (-0.0209, 0.0256, 1.0019))
_TRITAN = ((1.0000, 0.1273, -0.1273), (0.0000, 0.8739, 0.1261), (0.0000, 0.8739, 0.1261))


def simulate(hex_colour: str, kind: str) -> str:
    matrix = {"protan": _PROTAN, "deutan": _DEUTAN, "tritan": _TRITAN}[kind]
    lin = tuple(_to_linear(c) for c in hex_to_rgb(hex_colour))
    out = tuple(sum(m * v for m, v in zip(row, lin, strict=True)) for row in matrix)
    return rgb_to_hex(tuple(_from_linear(x) for x in out))  # type: ignore[arg-type]
