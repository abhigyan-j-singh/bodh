"""The app icon master: a teal squircle, the word बोध, a thin open book; 1024 px and the
smaller sizes beside it. `python3 mac/scripts/icon.py` rewrites mac/Resources/Icon/*.png;
`make icon` (on a Mac) turns them into AppIcon.icns with iconutil."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "Resources" / "Icon"
SIZE = 1024
FONTS = [
    "/usr/share/fonts/truetype/google-fonts/Poppins-Medium.ttf",
    "/Library/Fonts/Poppins-Medium.ttf",
    "/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc",
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
]


def main() -> None:
    pad, radius = 100, 190
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(bg).rounded_rectangle([pad, pad, SIZE - pad, SIZE - pad], radius=radius, fill=(15, 92, 96, 255))
    grad = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(pad, SIZE - pad):
        t = (y - pad) / (SIZE - 2 * pad)
        gd.line([(pad, y), (SIZE - pad, y)], fill=(255, 255, 255, int(70 * (1 - t))))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, SIZE - pad, SIZE - pad], radius=radius, fill=255)
    bg = Image.composite(Image.alpha_composite(bg, grad), bg, mask)
    img = Image.alpha_composite(img, bg)
    d = ImageDraw.Draw(img)
    font = None
    for path in FONTS:
        if os.path.exists(path):
            candidate = ImageFont.truetype(path, 400)
            if candidate.getlength("बोध") > 100:
                font = candidate
                break
    if font is None:
        raise SystemExit("no font with Devanagari found; install Poppins or point FONTS at one")
    text = "बोध"
    bbox = d.textbbox((0, 0), text, font=font)
    x = (SIZE - (bbox[2] - bbox[0])) / 2 - bbox[0]
    y = (SIZE - (bbox[3] - bbox[1])) / 2 - bbox[1] - 40
    d.text((x, y), text, font=font, fill=(255, 251, 240, 255))
    by, cx = SIZE - pad - 150, SIZE / 2
    d.line(
        [(cx - 200, by), (cx - 20, by - 26), (cx, by - 10), (cx + 20, by - 26), (cx + 200, by)],
        fill=(255, 251, 240, 200),
        width=14,
        joint="curve",
    )
    OUT.mkdir(parents=True, exist_ok=True)
    img.save(OUT / "icon-1024.png")
    for size in (16, 32, 64, 128, 256, 512):
        img.resize((size, size), Image.LANCZOS).save(OUT / f"icon-{size}.png")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
