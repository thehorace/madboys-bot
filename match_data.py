"""
Turning EA match payloads into rows we keep forever, plus the queries that
power /form, /h2h, /lastgame, the weekly recap and the match embeds.

Why store matches at all: EA's API only returns a club's last handful of
games. Anything longer-term (form over 10 games, record vs a specific club,
a weekly recap) needs our own history.

EA match payload shape (the parts we use):
  match["matchId"], match["timestamp"]   (unix seconds)
  match["clubs"][<clubId>]["goals"], ["details"]["name"]
  match["players"][<clubId>][<personaId>] = {
      "playername", "pos", "goals", "assists", "rating", "shots",
      "passesmade", "passattempts", "tacklesmade", "tackleattempts",
      "saves", "redcards", "mom", ...
  }
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import discord

from config import CLUB_COLOUR, CLUB_NAME
from db import connect, now_iso
from utils import RESULT_COLOUR, RESULT_EMOJI, RESULT_LABEL, clip, pct, result_letter, to_float, to_int

log = logging.getLogger("madboys-bot.matches")

POS_SHORT = {"goalkeeper": "GK", "defender": "DEF", "midfielder": "MID", "forward": "FWD"}
MATCH_TYPE_LABEL = {"leagueMatch": "League", "playoffMatch": "Playoffs", "friendlyMatch": "Friendly"}


@dataclass
class PlayerLine:
    persona_id: str
    name: str
    pos: Optional[str]
    goals: int = 0
    assists: int = 0
    rating: Optional[float] = None
    shots: int = 0
    passes_made: int = 0
    pass_attempts: int = 0
    tackles_made: int = 0
    tackle_attempts: int = 0
    saves: int = 0
    red_cards: int = 0
    motm: int = 0


@dataclass
class ParsedMatch:
    match_id: str
    match_type: str
    ts: int
    our_goals: int
    opp_goals: int
    opp_id: Optional[str]
    opp_name: str
    players: list[PlayerLine] = field(default_factory=list)

    @property
    def result(self) -> str:
        return result_letter(self.our_goals, self.opp_goals)


def parse_match(match: dict, club_id: int) -> Optional[ParsedMatch]:
    match_id = str(match.get("matchId") or match.get("timestamp") or "")
    if not match_id:
        return None
    clubs = match.get("clubs", {}) or {}
    ours = clubs.get(str(club_id), {}) or {}
    opp_id = next((k for k in clubs if k != str(club_id)), None)
    opp = clubs.get(opp_id, {}) if opp_id else {}

    pm = ParsedMatch(
        match_id=match_id,
        match_type=match.get("_matchType") or match.get("matchType") or "leagueMatch",
        ts=to_int(match.get("timestamp")),
        our_goals=to_int(ours.get("goals")),
        opp_goals=to_int(opp.get("goals")),
        opp_id=opp_id,
        opp_name=((opp.get("details") or {}).get("name")) or "Unknown",
    )
    for pid, p in ((match.get("players") or {}).get(str(club_id)) or {}).items():
        pm.players.append(PlayerLine(
            persona_id=str(pid),
            name=p.get("playername") or f"<{pid}>",
            pos=(p.get("pos") or None),
            goals=to_int(p.get("goals")),
            assists=to_int(p.get("assists")),
            rating=to_float(p.get("rating")),
            shots=to_int(p.get("shots")),
            passes_made=to_int(p.get("passesmade")),
            pass_attempts=to_int(p.get("passattempts")),
            tackles_made=to_int(p.get("tacklesmade")),
            tackle_attempts=to_int(p.get("tackleattempts")),
            saves=to_int(p.get("saves")),
            red_cards=to_int(p.get("redcards")),
            motm=to_int(p.get("mom")),
        ))
    return pm


def motm_of(pm: ParsedMatch) -> Optional[PlayerLine]:
    flagged = [p for p in pm.players if p.motm]
    if flagged:
        return flagged[0]
    rated = [p for p in pm.players if p.rating is not None]
    return max(rated, key=lambda p: p.rating) if rated else None


# --------------------------------------------------------------------------- #
#  Storage
# --------------------------------------------------------------------------- #
def is_stored(club_id: int, match_id: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM matches WHERE club_id=? AND match_id=?", (club_id, match_id)
        ).fetchone() is not None


def store_match(club_id: int, pm: ParsedMatch, raw: dict) -> bool:
    """Insert a match + its player lines. Returns False if it was already stored."""
    with connect() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO matches
               (club_id, match_id, match_type, ts, our_goals, opp_goals, opp_id, opp_name, result, raw_json, stored_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (club_id, pm.match_id, pm.match_type, pm.ts, pm.our_goals, pm.opp_goals, pm.opp_id,
             pm.opp_name, pm.result, json.dumps(raw), now_iso()),
        )
        if cur.rowcount == 0:
            return False
        conn.executemany(
            """INSERT OR IGNORE INTO match_players
               (club_id, match_id, persona_id, name, pos, goals, assists, rating, shots, passes_made,
                pass_attempts, tackles_made, tackle_attempts, saves, red_cards, motm)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(club_id, pm.match_id, p.persona_id, p.name, p.pos, p.goals, p.assists, p.rating, p.shots,
              p.passes_made, p.pass_attempts, p.tackles_made, p.tackle_attempts, p.saves, p.red_cards, p.motm)
             for p in pm.players],
        )
        return True


def match_count(club_id: int) -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM matches WHERE club_id=?", (club_id,)).fetchone()[0]


def latest_match_ts(club_id: int) -> Optional[int]:
    with connect() as conn:
        row = conn.execute("SELECT MAX(ts) FROM matches WHERE club_id=?", (club_id,)).fetchone()
        return row[0] if row and row[0] else None


