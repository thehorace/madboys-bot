"""
Rotation tracker (single club: MADBOYS FC).

Commands:
  /rotation check    - Flag players stuck in the same role too long
  /rotation history  - Recent position history for a player
  /rotation stats    - Games per position for each player

History comes from every tracked match (cogs/matchday.py + positions.py):
  - EA only says defender / midfielder / forward / goalkeeper, so the exact
    spot (LB vs CB...) comes from the lineup a manager posted, or from the
    player tapping where they played after the game.
  - Games nobody confirmed are stored as DEF / MID / FWD and only compared
    at that broad level (see current_streak()).
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import CLUB_COLOUR, CLUB_NAME, ROTATION_THRESHOLD
from db import connect, now_iso
from utils import clip, resolve_name

log = logging.getLogger("madboys-bot.rotation")

_ROLE_OF = {
    "GK": "GK",
    "RB": "DEF", "CB": "DEF", "LB": "DEF", "RWB": "DEF", "LWB": "DEF", "DEF": "DEF",
    "CDM": "MID", "CM": "MID", "CAM": "MID", "RM": "MID", "LM": "MID", "RAM": "MID", "LAM": "MID", "MID": "MID",
    "RW": "FWD", "LW": "FWD", "ST": "FWD", "CF": "FWD", "FWD": "FWD",
}


def strip_number(pos: str) -> str:
    """CB1/CB2 -> CB, ST1/ST2 -> ST."""
    return pos.rstrip("0123456789")


def broad_role(pos: str) -> str:
    """Any slot or bucket -> GK / DEF / MID / FWD."""
    return _ROLE_OF.get(strip_number(pos.upper()), strip_number(pos.upper()))


# --------------------------------------------------------------------------- #
#  Writes
# --------------------------------------------------------------------------- #
BROAD = {"DEF", "MID", "FWD"}   # GK is both broad and exact


def is_exact(pos: str) -> bool:
    """'LB', 'CB2', 'GK' are exact; 'DEF' / 'MID' / 'FWD' (EA's buckets) are not."""
    return strip_number(pos.upper()) not in BROAD


def log_positions(guild_id: str, club: str, entries: list[tuple[str, str]], source: str = "manual",
                  logged_at: Optional[str] = None, match_id: Optional[str] = None):
    """
    Log (discord_id, position) pairs. `logged_at` should be the match's own
    time for auto-logged matches, so history is ordered by when games were
    actually played, not when the bot happened to notice them.
    """
    when = logged_at or now_iso()
    rows = [(guild_id, club, did, strip_number(pos), when, source, match_id) for did, pos in entries if did and pos]
    if not rows:
        return
    with connect() as conn:
        conn.executemany(
            "INSERT INTO rotation_log (guild_id, club, discord_id, position, logged_at, source, match_id) "
            "VALUES (?,?,?,?,?,?,?)", rows)


def set_match_position(guild_id: str, club: str, match_id: str, discord_id: str, position: str,
                       logged_at: Optional[str] = None) -> None:
    """A player said where they actually played in a match: replace (or add) that game's entry."""
    position = strip_number(position.upper())
    with connect() as conn:
        cur = conn.execute("UPDATE rotation_log SET position=?, source='player' "
                           "WHERE guild_id=? AND club=? AND match_id=? AND discord_id=?",
                           (position, guild_id, club, match_id, discord_id))
        if cur.rowcount == 0:
            conn.execute("INSERT INTO rotation_log (guild_id, club, discord_id, position, logged_at, source, match_id) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (guild_id, club, discord_id, position, logged_at or now_iso(), "player", match_id))


def current_streak(positions: list[str]) -> tuple[Optional[str], int]:
    """
    positions newest-first -> (position, games in a row). Compares exact positions
    (LB vs CB count as different) when the newest entry is exact, otherwise falls
    back to the broad role for games nobody confirmed.
    """
    if not positions:
        return None, 0
    first = strip_number(positions[0].upper())
    if is_exact(first):
        n = 0
        for p in positions:
            if strip_number(p.upper()) != first:
                break
            n += 1
        return first, n
    role = broad_role(first)
    n = 0
    for p in positions:
        if broad_role(p) != role:
            break
        n += 1
    return role, n


def is_match_processed(guild_id: str, club: str, match_id: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM processed_matches WHERE guild_id=? AND club=? AND match_id=?",
            (guild_id, club, match_id),
        ).fetchone() is not None


def mark_match_processed(guild_id: str, club: str, match_id: str):
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_matches (guild_id, club, match_id, processed_at) VALUES (?,?,?,?)",
            (guild_id, club, match_id, now_iso()),
        )


# --------------------------------------------------------------------------- #
#  Reads
# --------------------------------------------------------------------------- #
def get_recent_positions(guild_id: str, club: str, discord_id: str, limit: int = 10) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("""
            SELECT position, logged_at, source FROM rotation_log
            WHERE guild_id=? AND club=? AND discord_id=?
            ORDER BY logged_at DESC, id DESC LIMIT ?
        """, (guild_id, club, discord_id, limit)).fetchall()
    return [dict(r) for r in rows]


def get_all_recent(guild_id: str, club: str, limit_per_player: int = 5) -> dict[str, list[str]]:
    """
    {discord_id: [newest, 2nd newest, ...]} capped at limit_per_player each.
    The cap is applied inside SQL, so this stays fast however long history gets
    (it used to load every row ever logged and filter in Python).
    """
    with connect() as conn:
        rows = conn.execute("""
            SELECT discord_id, position FROM (
                SELECT discord_id, position,
                       ROW_NUMBER() OVER (PARTITION BY discord_id ORDER BY logged_at DESC, id DESC) AS rn
                FROM rotation_log WHERE guild_id=? AND club=?
            ) WHERE rn <= ? ORDER BY discord_id, rn
        """, (guild_id, club, limit_per_player)).fetchall()
    result: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        result[r["discord_id"]].append(r["position"])
    return dict(result)


def get_position_counts(guild_id: str, club: str) -> dict[str, dict[str, int]]:
    with connect() as conn:
        rows = conn.execute("""
            SELECT discord_id, position, COUNT(*) AS cnt FROM rotation_log
            WHERE guild_id=? AND club=? GROUP BY discord_id, position
        """, (guild_id, club)).fetchall()
    result: dict[str, dict[str, int]] = defaultdict(dict)
    for r in rows:
        result[r["discord_id"]][r["position"]] = r["cnt"]
    return dict(result)


class RotationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    rotation_group = app_commands.Group(name="rotation", description="Rotation tracking")

    @rotation_group.command(name="check", description="Flag players who've been stuck in the same role")
    async def rotation_check(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        history = get_all_recent(guild_id, CLUB_NAME, limit_per_player=10)
        if not history:
            await interaction.response.send_message(
                f"No rotation history for {CLUB_NAME} yet. It fills in automatically after matches "
                f"once players have used `/link me`.", ephemeral=True)
            return

        await interaction.response.defer()
        flagged, healthy = [], []
        for discord_id, positions in history.items():
            if len(positions) < ROTATION_THRESHOLD:
                continue
            name = await resolve_name(interaction.guild, discord_id)
            pos, run = current_streak(positions)
            if run >= ROTATION_THRESHOLD:
                flagged.append(f"⚠️ **{name}** — {pos} for last {run} games")
            else:
                healthy.append(f"✅ {name} — {' → '.join(strip_number(p) for p in positions[:3])}")

        embed = discord.Embed(title=f"🔄 {CLUB_NAME} — Rotation Check", colour=0xFF4444 if flagged else 0x2ECC71)
        if flagged:
            embed.add_field(name=f"Needs rotation ({len(flagged)})", value=clip("\n".join(flagged)), inline=False)
        else:
            embed.add_field(name="All good!", value="No one is stuck in the same role.", inline=False)
        if healthy:
            embed.add_field(name="Rotating well", value=clip("\n".join(healthy[:15])), inline=False)
        embed.set_footer(text=f"Flags {ROTATION_THRESHOLD}+ games in a row in the same position • "
                              f"/lineup suggest takes this into account")
        await interaction.followup.send(embed=embed)

    @rotation_group.command(name="history", description="Show recent position history for a player")
    @app_commands.describe(player="The player (leave blank for yourself)")
    async def rotation_history(self, interaction: discord.Interaction, player: Optional[discord.Member] = None):
        player = player or interaction.user
        history = get_recent_positions(str(interaction.guild_id), CLUB_NAME, str(player.id), limit=10)
        if not history:
            await interaction.response.send_message(
                f"No rotation history for **{player.display_name}** yet.", ephemeral=True)
            return
        lines = []
        for i, e in enumerate(history):
            try:
                dt = datetime.fromisoformat(e["logged_at"]).astimezone(timezone.utc)
                when = f"<t:{int(dt.timestamp())}:d>"
            except ValueError:
                when = e["logged_at"][:10]
            tag = "" if e.get("source") == "auto" else " *(lineup)*"
            lines.append(f"`{i + 1}.` **{e['position']}** — {when}{tag}")
        embed = discord.Embed(title=f"📋 {player.display_name} — Position History",
                              description="\n".join(lines), colour=CLUB_COLOUR)
        embed.set_footer(text="Most recent first")
        await interaction.response.send_message(embed=embed)

    @rotation_group.command(name="stats", description="Show how many games each player has played per role")
    async def rotation_stats(self, interaction: discord.Interaction):
        counts = get_position_counts(str(interaction.guild_id), CLUB_NAME)
        if not counts:
            await interaction.response.send_message(f"No rotation data for {CLUB_NAME} yet.", ephemeral=True)
            return
        await interaction.response.defer()
        lines = []
        for discord_id, pos_counts in counts.items():
            name = await resolve_name(interaction.guild, discord_id)
            merged: dict[str, int] = defaultdict(int)
            for p, c in pos_counts.items():
                base = strip_number(p)
                merged[base if is_exact(base) else f"{base}?"] += c   # "DEF?" = defender, exact spot not confirmed
            total = sum(merged.values())
            breakdown = ", ".join(f"{p} ×{c}" for p, c in sorted(merged.items(), key=lambda x: -x[1]))
            lines.append((name.lower(), f"**{name}** ({total}): {breakdown}"))
        embed = discord.Embed(title=f"📊 {CLUB_NAME} — Games per Position (All Time)",
                              description=clip("\n".join(l for _, l in sorted(lines)), 4096), colour=CLUB_COLOUR)
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(RotationCog(bot))
