"""
Rotation tracker cog.

Commands:
  /lineup confirm          - Logs the current lineup to rotation history
  /rotation check          - Flags players stuck in the same position too long
  /rotation history        - Shows recent position history for a player
  /rotation stats          - Shows how many games each player has played per position
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from utils import resolve_name

log = logging.getLogger("madboys-bot.rotation")

DB_PATH = os.getenv("DB_PATH", "madboys.db")

# How many consecutive games in the same position before we flag it
ROTATION_THRESHOLD = int(os.getenv("ROTATION_THRESHOLD", "3"))


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS rotation_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   TEXT NOT NULL,
                club       TEXT NOT NULL,
                discord_id TEXT NOT NULL,
                position   TEXT NOT NULL,
                logged_at  TEXT NOT NULL,
                source     TEXT NOT NULL DEFAULT 'manual'
            );

            CREATE INDEX IF NOT EXISTS idx_rotation_guild_club
                ON rotation_log (guild_id, club, discord_id, logged_at);

            CREATE TABLE IF NOT EXISTS processed_matches (
                guild_id   TEXT NOT NULL,
                club       TEXT NOT NULL,
                match_id   TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, club, match_id)
            );
        """)
        # Backfill 'source' column for DBs created before this change
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(rotation_log)").fetchall()]
        if "source" not in cols:
            conn.execute("ALTER TABLE rotation_log ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")


