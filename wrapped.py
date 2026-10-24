"""
Season Wrapped: one player's season on a shareable card (like Spotify Wrapped).

The season is the FC game year: it starts on 1 September (FC 27 = September 2026 onwards)
and the card shows the season "so far" until the next one begins. Built only from games the
bot saved, for every club the player turned out for. Forfeits/void games never count.
"""

import io
from collections import Counter
from datetime import datetime
from typing import Optional

from PIL import Image, ImageDraw

from awards import BG_BOTTOM, BG_TOP, GOLD, GOLD_DARK, LINE, MUTED, TEXT, TILE, _fit
from clubs import club_for, monitored_clubs
from db import connect
from fonts import font

SEASON_START_MONTH = 9
POS_NAME = {"goalkeeper": "Goalkeeper", "defender": "Defence", "midfielder": "Midfield", "forward": "Attack"}


def season_bounds(now: datetime) -> tuple[int, str]:
    """(start timestamp, label like 'FC 27') for the season `now` falls in."""
    year = now.year if now.month >= SEASON_START_MONTH else now.year - 1
    start = now.replace(year=year, month=SEASON_START_MONTH, day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), f"FC {str(year + 1)[-2:]}"


def _games(name: str, since: int) -> list[dict]:
    rows = []
    with connect() as conn:
        for club in monitored_clubs():
            rows += [dict(r) for r in conn.execute("""
                SELECT mp.*, m.ts, m.result, m.our_goals, m.opp_goals, m.opp_name, m.opp_id
                FROM match_players mp CROSS JOIN matches m ON m.club_id=mp.club_id AND m.match_id=mp.match_id
                WHERE mp.club_id=? AND mp.name=? COLLATE NOCASE AND m.ts>=?""", (club["club_id"], name, since))]
    return sorted(rows, key=lambda r: r["ts"])


def _squad_rank(cid: int, since: int, name: str, key: str) -> Optional[int]:
    import match_data as md
    totals = [t for t in md.player_totals(cid, since) if (t.get(key) or 0) > 0]
    totals.sort(key=lambda t: -(t[key] or 0))
    for i, t in enumerate(totals, start=1):
        if t["name"].casefold() == name.casefold():
            return i
    return None


def compute(guild_id: str, name: str, now: datetime) -> Optional[dict]:
    since, label = season_bounds(now)
    games = _games(name, since)
    if not games:
        return None
    n = len(games)
    res = Counter(g["result"] for g in games)
    rated = [g for g in games if g["rating"] is not None]
    clubs = Counter(g["club_id"] for g in games)
    main_club = clubs.most_common(1)[0][0]
    pos = Counter((g["pos"] or "").lower() for g in games if g["pos"])
    fav_pos, fav_n = pos.most_common(1)[0] if pos else ("", 0)
    best = max(rated, key=lambda g: (g["rating"], g["goals"] or 0, g["assists"] or 0, -g["ts"])) if rated else None
    wins = [g for g in games if g["result"] == "W"]
    biggest = max(wins, key=lambda g: (g["our_goals"] - g["opp_goals"], g["our_goals"])) if wins else None
    vs = {}
    for g in games:
        v = vs.setdefault(g["opp_name"] or "Opponent", Counter())
        v[g["result"]] += 1
    nemesis = max(((o, c) for o, c in vs.items() if c["L"] >= 2), key=lambda oc: (oc[1]["L"] - oc[1]["W"], oc[1]["L"]),
                  default=None)
    defensive = sum(1 for g in games if (g["pos"] or "").lower() in ("goalkeeper", "defender"))

    from badges import _squad_motm_wins
    import insights
    try:
        partners = insights.partners_of(name, main_club, 1)
    except Exception:
        partners = []
    ranks = {k: _squad_rank(main_club, since, name, k) for k in ("goals", "assists", "games")}
    return {
        "name": games[-1]["name"], "season": label, "since": since, "until": games[-1]["ts"],
        "clubs": [club_for(c)["name"] for c, _ in clubs.most_common()],
        "games": n, "w": res["W"], "d": res["D"], "l": res["L"],
        "goals": sum(g["goals"] or 0 for g in games), "assists": sum(g["assists"] or 0 for g in games),
        "rating": sum(g["rating"] for g in rated) / len(rated) if rated else None,
        "ea_motm": sum(g["motm"] or 0 for g in games),
        "squad_motm": sum(1 for t in _squad_motm_wins(name) if t >= since),
        "clean_sheets": sum(1 for g in games if (g["pos"] or "").lower() in ("goalkeeper", "defender") and g["clean_sheet"]),
        "defensive": defensive,
        "saves": sum(g["saves"] or 0 for g in games),
        "hat_tricks": sum(1 for g in games if (g["goals"] or 0) >= 3),
        "potd": _potd_since(guild_id, name, since),
        "fav_pos": POS_NAME.get(fav_pos, fav_pos.title()), "fav_pos_pct": round(100 * fav_n / n) if n else 0,
        "best": {k: best[k] for k in ("rating", "goals", "assists", "opp_name", "our_goals", "opp_goals", "ts")} if best else None,
        "biggest_win": {k: biggest[k] for k in ("opp_name", "our_goals", "opp_goals", "ts")} if biggest else None,
        "nemesis": {"opp": nemesis[0], "w": nemesis[1]["W"], "d": nemesis[1]["D"], "l": nemesis[1]["L"]} if nemesis else None,
        "partner": ({"name": partners[0]["partner"], "games": partners[0]["games"],
                     "pct": round(100 * partners[0]["w"] / partners[0]["games"])} if partners else None),
        "ranks": ranks,
    }