def get_raw_match(club_id: int, match_id: Optional[str] = None) -> Optional[dict]:
    with connect() as conn:
        if match_id:
            row = conn.execute("SELECT raw_json FROM matches WHERE club_id=? AND match_id=?", (club_id, match_id)).fetchone()
        else:
            row = conn.execute("SELECT raw_json FROM matches WHERE club_id=? ORDER BY ts DESC LIMIT 1", (club_id,)).fetchone()
    return json.loads(row["raw_json"]) if row and row["raw_json"] else None


def recent_results(club_id: int, limit: int = 10) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM matches WHERE club_id=? ORDER BY ts DESC LIMIT ?", (club_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def matches_between(club_id: int, start_ts: int, end_ts: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM matches WHERE club_id=? AND ts>=? AND ts<? ORDER BY ts", (club_id, start_ts, end_ts)
        ).fetchall()
    return [dict(r) for r in rows]


def player_totals(club_id: int, start_ts: int = 0, end_ts: int = 2**62) -> list[dict]:
    """Per-player totals over stored matches in a time window."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT mp.name AS name, COUNT(*) AS games, SUM(mp.goals) AS goals, SUM(mp.assists) AS assists,
                      AVG(mp.rating) AS rating, SUM(mp.motm) AS motm,
                      SUM(mp.passes_made) AS passes_made, SUM(mp.pass_attempts) AS pass_attempts,
                      SUM(mp.tackles_made) AS tackles_made, SUM(mp.shots) AS shots
               FROM match_players mp JOIN matches m ON m.club_id=mp.club_id AND m.match_id=mp.match_id
               WHERE mp.club_id=? AND m.ts>=? AND m.ts<?
               GROUP BY mp.name COLLATE NOCASE""",
            (club_id, start_ts, end_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def opponents(club_id: int, search: str = "", limit: int = 25) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT opp_name, MAX(ts) AS last FROM matches
               WHERE club_id=? AND opp_name LIKE ? GROUP BY opp_name ORDER BY last DESC LIMIT ?""",
            (club_id, f"%{search}%", limit),
        ).fetchall()
    return [r["opp_name"] for r in rows]


def head_to_head(club_id: int, opp_name: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM matches WHERE club_id=? AND opp_name = ? COLLATE NOCASE ORDER BY ts DESC",
            (club_id, opp_name),
        ).fetchall()
    return [dict(r) for r in rows]


def streak(results: list[str]) -> str:
    """results newest-first, e.g. ['W','W','L'] -> 'W2'."""
    if not results:
        return "—"
    first, n = results[0], 0
    for r in results:
        if r != first:
            break
        n += 1
    return f"{first}{n}"


# --------------------------------------------------------------------------- #
#  Embeds
# --------------------------------------------------------------------------- #
def match_embed(pm: ParsedMatch, footer_extra: str = "", with_table: bool = True) -> discord.Embed:
    r = pm.result
    type_label = MATCH_TYPE_LABEL.get(pm.match_type, pm.match_type)
    embed = discord.Embed(
        title=f"{RESULT_LABEL[r]}  {CLUB_NAME} {pm.our_goals}–{pm.opp_goals} {pm.opp_name}",
        colour=RESULT_COLOUR.get(r, CLUB_COLOUR),
        description=f"{type_label}" + (f" • <t:{pm.ts}:R>" if pm.ts else ""),
    )

    scorers = [f"{p.name} — {p.goals}G {p.assists}A" for p in
               sorted(pm.players, key=lambda p: (-p.goals, -p.assists)) if p.goals or p.assists]
    embed.add_field(name="Goals & Assists", value=clip("\n".join(scorers)) if scorers else "No goal contributions recorded",
                    inline=False)

    motm = motm_of(pm)
    if motm:
        embed.add_field(name="⭐ MOTM", value=f"{motm.name}" + (f" ({motm.rating:.1f})" if motm.rating else ""), inline=True)

    # Full ratings table, as a code block so columns line up (skipped when the card image shows it).
    if pm.players and with_table:
        rows = []
        for p in sorted(pm.players, key=lambda p: -(p.rating or 0)):
            pa = pct(p.passes_made, p.pass_attempts)
            pos = POS_SHORT.get((p.pos or "").lower(), (p.pos or "")[:3].upper())
            rating = f"{p.rating:.1f}" if p.rating is not None else " - "
            rows.append(f"{p.name[:12]:<12} {pos:<3} {rating:>4} {p.shots:>2}sh "
                        f"{(f'{pa:.0f}%' if pa is not None else '-'):>4} {p.tackles_made:>2}tk")
        table = "```\n" + "\n".join(rows) + "\n```"
        embed.add_field(name="Ratings  (rating • shots • pass% • tackles)", value=clip(table), inline=False)

    embed.set_footer(text=f"{CLUB_NAME} • EA FC Pro Clubs" + (f" • {footer_extra}" if footer_extra else ""))
    return embed


async def match_post(pm: ParsedMatch, footer_extra: str = "") -> tuple[discord.Embed, Optional[discord.File]]:
    """Embed + result card image. Falls back to the text table if the image fails for any reason."""
    import asyncio
    try:
        from match_card import render_match_card
        png = await asyncio.to_thread(render_match_card, pm)
    except Exception:
        log.exception("Result card render failed; posting text only")
        return match_embed(pm, footer_extra), None
    embed = match_embed(pm, footer_extra, with_table=False)
    embed.set_image(url="attachment://result.png")
    return embed, discord.File(png, "result.png")


def form_string(rows: list[dict]) -> str:
    """rows newest-first -> emoji string oldest→newest reads left to right."""
    return "".join(RESULT_EMOJI.get(r["result"], "▫️") for r in reversed(rows))
