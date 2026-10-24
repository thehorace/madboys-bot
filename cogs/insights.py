"""
/partners [player]   - best partnerships (or one player's best partners), for your club
/attendance [period] - who shows up: sign-ups vs actually playing
/records             - club records (biggest win, streaks, best single games, all-time leaders)
/wrapped [player]    - Season Wrapped: a player's season so far on a shareable card
"""
import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import insights as I
from clubs import club_id, club_name, club_scoped
from cogs.link import get_link
from cogs.stats import player_autocomplete, send_screen

PERIODS = [app_commands.Choice(name=label, value=key) for key, (label, _) in I.ATTENDANCE_DAYS.items()]


def _records(guild_id: str, cid: int, name: str):
    import potd
    import records
    from config import CLUB_COLOUR
    return records.build_embed(name, cid, potd.leaders(guild_id, cid, 1), CLUB_COLOUR)


async def build_records(guild_id: str):
    """Records page for the current club (runs the queries off the main loop)."""
    return await asyncio.to_thread(_records, str(guild_id), club_id(), club_name())


async def build_wrapped(guild_id, name: Optional[str]):
    """(embed, png) for one player's season so far, or a message saying why not."""
    import wrapped as Wr
    from datetime import datetime
    from cogs.sessions import _tz
    if not name:
        return "Link your EA name first (tap **Setup** on the panel), or pick a player."
    tz = _tz(str(guild_id))
    w = await asyncio.to_thread(Wr.compute, str(guild_id), name, datetime.now(tz))
    if not w:
        return f"No saved games for **{discord.utils.escape_markdown(name)}** this season yet."
    png = await asyncio.to_thread(Wr.render_card, w, tz)
    from config import CLUB_COLOUR
    embed = discord.Embed(title=f"🎁 {w['season']} Wrapped — {w['name']}"[:256], colour=CLUB_COLOUR,
                          description=f"{w['games']} games · {w['goals']} goals · {w['assists']} assists so far this season.")
    embed.set_image(url="attachment://result.png")
    return embed, png


class InsightsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="partners", description="Best partnerships — which pairs win most together")
    @app_commands.describe(player="See one player's best partners (leave blank for the top pairs)")
    @app_commands.autocomplete(player=player_autocomplete)
    @app_commands.guild_only()
    @club_scoped
    async def partners(self, interaction: discord.Interaction, player: Optional[str] = None):
        await interaction.response.defer()
        if player == "me":
            player = get_link(str(interaction.guild_id), str(interaction.user.id))
        await send_screen(interaction, await asyncio.to_thread(I.build_partnerships, player))

    @app_commands.command(name="attendance", description="Who shows up — sign-ups vs actually playing")
    @app_commands.describe(period="How far back to look (default: last 30 days)")
    @app_commands.choices(period=PERIODS)
    @app_commands.guild_only()
    async def attendance(self, interaction: discord.Interaction, period: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer()
        await send_screen(interaction, await I.build_attendance(interaction.guild, period.value if period else "30",
                                                                me=str(interaction.user.id)))


    @app_commands.command(name="records", description="Club records — biggest wins, streaks, best single games")
    @app_commands.guild_only()
    @club_scoped
    async def records(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await send_screen(interaction, await build_records(interaction.guild_id))


    @app_commands.command(name="wrapped", description="Season Wrapped — your season so far on a card")
    @app_commands.describe(player="Someone else's Wrapped (leave blank for yours)")
    @app_commands.autocomplete(player=player_autocomplete)
    @app_commands.guild_only()
    async def wrapped(self, interaction: discord.Interaction, player: Optional[str] = None):
        await interaction.response.defer()
        if not player or player == "me":
            player = get_link(str(interaction.guild_id), str(interaction.user.id))
        await send_screen(interaction, await build_wrapped(interaction.guild_id, player))


async def setup(bot: commands.Bot):
    await bot.add_cog(InsightsCog(bot))
