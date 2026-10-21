"""
Monthly awards, per club, from the games the bot saved that month:

  👑 Player of the Month  best average rating (+0.1 per squad MOTM vote won), min games played
  ⚽ Top scorer / 🅰️ Top assister
  🗳️ Squad MOTM king      most squad MOTM votes won
  📈 Most improved        biggest rise in average rating vs the month before
  🎮 Ever-present         most games played

Months follow the session timezone (e.g. Australia/Sydney), so "October" means October
for the squad. A club needs MIN_CLUB_GAMES games in the month for awards.
"""

import io
import json
import math
from datetime import datetime
from typing import Optional

from PIL import Image, ImageDraw

from db import connect
from fonts import font

MIN_CLUB_GAMES = 3
SCHEMA = """
CREATE TABLE IF NOT EXISTS monthly_awards (
    guild_id   TEXT NOT NULL,
    club_id    INTEGER NOT NULL,
    month      TEXT NOT NULL,            -- 'YYYY-MM'
    data       TEXT NOT NULL,            -- JSON of the awards
    message_id TEXT,
    posted_at  INTEGER,
    PRIMARY KEY (guild_id, club_id, month)
);
"""


def init_awards():
    with connect() as conn:
        conn.executescript(SCHEMA)


def month_bounds(year: int, month: int, tz) -> tuple[int, int]:
    start = datetime(year, month, 1, tzinfo=tz)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _player_rows(cid: int, start: int, end: int) -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute("""
            SELECT MIN(mp.name) AS name, COUNT(*) AS games, SUM(mp.goals) AS goals, SUM(mp.assists) AS assists,
                   AVG(mp.rating) AS rating
            FROM match_players mp JOIN matches m ON m.club_id = mp.club_id AND m.match_id = mp.match_id
            WHERE mp.club_id = ? AND m.ts >= ? AND m.ts < ? GROUP BY LOWER(mp.name)""", (cid, start, end)).fetchall()
    return {r["name"].casefold(): dict(r) for r in rows}


def _squad_motm(cid: int, start: int, end: int) -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute("SELECT winners FROM motm_polls WHERE closed=1 AND club_id=? AND closes_at>=? AND closes_at<?",
                            (cid, start, end)).fetchall()
    wins: dict[str, int] = {}
    for r in rows:
        for w in json.loads(r["winners"] or "[]"):
            wins[w.casefold()] = wins.get(w.casefold(), 0) + 1
    return wins


def compute(cid: int, year: int, month: int, tz) -> Optional[dict]:
    """The month's awards for one club, or None if it played fewer than MIN_CLUB_GAMES games."""
    start, end = month_bounds(year, month, tz)
    with connect() as conn:
        rec = conn.execute("""SELECT COUNT(*) AS games, SUM(result='W') AS w, SUM(result='D') AS d,
                                     SUM(result='L') AS l, SUM(our_goals) AS gf, SUM(opp_goals) AS ga
                              FROM matches WHERE club_id=? AND ts>=? AND ts<?""", (cid, start, end)).fetchone()
    if not rec or rec["games"] < MIN_CLUB_GAMES:
        return None
    players = _player_rows(cid, start, end)
    motm = _squad_motm(cid, start, end)
    need = max(3, math.ceil(0.3 * rec["games"]))
    eligible = [p for p in players.values() if p["games"] >= need and p["rating"] is not None]

    def pick(rows, key, value_key):
        rows = [r for r in rows if r.get(value_key)]
        return max(rows, key=key) if rows else None

    out = {"month": f"{year:04d}-{month:02d}", "record": {k: rec[k] or 0 for k in ("games", "w", "d", "l", "gf", "ga")},
           "min_games": need}
    best = max(eligible, key=lambda p: (p["rating"] + 0.1 * motm.get(p["name"].casefold(), 0),
                                        (p["goals"] or 0) + (p["assists"] or 0)), default=None)
    if best:
        out["potm"] = {"name": best["name"], "rating": round(best["rating"], 2), "games": best["games"],
                       "goals": best["goals"] or 0, "assists": best["assists"] or 0,
                       "motm": motm.get(best["name"].casefold(), 0)}
    every = list(players.values())
    top = pick(every, lambda p: (p["goals"], -p["games"]), "goals")
    if top:
        out["scorer"] = {"name": top["name"], "value": top["goals"], "games": top["games"]}
    top = pick(every, lambda p: (p["assists"], -p["games"]), "assists")
    if top:
        out["assister"] = {"name": top["name"], "value": top["assists"], "games": top["games"]}
    if motm:
        name_key, n = max(motm.items(), key=lambda kv: (kv[1], kv[0]))
        name = players.get(name_key, {}).get("name", name_key)
        out["motm_king"] = {"name": name, "value": n}
    top = max(every, key=lambda p: (p["games"], p["rating"] or 0), default=None)
    if top:
        out["ever_present"] = {"name": top["name"], "value": top["games"]}
    py, pm = previous_month(year, month)
    prev = _player_rows(cid, *month_bounds(py, pm, tz))
    gains = [(p["rating"] - prev[k]["rating"], p) for k, p in players.items()
             if k in prev and p["games"] >= 3 and prev[k]["games"] >= 3
             and p["rating"] is not None and prev[k]["rating"] is not None]
    gains = [g for g in gains if g[0] > 0]
    if gains:
        gain, p = max(gains, key=lambda g: g[0])
        out["improved"] = {"name": p["name"], "value": round(gain, 2), "rating": round(p["rating"], 2)}
    return out


