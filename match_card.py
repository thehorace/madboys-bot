"""
Shareable result card image (PNG), attached to every full-time post and to
/lastgame. 1200x630 — Discord shows that size nicely in chat.

  ┌─────────────────────────────────────────────────────────┐
  │ LEAGUE • 30 SEP 2026                          FULL TIME │
  │     MADBOYS FC      3 – 1      RIVAL FC                  │
  │                      WIN                                 │
  │     Fauz 2G 1A · Ali 1G                                  │
  │ PLAYER RATINGS ──────────────────────────────────────── │
  │ [8.9] Fauz  MID  2G 1A ★     [7.2] Ali  FWD  1G          │
  │ ...                                                      │
  └─────────────────────────────────────────────────────────┘
"""

import io
from datetime import datetime, timezone
from typing import Optional

from PIL import Image, ImageDraw

from config import BOT_TZ, CLUB_NAME
from fonts import font
from match_data import MATCH_TYPE_LABEL, POS_SHORT, ParsedMatch, motm_of

W, H = 1200, 630

BG_TOP, BG_BOTTOM = (10, 18, 38), (22, 36, 66)
TEXT, MUTED, LINE = (240, 244, 250), (150, 162, 186), (48, 64, 98)
RESULT_COL = {"W": (31, 191, 117), "D": (244, 180, 0), "L": (231, 76, 60)}
RESULT_WORD = {"W": "WIN", "D": "DRAW", "L": "LOSS"}
GOLD = (255, 200, 60)


def rating_colour(r: Optional[float]) -> tuple[int, int, int]:
    if r is None:
        return (90, 100, 120)
    if r >= 8.0:
        return (31, 191, 117)
    if r >= 7.0:
        return (139, 195, 74)
    if r >= 6.0:
        return (244, 180, 0)
    return (231, 76, 60)


def _fit_font(d: ImageDraw.ImageDraw, text: str, max_w: int, size: int, min_size: int = 20, bold: bool = True):
    while size > min_size and d.textlength(text, font=font(size, bold)) > max_w:
        size -= 2
    f = font(size, bold)
    if d.textlength(text, font=f) > max_w:  # still too long at min size: trim
        while text and d.textlength(text + "…", font=f) > max_w:
            text = text[:-1]
        text += "…"
    return text, f


def _star(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill):
    import math
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    d.polygon(pts, fill=fill)


def _when(ts: int) -> str:
    if not ts:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(ts, ZoneInfo(BOT_TZ))
    except Exception:
        dt = datetime.fromtimestamp(ts, timezone.utc)
    return dt.strftime("%d %b %Y").upper()


def render_match_card(pm: ParsedMatch) -> io.BytesIO:
    img = Image.new("RGB", (W, H), BG_TOP)
    d = ImageDraw.Draw(img)

    # background: vertical gradient + result-coloured accent bar
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)))
    accent = RESULT_COL[pm.result]
    d.rectangle([0, 0, W, 8], fill=accent)

    # header
    header = f"{MATCH_TYPE_LABEL.get(pm.match_type, pm.match_type).upper()}  •  {_when(pm.ts)}".strip(" •")
    d.text((40, 30), header, font=font(22, True), fill=MUTED)
    d.text((W - 40, 30), "FULL TIME", font=font(22, True), fill=MUTED, anchor="ra")

    # score row
    score = f"{pm.our_goals} – {pm.opp_goals}"
    f_score = font(104, True)
    cy = 140
    d.text((W / 2, cy), score, font=f_score, fill=TEXT, anchor="mm")
    score_half = d.textlength(score, font=f_score) / 2
    name_w = int(W / 2 - score_half - 80)
    ours, f_ours = _fit_font(d, CLUB_NAME.upper(), name_w, 44)
    theirs, f_theirs = _fit_font(d, pm.opp_name.upper(), name_w, 44)
    d.text((W / 2 - score_half - 40, cy), ours, font=f_ours, fill=TEXT, anchor="rm")
    d.text((W / 2 + score_half + 40, cy), theirs, font=f_theirs, fill=MUTED, anchor="lm")

    # result pill
    word = RESULT_WORD[pm.result]
    f_pill = font(24, True)
    pw = d.textlength(word, font=f_pill) + 40
    d.rounded_rectangle([W / 2 - pw / 2, 206, W / 2 + pw / 2, 242], radius=18, fill=accent)
    d.text((W / 2, 224), word, font=f_pill, fill=(10, 18, 38), anchor="mm")

    # scorers line
    contrib = [p for p in sorted(pm.players, key=lambda p: (-p.goals, -p.assists)) if p.goals or p.assists]
    if contrib:
        parts = []
        for p in contrib:
            bits = ([f"{p.goals}G"] if p.goals else []) + ([f"{p.assists}A"] if p.assists else [])
            parts.append(f"{p.name} {' '.join(bits)}")
        line, f_line = _fit_font(d, "   •   ".join(parts), W - 120, 24, min_size=16, bold=False)
        d.text((W / 2, 272), line, font=f_line, fill=TEXT, anchor="mm")

    # ratings grid
    top = 310
    d.text((40, top), "PLAYER RATINGS", font=font(20, True), fill=MUTED)
    d.line([(240, top + 12), (W - 40, top + 12)], fill=LINE, width=2)

    motm = motm_of(pm)
    players = sorted(pm.players, key=lambda p: -(p.rating or 0))[:12]
    rows = (len(players) + 1) // 2 or 1
    row_h = min(44, (H - 40 - (top + 40)) // max(rows, 1))
    gutter = 40
    col_w = (W - 80 - gutter) // 2
    f_rate, f_small = font(22, True), font(19, False)
    for i, p in enumerate(players):
        col, row = i // rows, i % rows
        x = 40 + col * (col_w + gutter)
        y = top + 40 + row * row_h
        # rating badge
        d.rounded_rectangle([x, y, x + 64, y + row_h - 8], radius=8, fill=rating_colour(p.rating))
        d.text((x + 32, y + (row_h - 8) / 2), f"{p.rating:.1f}" if p.rating is not None else "–",
               font=f_rate, fill=(10, 18, 38), anchor="mm")
        # name + position
        name, f_n = _fit_font(d, p.name, 250, 24, min_size=16)
        d.text((x + 80, y + (row_h - 8) / 2), name, font=f_n, fill=TEXT, anchor="lm")
        nx = x + 80 + d.textlength(name, font=f_n) + 12
        pos = POS_SHORT.get((p.pos or "").lower(), (p.pos or "")[:3].upper())
        if pos:
            d.text((nx, y + (row_h - 8) / 2), pos, font=f_small, fill=MUTED, anchor="lm")
        # G/A + MOTM on the right of the column
        right = x + col_w - 16
        ga = " ".join(([f"{p.goals}G"] if p.goals else []) + ([f"{p.assists}A"] if p.assists else [])
                      + ([f"{p.saves} saves"] if (p.pos or "").lower() == "goalkeeper" and p.saves else []))
        if motm is p:
            _star(d, right, y + (row_h - 8) / 2, 13, GOLD)
            right -= 28
        if ga:
            d.text((right, y + (row_h - 8) / 2), ga, font=f_small, fill=TEXT, anchor="rm")

    d.text((W - 40, H - 22), f"{CLUB_NAME} • EA FC Pro Clubs", font=font(16), fill=MUTED, anchor="rm")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf
