"""
Matchday polling cog (single club: MADBOYS FC).

Commands:
  /matchday start   - Begin polling every 5 min for a new completed match,
                      posting results to the channel this was run in.
  /matchday stop    - Stop polling in this server.
  /matchday status  - Show whether polling is currently active in this server.

Design notes:
  - EA's Pro Clubs API only exposes *completed* matches (match-history based),
    so this can't show live goal-by-goal updates. Instead it polls for the
    newest completed match and posts it the first time it's seen.
  - "Newest match already posted" state is persisted in SQLite so a bot
    restart won't cause a re-post of an old result. The polling loop itself
    is in-memory only, so a restart does stop active polling — /matchday start
    needs to be run again after a redeploy/restart.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import CLUB_COLOUR, CLUB_ID, CLUB_NAME, PLATFORM, migrate_legacy_club
from ea_client import EAClient

log = logging.getLogger("madboys-bot.matchday")

DB_PATH = os.getenv("DB_PATH", "madboys.db")
POLL_MINUTES = int(os.getenv("MATCHDAY_POLL_MINUTES", "5"))


def format_result(club_score: int, opp_score: int) -> str:
    if club_score > opp_score:
        return "✅ WIN"
    elif club_score < opp_score:
        return "❌ LOSS"
    return "🟡 DRAW"


def match_identifier(match: dict) -> str:
    """Best-effort unique ID for a match, so we can tell 'new' from 'already posted'."""
    return str(match.get("matchId") or match.get("timestamp") or "")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS matchday_poll (
                guild_id      TEXT NOT NULL,
                club          TEXT NOT NULL,
                channel_id    TEXT NOT NULL,
                last_match_id TEXT,
                updated_at    TEXT NOT NULL,
                PRIMARY KEY (guild_id, club)
            );
        """)
        migrate_legacy_club(conn, "matchday_poll")


def start_poll_state(guild_id: str, club: str, channel_id: str):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO matchday_poll (guild_id, club, channel_id, last_match_id, updated_at)
            VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(guild_id, club) DO UPDATE SET channel_id=excluded.channel_id, updated_at=excluded.updated_at
        """, (guild_id, club, channel_id, now))


def get_last_match_id(guild_id: str, club: str) -> str | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT last_match_id FROM matchday_poll WHERE guild_id=? AND club=?",
            (guild_id, club),
        ).fetchone()
        return row["last_match_id"] if row else None


def set_last_match_id(guild_id: str, club: str, match_id: str):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            UPDATE matchday_poll SET last_match_id=?, updated_at=?
            WHERE guild_id=? AND club=?
        """, (match_id, now, guild_id, club))