def months_with_games(cid: int, tz, limit: int = 24) -> list[str]:
    """Past months (newest first, not counting this one) where this club played enough games for awards."""
    from collections import Counter
    with connect() as conn:
        stamps = [r[0] for r in conn.execute("SELECT ts FROM matches WHERE club_id=?", (cid,))]
    now = datetime.now(tz)
    this = f"{now.year:04d}-{now.month:02d}"
    counts = Counter(datetime.fromtimestamp(t, tz).strftime("%Y-%m") for t in stamps if t)
    return sorted((m for m, n in counts.items() if n >= MIN_CLUB_GAMES and m != this), reverse=True)[:limit]


def save(guild_id: str, cid: int, awards: dict, message_id: Optional[str], posted_at: int):
    with connect() as conn:
        conn.execute("""INSERT INTO monthly_awards (guild_id, club_id, month, data, message_id, posted_at)
                        VALUES (?,?,?,?,?,?) ON CONFLICT(guild_id, club_id, month) DO UPDATE SET
                        data=excluded.data, message_id=excluded.message_id, posted_at=excluded.posted_at""",
                     (guild_id, cid, awards["month"], json.dumps(awards), message_id, posted_at))


def already_posted(guild_id: str, cid: int, month: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM monthly_awards WHERE guild_id=? AND club_id=? AND month=?",
                            (guild_id, cid, month)).fetchone() is not None


