"""Post-hoc Pillow annotation of a Playwright screenshot.

Draws dashed numbered boxes over interactive elements. Mirrors browser-use's
design choice (`browser/python_highlights.py`) of painting in Python rather
than overlaying JS: lets the live DOM stay untouched, and lets us re-annotate
any saved screenshot offline for replay.

Three details that catch DIY attempts:

  1. CSS-pixel-to-device-pixel scaling. Element rects come back in CSS
     pixels; the PNG bytes Playwright returns are in device pixels. On a
     2x display every box position must be multiplied by the page's
     `devicePixelRatio`, otherwise the boxes drift up and left.

  2. Dashed lines for the box edge. Solid edges merge visually when two
     interactives overlap; dashed segments stay separable.

  3. Number badge as filled rect + outline + white text. Plain text on a
     busy background is the #1 reason VLMs misread the index.
"""
from __future__ import annotations

import base64
from io import BytesIO
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

from .dom import Element


# Tag → (border, badge_fill) RGB. Tag-keyed palette gives the VLM a free
# type hint (link blue / button green / input orange / …).
_PALETTE: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "a":        ((46, 134, 193), (33, 97,  140)),
    "button":   ((39, 174, 96),  (24, 106, 59)),
    "input":    ((230, 126, 34), (175, 96,  26)),
    "textarea": ((230, 126, 34), (175, 96,  26)),
    "select":   ((155, 89, 182), (113, 65,  133)),
    "label":    ((46, 134, 193), (33, 97,  140)),
}
_DEFAULT = ((192, 57, 43), (146, 43, 33))   # red


def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _draw_dashed_rect(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
    width: int = 2,
    dash: int = 8,
    gap: int = 5,
) -> None:
    x1, y1, x2, y2 = box
    def seg(p0, p1):
        # Walk along the segment from p0 to p1 painting dash/gap chunks.
        ax, ay = p0
        bx, by = p1
        dx, dy = bx - ax, by - ay
        length = (dx ** 2 + dy ** 2) ** 0.5
        if length == 0:
            return
        ux, uy = dx / length, dy / length
        n = 0
        cursor = 0.0
        while cursor < length:
            stop = min(cursor + dash, length)
            sx, sy = ax + ux * cursor, ay + uy * cursor
            ex, ey = ax + ux * stop,   ay + uy * stop
            draw.line([(sx, sy), (ex, ey)], fill=color, width=width)
            cursor = stop + gap
            n += 1
    seg((x1, y1), (x2, y1))
    seg((x2, y1), (x2, y2))
    seg((x2, y2), (x1, y2))
    seg((x1, y2), (x1, y1))


def annotate(
    screenshot_png: bytes,
    elements: Iterable[Element],
    dpr: float,
) -> bytes:
    """Return a new PNG (bytes) with dashed numbered boxes drawn over each element."""
    img = Image.open(BytesIO(screenshot_png)).convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")
    font = _font(max(12, int(14 * dpr)))

    for el in elements:
        border, badge = _PALETTE.get(el.tag, _DEFAULT)
        x1 = int(el.x * dpr)
        y1 = int(el.y * dpr)
        x2 = int((el.x + el.w) * dpr)
        y2 = int((el.y + el.h) * dpr)
        # Clamp to image bounds
        W, H = img.size
        x1 = max(0, min(W - 1, x1))
        y1 = max(0, min(H - 1, y1))
        x2 = max(0, min(W - 1, x2))
        y2 = max(0, min(H - 1, y2))
        _draw_dashed_rect(draw, (x1, y1, x2, y2), border, width=2)

        # Badge: filled rect with the id number, anchored to top-left of bbox.
        label = str(el.id)
        try:
            tw, th = draw.textbbox((0, 0), label, font=font)[2:]
        except AttributeError:
            tw, th = font.getsize(label)  # Pillow < 10
        pad = 3
        bw, bh = tw + 2 * pad, th + 2 * pad
        bx1, by1 = x1, max(0, y1 - bh)
        bx2, by2 = bx1 + bw, by1 + bh
        draw.rectangle((bx1, by1, bx2, by2), fill=badge + (235,))
        draw.rectangle((bx1, by1, bx2, by2), outline=(255, 255, 255, 255), width=1)
        draw.text((bx1 + pad, by1 + pad - 1), label, fill=(255, 255, 255), font=font)

    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def to_data_url(png_bytes: bytes) -> str:
    return f"data:image/png;base64,{base64.b64encode(png_bytes).decode()}"