def _potd_since(guild_id: str, name: str, since: int) -> int:
    with connect() as conn:
        try:
            return conn.execute("SELECT COUNT(*) FROM session_potd WHERE guild_id=? AND name=? COLLATE NOCASE "
                                "AND posted_at>=?", (guild_id, name, since)).fetchone()[0]
        except Exception:
            return 0


def highlights(w: dict) -> list[str]:
    """Plain-text story lines, used on the card and in the embed."""
    out = []
    b = w.get("best")
    if b:
        bits = [f"{b['goals']}G" if b["goals"] else "", f"{b['assists']}A" if b["assists"] else ""]
        extra = " ".join(x for x in bits if x)
        out.append(f"Best game: {b['rating']:.1f} vs {b['opp_name']} ({b['our_goals']}–{b['opp_goals']})"
                   + (f" · {extra}" if extra else ""))
    if w.get("fav_pos"):
        out.append(f"Home on the pitch: {w['fav_pos']} ({w['fav_pos_pct']}% of games)")
    p = w.get("partner")
    if p:
        out.append(f"Best partner: {p['name']} — {p['pct']}% wins in {p['games']} games together")
    bw = w.get("biggest_win")
    if bw:
        out.append(f"Biggest win: {bw['our_goals']}–{bw['opp_goals']} vs {bw['opp_name']}")
    ne = w.get("nemesis")
    if ne:
        out.append(f"Nemesis: {ne['opp']} (W{ne['w']} D{ne['d']} L{ne['l']})")
    ranks = [f"#{r} for {k}" for k, r in w["ranks"].items() if r and r <= 3]
    if ranks:
        out.append("Squad top 3: " + " · ".join(ranks))
    trophies = [f"Player of the Day ×{w['potd']}" if w["potd"] else "",
                f"Hat-tricks ×{w['hat_tricks']}" if w["hat_tricks"] else "",
                f"Squad MOTM ×{w['squad_motm']}" if w["squad_motm"] else ""]
    if any(trophies):
        out.append(" · ".join(t for t in trophies if t))
    return out


# --------------------------------------------------------------------------- #
#  The card (1080x1350 portrait, made for sharing)
# --------------------------------------------------------------------------- #
CW, CH = 1080, 1350


def render_card(w: dict, tz=None) -> bytes:
    img = Image.new("RGB", (CW, CH), BG_TOP)
    d = ImageDraw.Draw(img)
    for y in range(CH):
        t = y / CH
        d.line([(0, y), (CW, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    d.rectangle([0, 0, CW, 10], fill=GOLD)

    d.text((60, 50), f"{w['season']} WRAPPED", font=font(44, True), fill=GOLD)
    start = datetime.fromtimestamp(w["since"], tz).strftime("%b %Y")
    end = datetime.fromtimestamp(w["until"], tz).strftime("%b %Y")
    period = start if start == end else f"{start} – {end}"
    d.text((60, 108), f"{period} · so far", font=font(26), fill=MUTED)
    name, f = _fit(d, w["name"], CW - 120, 84)
    d.text((60, 160), name, font=f, fill=TEXT)
    clubs, f = _fit(d, " · ".join(w["clubs"]), CW - 120, 30, bold=False)
    d.text((60, 268), clubs, font=f, fill=MUTED)

    win_pct = round(100 * w["w"] / w["games"]) if w["games"] else 0
    tiles = [("GAMES", str(w["games"])), ("WIN RATE", f"{win_pct}%"),
             ("GOALS", str(w["goals"])), ("ASSISTS", str(w["assists"])),
             ("AVG RATING", f"{w['rating']:.1f}" if w["rating"] is not None else "—")]
    if w["defensive"] * 2 >= w["games"] and w["games"]:
        tiles.append(("CLEAN SHEETS", str(w["clean_sheets"])))
    else:
        tiles.append(("MAN OF THE MATCH", str(w["ea_motm"])))
    tw, th, gap, top = (CW - 120 - 2 * 24) / 3, 150, 24, 330
    for i, (label, value) in enumerate(tiles):
        x = 60 + (i % 3) * (tw + gap)
        y = top + (i // 3) * (th + gap)
        d.rounded_rectangle([x, y, x + tw, y + th], radius=20, fill=TILE)
        d.text((x + 24, y + 20), value, font=font(60, True), fill=TEXT)
        d.text((x + 24, y + 104), label, font=font(20, True), fill=GOLD)

    record = f"W{w['w']}  D{w['d']}  L{w['l']}"
    d.text((60, 680), record, font=font(30, True), fill=TEXT)

    y = 750
    lines = highlights(w)[:7]
    bottom = min(CH - 110, y + 62 * max(len(lines), 1) + 10)   # box fits however many highlights there are
    d.rounded_rectangle([60, y - 20, CW - 60, bottom], radius=24, fill=GOLD_DARK)
    d.rounded_rectangle([64, y - 16, CW - 64, bottom - 4], radius=22, fill=TILE)
    for line in lines:
        text, f = _fit(d, line, CW - 180, 28, bold=False)
        d.ellipse([92, y + 12, 104, y + 24], fill=GOLD)
        d.text((122, y), text, font=f, fill=TEXT)
        y += 62
    d.line([(60, CH - 80), (CW - 60, CH - 80)], fill=LINE, width=2)
    d.text((60, CH - 62), "From every game the bot saved this season · forfeits don't count", font=font(20), fill=MUTED)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