def month_label(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


# --------------------------------------------------------------------------- #
#  The card (1200x675)
# --------------------------------------------------------------------------- #
W, H = 1200, 675
BG_TOP, BG_BOTTOM = (10, 18, 38), (22, 36, 66)
TEXT, MUTED, LINE = (240, 244, 250), (150, 162, 186), (48, 64, 98)
GOLD, GOLD_DARK = (255, 200, 60), (120, 90, 20)
TILE = (28, 44, 78)


def _fit(d, text, max_w, size, bold=True, min_size=16):
    while size > min_size and d.textlength(text, font=font(size, bold)) > max_w:
        size -= 2
    f = font(size, bold)
    while text and d.textlength(text, font=f) > max_w:
        text = text[:-2] + "…"
    return text, f


def _wrap(d, text, max_w):
    """Gamer tags have no spaces: shrink a little, then break onto a second line, then trim."""
    for size in (32, 28):
        f = font(size, True)
        if d.textlength(text, font=f) <= max_w:
            return [text], f
    f = font(22, True)
    first = ""
    for ch in text:
        if d.textlength(first + ch, font=f) > max_w:
            break
        first += ch
    rest = text[len(first):]
    while rest and d.textlength(rest, font=f) > max_w:
        rest = rest[:-2] + "…"
    return [first, rest] if rest else [first], f


def _crown(d, cx, cy, w, colour):
    h = w * 0.6
    pts = [(cx - w / 2, cy + h / 2), (cx - w / 2, cy - h / 4), (cx - w / 4, cy + h / 12), (cx, cy - h / 2),
           (cx + w / 4, cy + h / 12), (cx + w / 2, cy - h / 4), (cx + w / 2, cy + h / 2)]
    d.polygon(pts, fill=colour)
    for x in (cx - w / 2, cx, cx + w / 2):
        y = cy - h / 4 if x != cx else cy - h / 2
        d.ellipse([x - w / 14, y - w / 14, x + w / 14, y + w / 14], fill=colour)


def render_card(club_name: str, awards: dict) -> bytes:
    img = Image.new("RGB", (W, H), BG_TOP)
    d = ImageDraw.Draw(img)
    for y in range(H):   # vertical gradient
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    d.rectangle([0, 0, W, 8], fill=GOLD)

    title, f = _fit(d, f"{club_name.upper()}  ·  {month_label(awards['month']).upper()} AWARDS", 1100, 40)
    d.text((50, 34), title, font=f, fill=TEXT)
    r = awards["record"]
    d.text((50, 88), f"{r['games']} games  ·  W{r['w']}  D{r['d']}  L{r['l']}  ·  {r['gf']}–{r['ga']} goals",
           font=font(24), fill=MUTED)

    # Player of the Month panel
    d.rounded_rectangle([50, 140, W - 50, 330], radius=22, fill=GOLD_DARK)
    d.rounded_rectangle([54, 144, W - 54, 326], radius=20, fill=TILE)
    _crown(d, 140, 235, 110, GOLD)
    d.text((220, 160), "PLAYER OF THE MONTH", font=font(24, True), fill=GOLD)
    potm = awards.get("potm")
    if potm:
        name, f = _fit(d, potm["name"], 880, 64)
        d.text((220, 192), name, font=f, fill=TEXT)
        line = f"{potm['rating']:.2f} avg rating  ·  {potm['games']} games  ·  {potm['goals']}G {potm['assists']}A"
        if potm.get("motm"):
            line += f"  ·  {potm['motm']} squad MOTM"
        d.text((220, 275), line, font=font(24), fill=MUTED)
    else:
        d.text((220, 205), f"Nobody played {awards['min_games']}+ games", font=font(40, True), fill=MUTED)

    tiles = [("TOP SCORER", "scorer", lambda a: f"{a['value']} goals"),
             ("TOP ASSISTS", "assister", lambda a: f"{a['value']} assists"),
             ("SQUAD MOTM", "motm_king", lambda a: f"{a['value']} vote{'s' if a['value'] != 1 else ''} won"),
             ("MOST IMPROVED", "improved", lambda a: f"+{a['value']:.2f} rating"),
             ("EVER-PRESENT", "ever_present", lambda a: f"{a['value']} games")]
    tw, gap, top = (W - 100 - 4 * 20) / 5, 20, 360
    for i, (label, key, fmt) in enumerate(tiles):
        x = 50 + i * (tw + gap)
        d.rounded_rectangle([x, top, x + tw, top + 230], radius=18, fill=TILE)
        d.text((x + 18, top + 20), label, font=font(18, True), fill=GOLD)
        a = awards.get(key)
        if a:
            lines, f = _wrap(d, a["name"], tw - 36)
            for j, line in enumerate(lines):
                d.text((x + 18, top + 64 + j * 30), line, font=f, fill=TEXT)
            d.text((x + 18, top + 160), fmt(a), font=font(22), fill=MUTED)
        else:
            d.text((x + 18, top + 90), "—", font=font(40, True), fill=LINE)
    d.text((50, H - 48), f"Min {awards['min_games']} games for Player of the Month  ·  from every game the bot saved",
           font=font(18), fill=MUTED)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
