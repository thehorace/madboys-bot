"""
Player of the Day: the standout player of a finished session, plus a card to post.

How the pick works (per player, over the games they played in that session):
    average match rating
  + 0.35 per goal or assist per game
  + 0.40 x win rate
  + 0.20 per squad MOTM vote won per game, 0.15 per EA man of the match per game
  + 0.30 x clean-sheet rate for keepers and defenders
Players must have played at least half of their club's games that session (min 1),
so one great cameo doesn't beat someone who carried the whole night.
"""

import io
import json
import math
from collections import defaultdict
from datetime import datetime
from typing import Optional

from PIL import Image, ImageDraw

from awards import BG_BOTTOM, BG_TOP, GOLD, GOLD_DARK, H, LINE, MUTED, TEXT, TILE, W, _fit
from clubs import club_for, match_key
from db import connect
from fonts import font

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_potd (
    session_id  INTEGER PRIMARY KEY,
    guild_id    TEXT NOT NULL,
    club_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    discord_id  TEXT,
    score       REAL NOT NULL,
    data        TEXT NOT NULL,
    message_id  TEXT,
    posted_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_potd_name ON session_potd (guild_id, name COLLATE NOCASE);
"""
DEFENSIVE = ("goalkeeper", "defender")


def init_potd():
    with connect() as conn:
        conn.executescript(SCHEMA)


def _squad_votes(keys: list[str]) -> dict[str, int]:
    """Squad MOTM wins per player (casefolded) in these matches."""
    if not keys:
        return {}
    wins: dict[str, int] = defaultdict(int)
    with connect() as conn:
        for r in conn.execute("SELECT winners FROM motm_polls WHERE closed=1 AND match_id IN "
                              "(SELECT value FROM json_each(?))", (json.dumps(keys),)):
            for w in json.loads(r["winners"] or "[]"):
                wins[w.casefold()] += 1
    return wins


def compute(matches: list[dict]) -> Optional[dict]:
    """matches: session match rows (club_id, match_id, ts, result, our_goals, opp_goals, opp_name)."""
    if not matches:
        return None
    by_id = {(m["club_id"], m["match_id"]): m for m in matches}
    club_games = defaultdict(int)
    for m in matches:
        club_games[m["club_id"]] += 1
    rows = []
    with connect() as conn:
        for cid in club_games:
            ids = [m["match_id"] for m in matches if m["club_id"] == cid]
            rows += [dict(r) for r in conn.execute(
                "SELECT club_id, match_id, name, pos, goals, assists, rating, saves, tackles_made, motm, clean_sheet "
                "FROM match_players WHERE club_id=? AND match_id IN (SELECT value FROM json_each(?))",
                (cid, json.dumps(ids)))]
    votes = _squad_votes([match_key(m["match_id"], m["club_id"]) for m in matches])

    players: dict[tuple, dict] = {}
    for r in rows:
        m = by_id[(r["club_id"], r["match_id"])]
        key = (r["club_id"], r["name"].casefold())
        p = players.setdefault(key, {"name": r["name"], "club_id": r["club_id"], "games": 0, "w": 0, "d": 0, "l": 0,
                                     "goals": 0, "assists": 0, "ratings": [], "saves": 0, "tackles": 0,
                                     "ea_motm": 0, "clean_sheets": 0, "defensive": 0, "best": None})
        p["games"] += 1
        p[m["result"].lower()] += 1
        p["goals"] += r["goals"] or 0
        p["assists"] += r["assists"] or 0
        p["saves"] += r["saves"] or 0
        p["tackles"] += r["tackles_made"] or 0
        p["ea_motm"] += r["motm"] or 0
        if r["rating"] is not None:
            p["ratings"].append(r["rating"])
        pos = (r["pos"] or "").lower()
        if pos in DEFENSIVE:
            p["defensive"] += 1
            p["clean_sheets"] += 1 if (r["clean_sheet"] or m["opp_goals"] == 0) else 0
        game_score = (r["rating"] or 0) + 0.3 * (r["goals"] or 0) + 0.2 * (r["assists"] or 0)
        if p["best"] is None or game_score > p["best"]["score"]:
            p["best"] = {"score": game_score, "rating": r["rating"], "goals": r["goals"] or 0,
                         "assists": r["assists"] or 0, "saves": r["saves"] or 0, "opp": m["opp_name"] or "Opponent",
                         "our": m["our_goals"], "their": m["opp_goals"], "result": m["result"]}

    ranked = []
    for p in players.values():
        need = max(1, math.ceil(club_games[p["club_id"]] / 2))
        if p["games"] < need or not p["ratings"]:
            continue
        g = p["games"]
        p["rating"] = sum(p["ratings"]) / len(p["ratings"])
        p["squad_motm"] = votes.get(p["name"].casefold(), 0)
        p["keeper"] = p["defensive"] * 2 >= g and p["saves"] > 0
        p["score"] = (p["rating"] + 0.35 * (p["goals"] + p["assists"]) / g + 0.4 * p["w"] / g
                      + 0.2 * p["squad_motm"] / g + 0.15 * p["ea_motm"] / g
                      + (0.3 * p["clean_sheets"] / g if p["defensive"] * 2 >= g else 0))
        p.pop("ratings")
        p["club_name"] = club_for(p["club_id"])["name"]
        ranked.append(p)
    if not ranked:
        return None
    ranked.sort(key=lambda p: (-p["score"], -(p["goals"] + p["assists"]), -p["games"], p["name"].casefold()))
    gf, ga = sum(m["our_goals"] for m in matches), sum(m["opp_goals"] for m in matches)
    return {"winner": ranked[0], "runners": ranked[1:3],
            "session": {"games": len(matches), "w": sum(m["result"] == "W" for m in matches),
                        "d": sum(m["result"] == "D" for m in matches), "l": sum(m["result"] == "L" for m in matches),
                        "gf": gf, "ga": ga, "ts": min(m["ts"] for m in matches)}}


def already_done(session_id: int) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM session_potd WHERE session_id=?", (session_id,)).fetchone() is not None


def save(guild_id: str, session_id: int, result: dict, discord_id: Optional[str], message_id: Optional[str], now: int):
    w = result["winner"]
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO session_potd (session_id,guild_id,club_id,name,discord_id,score,data,"
                     "message_id,posted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (session_id, guild_id, w["club_id"], w["name"], discord_id, w["score"], json.dumps(result),
                      message_id, now))


def count_for(guild_id: str, name: str) -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM session_potd WHERE guild_id=? AND name=? COLLATE NOCASE",
                            (guild_id, name)).fetchone()[0]


def leaders(guild_id: str, club_id: Optional[int] = None, limit: int = 5) -> list[tuple[str, int]]:
    where, args = "guild_id=?", [guild_id]
    if club_id is not None:
        where += " AND club_id=?"
        args.append(club_id)
    with connect() as conn:
        try:
            rows = conn.execute(f"SELECT MIN(name) name, COUNT(*) n FROM session_potd WHERE {where} "
                                f"GROUP BY LOWER(name) ORDER BY n DESC, MIN(posted_at) LIMIT ?", (*args, limit)).fetchall()
        except Exception:   # table not created yet
            return []
    return [(r["name"], r["n"]) for r in rows]


# --------------------------------------------------------------------------- #
#  The card (1200x675, same look as the monthly awards card)
# --------------------------------------------------------------------------- #
def _star(d, cx, cy, r, colour):
    pts = []
    for i in range(10):
        ang = math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rad * math.cos(ang), cy - rad * math.sin(ang)))
    d.polygon(pts, fill=colour)


def _chip(d, x, y, value, label):
    vf, lf = font(40, True), font(18, True)
    w = max(d.textlength(value, font=vf), d.textlength(label, font=lf)) + 44
    d.rounded_rectangle([x, y, x + w, y + 104], radius=16, fill=TILE)
    d.text((x + 22, y + 14), value, font=vf, fill=TEXT)
    d.text((x + 22, y + 66), label, font=lf, fill=GOLD)
    return x + w + 16


def render_card(result: dict, tz=None, wins_total: int = 0) -> bytes:
    w, s = result["winner"], result["session"]
    img = Image.new("RGB", (W, H), BG_TOP)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    d.rectangle([0, 0, W, 8], fill=GOLD)

    day = datetime.fromtimestamp(s["ts"], tz).strftime("%A %d %B %Y").replace(" 0", " ")
    d.text((50, 34), "PLAYER OF THE DAY", font=font(40, True), fill=GOLD)
    d.text((50, 88), f"{day}  ·  {s['games']} games  ·  W{s['w']} D{s['d']} L{s['l']}  ·  {s['gf']}–{s['ga']}",
           font=font(24), fill=MUTED)

    # hero panel
    d.rounded_rectangle([50, 140, W - 50, 330], radius=22, fill=GOLD_DARK)
    d.rounded_rectangle([54, 144, W - 54, 326], radius=20, fill=TILE)
    _star(d, 140, 235, 64, GOLD)
    name, f = _fit(d, w["name"], 880, 72)
    d.text((225, 168), name, font=f, fill=TEXT)
    club = w.get("club_name") or club_for(w["club_id"])["name"]
    sub = f"{club}  ·  {w['games']} game{'s' if w['games'] != 1 else ''}  ·  W{w['w']} D{w['d']} L{w['l']}"
    if wins_total > 1:
        sub += f"  ·  Player of the Day #{wins_total}"
    sub, f = _fit(d, sub, 880, 26, bold=False)
    d.text((225, 262), sub, font=f, fill=MUTED)

    # stat chips
    x = 50
    x = _chip(d, x, 360, f"{w['rating']:.1f}", "AVG RATING")
    x = _chip(d, x, 360, str(w["goals"]), "GOALS")
    x = _chip(d, x, 360, str(w["assists"]), "ASSISTS")
    if w.get("keeper"):
        x = _chip(d, x, 360, str(w["saves"]), "SAVES")
    if w["defensive"] * 2 >= w["games"]:
        x = _chip(d, x, 360, str(w["clean_sheets"]), "CLEAN SHEETS")
    elif w["tackles"]:
        x = _chip(d, x, 360, str(w["tackles"]), "TACKLES")
    if w.get("squad_motm") and x < W - 260:
        x = _chip(d, x, 360, str(w["squad_motm"]), "SQUAD MOTM")

    b = w.get("best")
    if b:
        bits = [f"{b['rating']:.1f} rating" if b["rating"] is not None else None,
                f"{b['goals']} goal{'s' if b['goals'] != 1 else ''}" if b["goals"] else None,
                f"{b['assists']} assist{'s' if b['assists'] != 1 else ''}" if b["assists"] else None,
                f"{b['saves']} saves" if w.get("keeper") and b["saves"] else None]
        line = f"Best game: {b['our']}–{b['their']} vs {b['opp']}  ·  " + "  ·  ".join(x for x in bits if x)
        line, f = _fit(d, line, W - 100, 26, bold=False)
        d.text((50, 495), line, font=f, fill=TEXT)

    if result["runners"]:
        d.line([(50, 550), (W - 50, 550)], fill=LINE, width=2)
        parts = [f"{i}. {r['name']} ({r['rating']:.1f})" for i, r in enumerate(result["runners"], start=2)]
        text, f = _fit(d, "Next best:  " + "     ".join(parts), W - 100, 24, bold=False)
        d.text((50, 572), text, font=f, fill=MUTED)
    d.text((50, H - 48), "Rating, goals + assists, wins, MOTM votes and clean sheets  ·  min half the session's games",
           font=font(18), fill=MUTED)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
