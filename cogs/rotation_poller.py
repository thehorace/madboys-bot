"""
Background poller that checks for new league matches and auto-logs each
player's position to rotation history — no manual /lineup confirm needed.

Runs on a discord.ext.tasks loop (simpler than adding apscheduler as a
second event loop inside the same process; requirements.txt already has
apscheduler if you'd rather switch to it later, but it's not needed here).

Uses cogs.link to map an EA persona name -> discord_id, and cogs.rotation's
log_lineup()/processed_matches table to avoid double-logging a match.

*** STUB WARNING ***
extract_position() below is NOT finished. I don't have a confirmed sample
of what EA's /matches response puts in each player's position field (it
may be a role code like "0"-"10", a string like "midfielder", or something
else). Paste a sample player block from a real /lastgame or middleware
response and I'll fill this in to map onto your GK/CB/RB/... slot labels
correctly. Until then this poller will log matches as "processed" but
SKIP writing any positions, so it's safe to enable — it just won't do
anything useful yet.
"""

import logging
import os

import discord
from discord.ext import commands, tasks

from cogs.rotation import log_lineup, is_match_processed, mark_match_processed
from cogs.link import find_discord_id_by_ea_name

log = logging.getLogger("madboys-bot.rotation_poller")

GUILD_ID = os.getenv("GUILD_ID")  # single-server bot; matches log against this guild
POLL_MINUTES = int(os.getenv("ROTATION_POLL_MINUTES", "15"))

CLUBS = {
    "MADBOYS": int(os.getenv("MADBOYS_CLUB_ID", "85077")),
    "GRASBOYS": int(os.getenv("GRASBOYS_CLUB_ID", "4137103")),
}


def extract_position(player_data: dict) -> str | None:
    """
    TODO: map EA's raw per-player position field to one of the formation
    slot labels used elsewhere in the bot (GK, CB, RB, LB, CDM, CM, CAM,
    RM, LM, RW, LW, ST — see lineup.py's POSITION_GROUPS).

    Once we have a sample match JSON this will probably look like:

        code = player_data.get("position")
        return POSITION_CODE_MAP.get(code)

    Returning None means "unknown / skip this player" — log_lineup()
    already ignores falsy values, so it's safe to leave as-is for now.
    """
    return None


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
        matches = await self.ea.get_recent_matches(club_id, match_type="leagueMatch", count=5)
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
            slots: dict[str, str | None] = {}
            unmatched = []

            for ea_name, pdata in players.items():
                discord_id = find_discord_id_by_ea_name(guild_id, ea_name)
                if not discord_id:
                    unmatched.append(ea_name)
                    continue

                position = extract_position(pdata)
                if position:
                    slots[position] = discord_id

            if slots:
                log_lineup(guild_id, club_name, slots, source="auto")
                log.info(f"[{club_name}] Auto-logged {len(slots)} positions for match {match_id}")

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
