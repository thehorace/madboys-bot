"""
Draws a lineup onto a pitch as a PNG (Pillow), for /formation show,
/lineup suggest and /lineup confirm.

Coordinates are (x, y) as fractions of the pitch: x=0 left touchline,
x=1 right; y=0 the opponent's goal line (top), y=1 our goal line (bottom).
"""

import io
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

W, H = 720, 960
MARGIN = 40

_DEF4 = {"GK": (.5, .9), "RB": (.85, .7), "CB1": (.62, .75), "CB2": (.38, .75), "LB": (.15, .7)}

COORDS: dict[str, dict[str, tuple[float, float]]] = {
    "4-3-3": {**_DEF4, "CM1": (.72, .5), "CM2": (.5, .55), "CM3": (.28, .5),
              "RW": (.82, .25), "ST": (.5, .18), "LW": (.18, .25)},
    "4-4-2": {**_DEF4, "RM": (.86, .46), "CM1": (.62, .5), "CM2": (.38, .5), "LM": (.14, .46),
              "ST1": (.62, .2), "ST2": (.38, .2)},
    "4-2-3-1": {**_DEF4, "CDM1": (.62, .57), "CDM2": (.38, .57), "RAM": (.8, .37), "CAM": (.5, .37),
                "LAM": (.2, .37), "ST": (.5, .16)},
    "3-5-2": {"GK": (.5, .9), "CB1": (.72, .74), "CB2": (.5, .77), "CB3": (.28, .74),
              "RWB": (.88, .5), "CM1": (.66, .52), "CM2": (.5, .57), "CM3": (.34, .52), "LWB": (.12, .5),
              "ST1": (.62, .2), "ST2": (.38, .2)},
    "5-3-2": {"GK": (.5, .9), "RWB": (.88, .66), "CB1": (.7, .75), "CB2": (.5, .77), "CB3": (.3, .75),
              "LWB": (.12, .66), "CM1": (.7, .48), "CM2": (.5, .52), "CM3": (.3, .48),
              "ST1": (.62, .2), "ST2": (.38, .2)},
    "4-1-2-1-2": {**_DEF4, "CDM": (.5, .6), "CM1": (.72, .47), "CM2": (.28, .47), "CAM": (.5, .35),
                  "ST1": (.62, .18), "ST2": (.38, .18)},
}

GRASS_A, GRASS_B = (46, 125, 50), (56, 138, 60)
LINE = (235, 245, 235)
DOT_FILLED, DOT_EMPTY = (30, 144, 255), (120, 120, 120)


def _font(size: int, bold: bool = False):
    for name in (("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _px(x: float, y: float) -> tuple[int, int]:
    return int(MARGIN + x * (W - 2 * MARGIN)), int(MARGIN + y * (H - 2 * MARGIN))


def _draw_pitch(d: ImageDraw.ImageDraw):
    stripes = 10
    for i in range(stripes):
        top = i * H // stripes
        d.rectangle([0, top, W, top + H // stripes], fill=GRASS_A if i % 2 else GRASS_B)
    l, t, r, b = MARGIN, MARGIN, W - MARGIN, H - MARGIN
    d.rectangle([l, t, r, b], outline=LINE, width=4)
    mid = (t + b) // 2
    d.line([l, mid, r, mid], fill=LINE, width=3)
    d.ellipse([W // 2 - 80, mid - 80, W // 2 + 80, mid + 80], outline=LINE, width=3)
    for goal_y, sign in ((t, 1), (b, -1)):
        bw, bh = 360, 150
        d.rectangle([W // 2 - bw // 2, min(goal_y, goal_y + sign * bh), W // 2 + bw // 2, max(goal_y, goal_y + sign * bh)],
                    outline=LINE, width=3)
        sw, sh = 170, 55
        d.rectangle([W // 2 - sw // 2, min(goal_y, goal_y + sign * sh), W // 2 + sw // 2, max(goal_y, goal_y + sign * sh)],
                    outline=LINE, width=3)


def _fit(d: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if d.textlength(text, font=font) <= max_w:
        return text
    while text and d.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def render_lineup(formation: str, slots: dict[str, Optional[str]], title: str = "") -> Optional[io.BytesIO]:
    """slots: {slot: display name or None}. Returns a PNG buffer, or None if the formation has no layout."""
    coords = COORDS.get(formation)
    if not coords:
        return None
    img = Image.new("RGB", (W, H), GRASS_A)
    d = ImageDraw.Draw(img)
    _draw_pitch(d)

    f_pos, f_name, f_title = _font(20, bold=True), _font(22, bold=True), _font(28, bold=True)
    for slot, name in slots.items():
        if slot not in coords:
            continue
        cx, cy = _px(*coords[slot])
        r = 30
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DOT_FILLED if name else DOT_EMPTY, outline="white", width=3)
        label = slot.rstrip("0123456789")
        d.text((cx, cy), label, font=f_pos, fill="white", anchor="mm")
        text = _fit(d, name or "—", f_name, 170)
        tw = d.textlength(text, font=f_name)
        d.rounded_rectangle([cx - tw / 2 - 8, cy + r + 4, cx + tw / 2 + 8, cy + r + 34], radius=8, fill=(0, 0, 0))
        d.text((cx, cy + r + 19), text, font=f_name, fill="white", anchor="mm")

    if title:
        tw = d.textlength(title, font=f_title)
        d.rounded_rectangle([W / 2 - tw / 2 - 12, 4, W / 2 + tw / 2 + 12, 40], radius=8, fill=(0, 0, 0))
        d.text((W / 2, 22), title, font=f_title, fill="white", anchor="mm")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf
