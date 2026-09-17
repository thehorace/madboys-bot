"""
Background poller that checks for new league matches and auto-logs each
player's position to rotation history — no manual /lineup confirm needed.

Runs on a discord.ext.tasks loop (simpler than adding apscheduler as a
second event loop inside the same process; requirements.txt already has
apscheduler if you'd rather switch to it later, but it's not needed here).

Uses cogs.link to map an EA persona name -> discord_id, and cogs.rotation's
log_positions()/processed_matches table to avoid double-logging a match.

Match type note:
EA partitions match history by matchType, so this checks both
"leagueMatch" and "playoffMatch" — checking leagueMatch alone would
silently skip rotation logging for any playoff games played.

Position data note:
EA's /matches response only gives a broad bucket per player, in
players.<clubId>.<personaId>.pos — confirmed values are "goalkeeper",
"defender", "midfielder", "forward". There's no finer field (e.g. no
distinction between CB/RB/LB or CDM/CM/CAM) anywhere in the payload, so
extract_position() maps onto GK/DEF/MID/FWD rather than the specific
formation slot labels used in lineup.py. Good enough for "is this player
stuck playing the same broad role every game", not for exact slot rotation.
"""

import logging
import os

import discord
from discord.ext import commands, tasks

from cogs.rotation import log_positions, is_match_processed, mark_match_processed
from cogs.link import find_discord_id_by_ea_name

log = logging.getLogger("madboys-bot.rotation_poller")

GUILD_ID = os.getenv("GUILD_ID")  # single-server bot; matches log against this guild
POLL_MINUTES = int(os.getenv("ROTATION_POLL_MINUTES", "15"))

CLUBS = {
    "MADBOYS": int(os.getenv("MADBOYS_CLUB_ID", "85077")),
    "GRASBOYS": int(os.getenv("GRASBOYS_CLUB_ID", "4137103")),
}

# EA's raw "pos" string -> our broad rotation category
POSITION_MAP = {
    "goalkeeper": "GK",
    "defender": "DEF",
    "midfielder": "MID",
    "forward": "FWD",
}


def extract_position(player_data: dict) -> str | None:
    """Map EA's raw per-player 'pos' field to a broad rotation category."""
    raw = (player_data.get("pos") or "").strip().lower()
    return POSITION_MAP.get(raw)


class RotationPollerCog(commands.Cog):
    def __init__(self, bot: commands.Bot, ea):
        self.bot = bot
        self.ea = ea
        self.poll_matches.change_interval(minutes=POLL_MINUTES)
        self.poll_matches.start()

    def cog_unload(self):
        self.poll_matches.cancel()

    @tasks.loop(minutes=15)
    async def poll_matches(self):
        if not GUILD_ID:
            log.warning("GUILD_ID not set — rotation poller has no guild to log against, skipping.")
            return

        guild = self.bot.get_guild(int(GUILD_ID))
        if guild is None:
            log.warning(f"Bot isn't in guild {GUILD_ID} yet — skipping this poll.")
            return

        for club_name, club_id in CLUBS.items():
            try:
                await self._poll_club(guild, club_name, club_id)
            except Exception:
                log.exception(f"Error polling {club_name} for new matches")

    async def _poll_club(self, guild: discord.Guild, club_name: str, club_id: int):
        # Checks league + playoff matches — see module docstring.
        matches = await self.ea.get_recent_matches_multi(club_id, count=5)
        if not matches:
            return

        guild_id = str(guild.id)

        # Oldest first, so rotation history stays in chronological order
        for match in reversed(matches):
            match_id = str(match.get("matchId") or match.get("timestamp") or "")
            if not match_id:
                continue
            if is_match_processed(guild_id, club_name, match_id):
                continue

            players = match.get("players", {}).get(str(club_id), {})
            entries: list[tuple[str, str]] = []
            unmatched = []

            for _persona_id, pdata in players.items():
                ea_name = pdata.get("playername")
                if not ea_name:
                    continue

                discord_id = find_discord_id_by_ea_name(guild_id, ea_name)
                if not discord_id:
                    unmatched.append(ea_name)
                    continue

                position = extract_position(pdata)
                if position:
                    entries.append((discord_id, position))

            if entries:
                log_positions(guild_id, club_name, entries, source="auto")
                log.info(f"[{club_name}] Auto-logged {len(entries)} positions for match {match_id}")

            if unmatched:
                log.info(
                    f"[{club_name}] Match {match_id}: {len(unmatched)} players with no /link — "
                    f"{', '.join(unmatched)}"
                )

            mark_match_processed(guild_id, club_name, match_id)

    @poll_matches.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    # Reuse the same EAClient the stats cog uses, so caching/config stay consistent
    stats_cog = bot.get_cog("StatsCog")
    if stats_cog is None:
        log.warning("StatsCog not loaded yet — rotation poller needs it for the EA client. "
                    "Make sure cogs.stats loads before cogs.rotation_poller in bot.py.")
        return
    await bot.add_cog(RotationPollerCog(bot, stats_cog.ea))
