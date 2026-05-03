#!/usr/bin/env python3
"""
Generate PNG icons for the Nexus Agent Chrome extension.

MVP approach: produce a simple, recognizable robot-face silhouette in each
state color, at three sizes (16/48/128). Visual polish is deferred to Phase 1.5.
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw


SIZES = (16, 48, 128)

# State → (background color, accent color)
STATES = {
    "icon-offline": ((148, 148, 148, 255), (40, 40, 40, 255)),      # grey
    "icon-connecting": ((245, 166, 35, 255), (90, 60, 0, 255)),     # amber
    "icon-idle": ((34, 197, 94, 255), (0, 80, 30, 255)),            # green
    "icon-active": ((168, 85, 247, 255), (40, 0, 80, 255)),         # purple
    "icon-alert": ((239, 68, 68, 255), (120, 0, 0, 255)),           # red
    # Base / default icon (matches idle green — ideal "brand" color)
    "icon": ((34, 197, 94, 255), (0, 80, 30, 255)),
}

HERE = Path(__file__).resolve().parent.parent
ICONS_DIR = HERE / "icons"
ICONS_DIR.mkdir(parents=True, exist_ok=True)


def draw_icon(size: int, bg_rgba, accent_rgba) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded-square background
    radius = max(2, size // 6)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=bg_rgba)

    # Robot-face geometry (scaled)
    def s(x): return int(round(x * size / 16))

    # Antenna
    d.rectangle((s(7.5), s(2), s(8.5), s(3.5)), fill=accent_rgba)
    d.ellipse((s(6.5), s(1), s(9.5), s(3)), fill=accent_rgba)

    # Head outline (inner)
    d.rounded_rectangle(
        (s(3), s(4), s(13), s(13)),
        radius=max(1, s(1.2)),
        outline=accent_rgba,
        width=max(1, size // 24),
    )

    # Eyes
    eye_r = max(1, s(0.9))
    d.ellipse((s(5) - eye_r, s(7.5) - eye_r, s(5) + eye_r, s(7.5) + eye_r), fill=accent_rgba)
    d.ellipse((s(11) - eye_r, s(7.5) - eye_r, s(11) + eye_r, s(7.5) + eye_r), fill=accent_rgba)

    # Mouth
    d.rectangle((s(6), s(10.5), s(10), s(11.3)), fill=accent_rgba)

    return img


def main() -> None:
    for name, (bg, accent) in STATES.items():
        for size in SIZES:
            img = draw_icon(size, bg, accent)
            out = ICONS_DIR / f"{name}-{size}.png"
            img.save(out, "PNG", optimize=True)
            print(f"wrote {out}")


if __name__ == "__main__":
    main()