def log_lineup(guild_id: str, club: str, slots: dict[str, str | None], source: str = "manual"):
    """Write current confirmed lineup to rotation history."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (guild_id, club, discord_id, position, now, source)
        for position, discord_id in slots.items()
        if discord_id
    ]
    with get_db() as conn:
        conn.executemany(
            "INSERT INTO rotation_log (guild_id, club, discord_id, position, logged_at, source) VALUES (?,?,?,?,?,?)",
            rows,
        )


def is_match_processed(guild_id: str, club: str, match_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_matches WHERE guild_id=? AND club=? AND match_id=?",
            (guild_id, club, match_id),
        ).fetchone()
        return row is not None


def mark_match_processed(guild_id: str, club: str, match_id: str):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_matches (guild_id, club, match_id, processed_at) VALUES (?,?,?,?)",
            (guild_id, club, match_id, now),
        )


def get_recent_positions(guild_id: str, club: str, discord_id: str, limit: int = 10) -> list[dict]:
    """Last N logged positions for a player."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT position, logged_at FROM rotation_log
            WHERE guild_id=? AND club=? AND discord_id=?
            ORDER BY logged_at DESC
            LIMIT ?
        """, (guild_id, club, discord_id, limit)).fetchall()
        return [dict(r) for r in rows]


def get_all_recent(guild_id: str, club: str, limit_per_player: int = 5) -> dict[str, list[str]]:
    """
    Returns {discord_id: [pos_newest, pos_2nd, ...]} for all players,
    up to limit_per_player entries each.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT discord_id, position, logged_at,
                   ROW_NUMBER() OVER (PARTITION BY discord_id ORDER BY logged_at DESC) as rn
            FROM rotation_log
            WHERE guild_id=? AND club=?
        """, (guild_id, club)).fetchall()

    result: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if row["rn"] <= limit_per_player:
            result[row["discord_id"]].append(row["position"])
    return dict(result)


def get_position_counts(guild_id: str, club: str) -> dict[str, dict[str, int]]:
    """Returns {discord_id: {position: count}} across all logged games."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT discord_id, position, COUNT(*) as cnt
            FROM rotation_log
            WHERE guild_id=? AND club=?
            GROUP BY discord_id, position
        """, (guild_id, club)).fetchall()

    result: dict[str, dict[str, int]] = defaultdict(dict)
    for row in rows:
        result[row["discord_id"]][row["position"]] = row["cnt"]
    return dict(result)


def strip_number(pos: str) -> str:
    """CB1/CB2 -> CB, ST1/ST2 -> ST for grouping purposes."""
    return pos.rstrip("123")


class RotationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()

    rotation_group = app_commands.Group(name="rotation", description="Rotation tracking")

    # ------------------------------------------------------------------ #
    #  /rotation check
    # ------------------------------------------------------------------ #
    @rotation_group.command(name="check", description="Flag players who've been stuck in the same position")
    @app_commands.describe(club="Which club to check")
    @app_commands.choices(club=[
        app_commands.Choice(name="MADBOYS", value="MADBOYS"),
        app_commands.Choice(name="GRASBOYS", value="GRASBOYS"),
    ])
    async def rotation_check(self, interaction: discord.Interaction, club: app_commands.Choice[str]):
        guild_id = str(interaction.guild_id)
        history = get_all_recent(guild_id, club.value, limit_per_player=ROTATION_THRESHOLD + 2)

        if not history:
            await interaction.response.send_message(
                f"No rotation history for {club.value} yet. Confirm some lineups with `/lineup confirm` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        flagged = []
        healthy = []

        for discord_id, positions in history.items():
            name = await resolve_name(interaction.guild, discord_id)
            if len(positions) < ROTATION_THRESHOLD:
                continue
            last_n = [strip_number(p) for p in positions[:ROTATION_THRESHOLD]]
            if len(set(last_n)) == 1:
                flagged.append(f"⚠️ **{name}** — {last_n[0]} for last {ROTATION_THRESHOLD} games")
            else:
                healthy.append(f"✅ {name} — {' → '.join(strip_number(p) for p in positions[:3])}")

        embed = discord.Embed(
            title=f"🔄 {club.value} — Rotation Check",
            colour=0xFF4444 if flagged else 0x2ECC71,
        )

        if flagged:
            embed.add_field(
                name=f"Needs rotation ({len(flagged)} players)",
                value="\n".join(flagged),
                inline=False,
            )
        else:
            embed.add_field(name="All good!", value="No one is stuck in the same position.", inline=False)

        if healthy:
            embed.add_field(
                name="Rotating well",
                value="\n".join(healthy[:10]),  # cap display length
                inline=False,
            )

        embed.set_footer(text=f"Flagging players in same position for {ROTATION_THRESHOLD}+ consecutive games")
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #
    #  /rotation history
    # ------------------------------------------------------------------ #
    @rotation_group.command(name="history", description="Show recent position history for a player")
    @app_commands.describe(club="Which club", player="The player (mention them with @)")
    @app_commands.choices(club=[
        app_commands.Choice(name="MADBOYS", value="MADBOYS"),
        app_commands.Choice(name="GRASBOYS", value="GRASBOYS"),
    ])
    async def rotation_history(
        self,
        interaction: discord.Interaction,
        club: app_commands.Choice[str],
        player: discord.Member,
    ):
        guild_id = str(interaction.guild_id)
        history = get_recent_positions(guild_id, club.value, str(player.id), limit=10)

        if not history:
            await interaction.response.send_message(
                f"No rotation history found for **{player.display_name}** in {club.value}.",
                ephemeral=True,
            )
            return

        lines = []
        for i, entry in enumerate(history):
            dt = datetime.fromisoformat(entry["logged_at"]).strftime("%d %b")
            lines.append(f"`{i+1}.` **{entry['position']}** — {dt}")

        embed = discord.Embed(
            title=f"📋 {player.display_name} — Position History ({club.value})",
            description="\n".join(lines),
            colour=0x1E90FF if club.value == "MADBOYS" else 0x2ECC71,
        )
        embed.set_footer(text="Most recent first")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    #  /rotation stats
    # ------------------------------------------------------------------ #
    @rotation_group.command(name="stats", description="Show how many games each player has played per position")
    @app_commands.describe(club="Which club")
    @app_commands.choices(club=[
        app_commands.Choice(name="MADBOYS", value="MADBOYS"),
        app_commands.Choice(name="GRASBOYS", value="GRASBOYS"),
    ])
    async def rotation_stats(self, interaction: discord.Interaction, club: app_commands.Choice[str]):
        guild_id = str(interaction.guild_id)
        counts = get_position_counts(guild_id, club.value)

        if not counts:
            await interaction.response.send_message(
                f"No rotation data for {club.value} yet.", ephemeral=True
            )
            return

        await interaction.response.defer()

        lines = []
        for discord_id, pos_counts in sorted(counts.items()):
            name = await resolve_name(interaction.guild, discord_id)
            breakdown = ", ".join(
                f"{strip_number(p)} ×{c}"
                for p, c in sorted(pos_counts.items(), key=lambda x: -x[1])
            )
            lines.append(f"**{name}**: {breakdown}")

        embed = discord.Embed(
            title=f"📊 {club.value} — Position Stats (All Time)",
            description="\n".join(lines) if lines else "No data yet.",
            colour=0x1E90FF if club.value == "MADBOYS" else 0x2ECC71,
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(RotationCog(bot))
