"""
Squad insights built from the games the tracker has saved (no EA calls, so they're instant):

  Best partnerships  - which pairs of players win most when they're on the pitch together
  Who shows up       - sign-ups vs actually playing, from finished sessions

Partnerships are per club (the current club scope). Attendance covers every club,
because a session is shared by everyone in the server.
"""

import json
import time
from typing import Optional

import discord

from clubs import club_id, club_name
from config import CLUB_COLOUR
from db import connect
import match_data as md

PAIR_MIN_GAMES = 5          # pairs need this many games together to be ranked
PAIR_MIN_GAMES_SMALL = 3    # ...or this many, when the club has few games saved
ATTENDANCE_DAYS = {"30": ("Last 30 days", 30 * 86400), "90": ("Last 90 days", 90 * 86400),
                   "all": ("All time", None)}


# --------------------------------------------------------------------------- #
#  Best partnerships
# --------------------------------------------------------------------------- #
def _pair_rows(cid: int, name: Optional[str] = None) -> list[dict]:
    """One row per pair of players who played the same game (same club), with totals."""
    where, args = "", [cid]
    if name:
        where = "AND (a.name = ? COLLATE NOCASE OR b.name = ? COLLATE NOCASE)"
        args += [name, name]
    with connect() as conn:
        rows = conn.execute(f"""
            SELECT MIN(a.name) AS a, MIN(b.name) AS b, COUNT(*) AS games,
                   SUM(m.result='W') AS w, SUM(m.result='D') AS d, SUM(m.result='L') AS l,
                   SUM(m.our_goals) AS gf, SUM(m.opp_goals) AS ga,
                   GROUP_CONCAT(LOWER(COALESCE(a.pos, ''))) AS a_pos, GROUP_CONCAT(LOWER(COALESCE(b.pos, ''))) AS b_pos
            FROM match_players a
            JOIN match_players b ON b.club_id = a.club_id AND b.match_id = a.match_id
                                 AND LOWER(a.name) < LOWER(b.name)
            JOIN matches m ON m.club_id = a.club_id AND m.match_id = a.match_id
            WHERE a.club_id = ? {where}
            GROUP BY LOWER(a.name), LOWER(b.name)""", args).fetchall()
    return [dict(r) for r in rows]


def _usual_pos(csv: str) -> str:
    """Most common EA role in those games, as DEF / MID / FWD / GK."""
    from collections import Counter
    roles = [p for p in (csv or "").split(",") if p]
    if not roles:
        return ""
    top = Counter(roles).most_common(1)[0][0]
    return md.POS_SHORT.get(top, top[:3].upper())


def _min_games(cid: int) -> int:
    with connect() as conn:
        games = conn.execute("SELECT COUNT(*) FROM matches WHERE club_id=?", (cid,)).fetchone()[0]
    return PAIR_MIN_GAMES if games >= 40 else PAIR_MIN_GAMES_SMALL


def _rank_key(p: dict):
    # win % first, then points per game (draws count), then more games together
    return (-p["w"] / p["games"], -(3 * p["w"] + p["d"]) / p["games"], -p["games"])


def best_partnerships(cid: Optional[int] = None, limit: int = 10) -> list[dict]:
    cid = cid or club_id()
    need = _min_games(cid)
    pairs = [p for p in _pair_rows(cid) if p["games"] >= need]
    return sorted(pairs, key=_rank_key)[:limit]


def partners_of(name: str, cid: Optional[int] = None, limit: int = 5) -> list[dict]:
    """A player's best partners, as rows with 'partner' set to the other name."""
    cid = cid or club_id()
    need = _min_games(cid)
    out = []
    for p in _pair_rows(cid, name):
        if p["games"] < need:
            continue
        mine_is_a = p["a"].casefold() == name.casefold()
        out.append({**p, "partner": p["b"] if mine_is_a else p["a"],
                    "partner_pos": _usual_pos(p["b_pos"] if mine_is_a else p["a_pos"])})
    return sorted(out, key=_rank_key)[:limit]


def _pair_line(i: int, p: dict) -> str:
    pct = round(100 * p["w"] / p["games"])
    pa, pb = _usual_pos(p["a_pos"]), _usual_pos(p["b_pos"])
    who = f"**{p['a']}**{f' ({pa})' if pa else ''} + **{p['b']}**{f' ({pb})' if pb else ''}"
    return (f"`{i:>2}.` {who}\n"
            f"      {pct}% wins · W{p['w']} D{p['d']} L{p['l']} in {p['games']} games · "
            f"{p['gf'] / p['games']:.1f}–{p['ga'] / p['games']:.1f} avg score")