class MatchdayCog(commands.Cog):
    def __init__(self, bot: commands.Bot, ea: EAClient):
        self.bot = bot
        self.ea = ea
        init_db()
        # key: guild_id -> tasks.Loop
        self.active_loops: dict[str, tasks.Loop] = {}

    def cog_unload(self):
        for loop in self.active_loops.values():
            loop.cancel()

    matchday_group = app_commands.Group(name="matchday", description="Poll for new match results")

    # ------------------------------------------------------------------ #
    #  Core poll logic
    # ------------------------------------------------------------------ #
    async def _check_and_post(self, guild_id: str, channel_id: str):
        matches = await self.ea.get_recent_matches(
            CLUB_ID, match_type="leagueMatch", count=1, bypass_cache=True
        )
        if not matches:
            return

        match = matches[0]
        match_id = match_identifier(match)
        if not match_id:
            return

        last_posted = get_last_match_id(guild_id, CLUB_NAME)
        if match_id == last_posted:
            return  # already posted this one

        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except discord.HTTPException:
                log.warning(f"Could not fetch channel {channel_id} for matchday post")
                return

        try:
            embed = self._build_embed(match)
        except Exception as e:
            log.error(f"Error building matchday embed: {e}", exc_info=True)
            return

        await channel.send(content="📡 New match result:", embed=embed)
        set_last_match_id(guild_id, CLUB_NAME, match_id)

    def _build_embed(self, match: dict) -> discord.Embed:
        club_id = CLUB_ID
        clubs_data = match.get("clubs", {})
        club_data = clubs_data.get(str(club_id), {})
        opp_id = next((k for k in clubs_data if k != str(club_id)), None)
        opp_data = clubs_data.get(opp_id, {}) if opp_id else {}

        our_score = int(club_data.get("goals", 0))
        opp_score = int(opp_data.get("goals", 0))
        opp_name = opp_data.get("details", {}).get("name", "Unknown")
        result = format_result(our_score, opp_score)

        # NOTE: EA keys this dict by numeric player ID, not name — the
        # actual display name lives in each player's "playername" field.
        players = match.get("players", {}).get(str(club_id), {})
        scorers = []
        for pid, pdata in players.items():
            pname = pdata.get("playername") or f"<{pid}>"
            goals = int(pdata.get("goals", 0))
            assists = int(pdata.get("assists", 0))
            if goals > 0 or assists > 0:
                scorers.append(f"{pname} — {goals}G {assists}A")

        embed = discord.Embed(
            title=f"{result}  {CLUB_NAME} {our_score}–{opp_score} {opp_name}",
            colour=CLUB_COLOUR,
        )
        embed.add_field(
            name="Goals & Assists",
            value="\n".join(scorers) if scorers else "No goal contributions recorded",
            inline=False,
        )

        man_ratings = []
        for pid, pdata in players.items():
            rating = pdata.get("rating")
            if rating:
                pname = pdata.get("playername") or f"<{pid}>"
                man_ratings.append((pname, float(rating)))
        if man_ratings:
            man_ratings.sort(key=lambda x: x[1], reverse=True)
            motm = man_ratings[0]
            embed.add_field(name="⭐ MOTM", value=f"{motm[0]} ({motm[1]:.1f})", inline=True)

        embed.set_footer(text=f"{CLUB_NAME} • EA FC Pro Clubs • auto-detected via /matchday")
        return embed

    def _make_loop(self, guild_id: str, channel_id: str) -> tasks.Loop:
        @tasks.loop(minutes=POLL_MINUTES)
        async def poll():
            await self._check_and_post(guild_id, channel_id)

        @poll.before_loop
        async def before():
            await self.bot.wait_until_ready()

        return poll

    # ------------------------------------------------------------------ #
    #  /matchday start
    # ------------------------------------------------------------------ #
    @matchday_group.command(name="start", description="Start polling for new match results in this channel")
    async def matchday_start(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)

        if guild_id in self.active_loops:
            await interaction.response.send_message(
                f"Already polling **{CLUB_NAME}** in this server — use `/matchday stop` first if you want to move it.",
                ephemeral=True,
            )
            return

        channel_id = str(interaction.channel_id)
        start_poll_state(guild_id, CLUB_NAME, channel_id)

        loop = self._make_loop(guild_id, channel_id)
        loop.start()
        self.active_loops[guild_id] = loop

        await interaction.response.send_message(
            f"📡 Polling **{CLUB_NAME}** every {POLL_MINUTES} min. New results will post here automatically. "
            f"Use `/matchday stop` to end."
        )

    # ------------------------------------------------------------------ #
    #  /matchday stop
    # ------------------------------------------------------------------ #
    @matchday_group.command(name="stop", description="Stop polling in this server")
    async def matchday_stop(self, interaction: discord.Interaction):
        loop = self.active_loops.pop(str(interaction.guild_id), None)

        if loop is None:
            await interaction.response.send_message(
                f"Not currently polling **{CLUB_NAME}** in this server.", ephemeral=True
            )
            return

        loop.cancel()
        await interaction.response.send_message(f"🛑 Stopped polling **{CLUB_NAME}**.")

    # ------------------------------------------------------------------ #
    #  /matchday status
    # ------------------------------------------------------------------ #
    @matchday_group.command(name="status", description="Show whether match polling is active")
    async def matchday_status(self, interaction: discord.Interaction):
        if str(interaction.guild_id) not in self.active_loops:
            await interaction.response.send_message("No active matchday polling in this server.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Currently polling: **{CLUB_NAME}** (every {POLL_MINUTES} min)", ephemeral=True
        )


async def setup(bot: commands.Bot):
    ea = EAClient(platform=PLATFORM)
    await bot.add_cog(MatchdayCog(bot, ea))
