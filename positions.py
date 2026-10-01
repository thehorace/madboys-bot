"""
Exact positions (LB vs CB vs RB...), which EA's data doesn't have.

EA's match data only says goalkeeper / defender / midfielder / forward. To
know who played LB and who played CB, the bot combines:

  1. The lineup a manager posted before the session (lineup_plans). If a player
     is in it and EA's bucket agrees with their slot (lineup says LB, EA says
     "defender"), that slot is logged automatically.
  2. Otherwise, the player is asked after the match with a one-tap picker
     (cogs/positions.py). Until they answer, the game is stored as DEF/MID/FWD.

This file holds the logic; cogs/positions.py holds the Discord parts.
"""

import json
import time
from typing import Optional

from cogs.rotation import broad_role, current_streak, get_recent_positions, is_exact, strip_number
from config import CLUB_NAME, ROTATION_THRESHOLD
from db import connect

# EA bucket -> broad role
EA_BUCKET = {"goalkeeper": "GK", "defender": "DEF", "midfielder": "MID", "forward": "FWD"}

# What players can pick, grouped by EA bucket (so the picker only offers sensible options first)
POSITIONS_BY_ROLE = {
    "GK": ["GK"],
    "DEF": ["RB", "CB", "LB", "RWB", "LWB"],
    "MID": ["CDM", "CM", "CAM", "RM", "LM"],
    "FWD": ["RW", "LW", "ST"],
}
ALL_POSITIONS = [p for ps in POSITIONS_BY_ROLE.values() for p in ps]

# A posted lineup counts for matches played up to this long after it was posted
PLAN_VALID_HOURS = 5


# --------------------------------------------------------------------------- #
#  Posted lineups
# --------------------------------------------------------------------------- #
def save_plan(guild_id: str, formation: str, slots: dict[str, Optional[str]], posted_by: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO lineup_plans (guild_id, club, formation, slots, posted_at, posted_by) VALUES (?,?,?,?,?,?)",
            (guild_id, CLUB_NAME, formation, json.dumps({k: v for k, v in slots.items() if v}),
             int(time.time()), posted_by))
        return cur.lastrowid


def plan_for_match(guild_id: str, match_ts: int) -> Optional[dict]:
    """The most recent lineup posted before this match (within PLAN_VALID_HOURS)."""
    when = match_ts or int(time.time())
    with connect() as conn:
        row = conn.execute("""
            SELECT * FROM lineup_plans WHERE guild_id=? AND club=? AND posted_at<=? AND posted_at>=?
            ORDER BY posted_at DESC LIMIT 1""",
            (guild_id, CLUB_NAME, when + 60, when - PLAN_VALID_HOURS * 3600)).fetchone()
    if not row:
        return None
    plan = dict(row)
    plan["slots"] = json.loads(plan["slots"])
    return plan


def slot_of(plan: Optional[dict], discord_id: str) -> Optional[str]:
    if not plan:
        return None
    for slot, did in plan["slots"].items():
        if did == discord_id:
            return slot
    return None


# Without a lineup, a player is assumed to have stayed in the exact spot they played in the
# previous game if it was this recent and EA still puts them in the same role.
CARRY_OVER_HOURS = 2


def previous_game_position(guild_id: str, discord_id: str, before_ts: int) -> Optional[str]:
    """Exact position from this player's previous game, if it was within CARRY_OVER_HOURS."""
    with connect() as conn:
        row = conn.execute("""
            SELECT r.position FROM rotation_log r JOIN matches m ON m.match_id = r.match_id
            WHERE r.guild_id=? AND r.club=? AND r.discord_id=? AND m.ts < ? AND m.ts >= ?
            ORDER BY m.ts DESC LIMIT 1""",
            (guild_id, CLUB_NAME, discord_id, before_ts, before_ts - CARRY_OVER_HOURS * 3600)).fetchone()
    return row["position"] if row and is_exact(row["position"]) else None


def resolve_position(plan: Optional[dict], discord_id: str, ea_pos: Optional[str],
                     guild_id: Optional[str] = None, match_ts: Optional[int] = None) -> tuple[Optional[str], bool]:
    """
    -> (position to log, confirmed?)
    confirmed=True: exact spot — from the posted lineup, or the same spot as their previous
                    game this session — and EA agrees on the role.
    confirmed=False: we only know EA's bucket (DEF/MID/FWD); ask the player.
    GK is always exact (there's only one keeper slot).
    """
    role = EA_BUCKET.get((ea_pos or "").strip().lower())
    if role is None:
        return None, True  # EA gave nothing usable; nothing to log or ask
    if role == "GK":
        return "GK", True
    slot = slot_of(plan, discord_id)
    if slot and broad_role(slot) == role:
        return strip_number(slot), True
    if not slot and guild_id and match_ts:
        prev = previous_game_position(guild_id, discord_id, match_ts)
        if prev and broad_role(prev) == role:
            return prev, True
    return role, False


def last_exact_position(guild_id: str, discord_id: str, role: Optional[str] = None) -> Optional[str]:
    """The player's most recent confirmed exact position (optionally within a role), for 'same as last time'."""
    for e in get_recent_positions(guild_id, CLUB_NAME, discord_id, limit=20):
        p = strip_number(e["position"])
        if is_exact(p) and (role is None or broad_role(p) == role):
            return p
    return None


# --------------------------------------------------------------------------- #
#  Rotation notes for managers
# --------------------------------------------------------------------------- #
def rotation_note(guild_id: str, discord_id: str, builds: list[str]) -> Optional[tuple[str, str]]:
    """
    If this player has just been in the same exact position ROTATION_THRESHOLD+
    games in a row, -> (dedupe key, suggestion). The key changes only when a
    new streak starts, so each streak is mentioned once, not after every game.
    """
    entries = get_recent_positions(guild_id, CLUB_NAME, discord_id, limit=15)
    positions = [e["position"] for e in entries]
    pos, run = current_streak(positions)
    if not pos or run < ROTATION_THRESHOLD or not is_exact(pos) or pos == "GK":
        return None   # keepers staying in goal is the plan, not a rotation problem
    # oldest game in the streak identifies it
    key = f"{pos}:{entries[run - 1]['logged_at']}"
    # suggest another position they have a build for, preferring ones they've played least lately
    recent = [strip_number(p) for p in positions]
    others = [b for b in builds if b != pos and b != "GK"]
    others.sort(key=lambda b: (recent.count(b), ALL_POSITIONS.index(b) if b in ALL_POSITIONS else 99))
    if others:
        tip = f"has builds for {', '.join(others[:3])} → try **{others[0]}** next"
    else:
        tip = "no other builds set yet (they can add some under 🛠️ My builds)"
    return key, f"**{pos}** {run} games in a row — {tip}"