def build_partnerships(name: Optional[str] = None) -> discord.Embed | str:
    cid = club_id()
    need = _min_games(cid)
    if name:
        rows = partners_of(name)
        if not rows:
            return (f"Not enough games yet to rank **{name}**'s partnerships in {club_name()} "
                    f"(pairs need {need}+ games together).")
        lines = []
        for i, p in enumerate(rows, 1):
            pos = f" ({p['partner_pos']})" if p["partner_pos"] else ""
            lines.append(f"`{i:>2}.` **{p['partner']}**{pos} — {round(100 * p['w'] / p['games'])}% wins · "
                         f"W{p['w']} D{p['d']} L{p['l']} in {p['games']} games")
        embed = discord.Embed(title=f"🤝 {name}'s best partners — {club_name()}", description="\n".join(lines),
                              colour=CLUB_COLOUR)
    else:
        rows = best_partnerships()
        if not rows:
            return (f"Not enough saved games yet for partnerships in {club_name()} "
                    f"(pairs need {need}+ games together).")
        embed = discord.Embed(title=f"🤝 Best partnerships — {club_name()}",
                              description="\n".join(_pair_line(i, p) for i, p in enumerate(rows, 1))[:4000],
                              colour=CLUB_COLOUR)
    embed.set_footer(text=f"Pairs who played {need}+ games together • from every game the bot has saved")
    return embed


# --------------------------------------------------------------------------- #
#  Who shows up
# --------------------------------------------------------------------------- #
def attendance(guild_id: str, period: str = "30") -> dict[str, dict]:
    """
    {discord_id: {said_in, played, showed, no_show, surprise, sessions}} over finished sessions
    that had games. "Said in" is a ✅ click (auto sign-ups from joining voice don't count as a
    promise); "played" means EA recorded them in that session's games.
    """
    secs = ATTENDANCE_DAYS.get(period, ATTENDANCE_DAYS["30"])[1]
    since = int(time.time()) - secs if secs else 0
    with connect() as conn:
        rows = conn.execute("""SELECT h.rsvps, h.players FROM session_history h JOIN sessions s ON s.id = h.session_id
                               WHERE h.guild_id=? AND h.outcome='completed' AND s.starts_at>=?""",
                            (guild_id, since)).fetchall()
    stats: dict[str, dict] = {}

    def row(did):
        return stats.setdefault(did, {"said_in": 0, "played": 0, "showed": 0, "no_show": 0, "surprise": 0})

    for r in rows:
        said = {x["discord_id"] for x in json.loads(r["rsvps"])
                if x.get("status") == "yes" and x.get("source") != "voice"}
        played = {did for p in json.loads(r["players"]) for did in p.get("discord_ids", [])}
        for did in said | played:
            s = row(did)
            s["said_in"] += did in said
            s["played"] += did in played
            s["showed"] += did in said and did in played
            s["no_show"] += did in said and did not in played
            s["surprise"] += did in played and did not in said
    return stats


def show_rate(s: dict) -> Optional[float]:
    return 100 * s["showed"] / s["said_in"] if s["said_in"] else None


async def build_attendance(guild: discord.Guild, period: str = "30", me: Optional[str] = None) -> discord.Embed | str:
    from utils import resolve_name
    label = ATTENDANCE_DAYS.get(period, ATTENDANCE_DAYS["30"])[0]
    import asyncio
    stats = await asyncio.to_thread(attendance, str(guild.id), period)
    if not stats:
        return f"No finished sessions with games in the {label.lower()} yet."
    order = sorted(stats.items(), key=lambda kv: (-(kv[1]["played"]), -(show_rate(kv[1]) or 0)))
    lines = []
    for did, s in order[:25]:
        rate = show_rate(s)
        name = await resolve_name(guild, did)
        mark = "▶️ " if did == me else ""
        extra = f" · {s['surprise']} surprise" if s["surprise"] else ""
        lines.append(f"{mark}**{name}** — 🎮 {s['played']} played · ✅ {s['said_in']} said in · "
                     f"{f'{rate:.0f}% showed' if rate is not None else 'never signed up'}"
                     f" · ❌ {s['no_show']} no-show{'s' if s['no_show'] != 1 else ''}{extra}")
    embed = discord.Embed(title=f"📋 Who shows up — {label}", description="\n".join(lines)[:4000],
                          colour=CLUB_COLOUR)
    embed.set_footer(text="Finished sessions with games • said in = clicked ✅ In • surprise = played without "
                          "signing up • joining voice doesn't count as a sign-up")
    return embed
