"""
/ping, /help, /build
"""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import BUILDS_URL, CLUB_COLOUR, CLUB_NAME

HELP_SECTIONS = [
    ("📊 Stats", [
        ("/stats", "Button menu for everything below"),
        ("/lastgame", "Latest result with full player ratings"),
        ("/clubstats", "Season record, division, form"),
        ("/me", "Your stats + your last games (after /link me)"),
        ("/playerstats", "Anyone's season or career stats"),
        ("/leaderboard", "Rank the squad by goals, assists, rating..."),
        ("/compare", "Two players side by side"),
        ("/form", "Recent results"),
        ("/h2h", "Record vs a specific club"),
        ("/recap", "Summary of the last N days"),
        ("/motm table", "Squad MOTM awards (voted after every game)"),
    ]),
    ("🔗 Setup", [
        ("/link me", "Connect your Discord to your EA name — do this first!"),
        ("/prefer", "Pick the positions you're happy playing"),
    ]),
    ("🎮 Sessions & lineups", [
        ("/session create", "Post a 'who's on tonight?' sign-up"),
        ("/formation show", "Current lineup on a pitch"),
        ("/lineup suggest", "Manager: auto-pick from sign-ups, prefs and rotation"),
        ("/rotation check", "Who's been stuck in one role"),
    ]),
    ("📡 Match tracking", [
        ("/matchday start", "Manager: choose where results auto-post (one time)"),
        ("/panel", "Manager: post a pinned button panel"),
        ("/matchday status", "What the tracker is doing"),
        ("/status", "Is the bot / EA relay healthy?"),
    ]),
]

BUILD_POSITIONS = ["GK", "CB", "FB", "CDM", "CM", "CAM", "Winger", "ST"]


class MiscCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Check the bot is alive")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Pong! {round(self.bot.latency * 1000)}ms")

    @app_commands.command(name="help", description="What this bot can do")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(title=f"⚽ {CLUB_NAME} Bot", colour=CLUB_COLOUR,
                              description="Match results post automatically after every game.\n"
                                          "**Easiest way in: `/stats`** — a menu of buttons, no typing needed.")
        for name, cmds in HELP_SECTIONS:
            embed.add_field(name=name, value="\n".join(f"`{c}` — {d}" for c, d in cmds), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="build", description="Pro Clubs builds for a position")
    @app_commands.choices(position=[app_commands.Choice(name=p, value=p) for p in BUILD_POSITIONS])
    async def build(self, interaction: discord.Interaction, position: Optional[app_commands.Choice[str]] = None):
        if not BUILDS_URL:
            await interaction.response.send_message(
                "No builds site configured yet — set `BUILDS_URL` in the bot's environment.", ephemeral=True)
            return
        pos = f" for **{position.value}**" if position else ""
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open FC 27 Clubs Builder", url=BUILDS_URL))
        await interaction.response.send_message(f"🛠️ Plan your Pro Clubs build{pos}:", view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(MiscCog(bot))
