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
        ("/club", "Choose your stats/lineup club, or auto-follow latest activity"),
        ("/lastgame", "Latest result with full player ratings"),
        ("/clubstats", "Season record, division, form"),
        ("/me", "Your stats + your last games (after /link me)"),
        ("/playerstats", "Anyone's season or career stats"),
        ("/leaderboard", "Rank the squad by goals, assists, rating..."),
        ("/passing", "Passing accuracy and volume by position"),
        ("/compare", "Two players side by side"),
        ("/form", "Recent results"),
        ("/h2h", "Record vs a specific club"),
        ("/scout", "Any opponent's record, form and our head-to-head"),
        ("/recap", "Summary of the last N days"),
        ("/weeklyprogress", "Compare the last two weeks"),
        ("/motm table", "Squad MOTM awards (voted after every game)"),
    ]),
    ("🏅 Squad", [
        ("/badges", "Your badges, and which emoji shows on your nickname"),
        ("/awards", "Monthly awards — this month's race, or any past month"),
        ("/partners", "Best partnerships — which pairs win most together"),
        ("/attendance", "Who shows up — sign-ups vs actually playing"),
        ("/records", "Club records — biggest wins, streaks, best single games"),
        ("/wrapped", "Season Wrapped — your season so far on a card"),
        ("/reminders", "Turn your 'are you playing tonight?' DMs on or off"),
    ]),
    ("🔗 Setup", [
        ("/setup", "Guided EA link, positions and button tour"),
        ("/link me", "Connect your Discord to your EA name — do this first!"),
        ("/link show", "See who someone is linked to"),
        ("/link list", "Manager: every link, and who in the roster isn't linked"),
        ("/builds", "Tick the positions you have a build for (or 🛠️ on the panel)"),
    ]),
    ("🎮 Sessions", [
        ("/session create", "Post a 'who's on tonight?' sign-up"),
        ("/session list", "Upcoming sessions and sign-ups"),
        ("/session cancel", "Manager: cancel the next upcoming session"),
        ("/session skip", "Admin: skip today's daily session"),
        ("/session settings", "Admin: daily session time and options"),
        ("/mysession", "Private personal summary after the session finishes"),
        ("/sessionhistory", "Browse finished sessions and your past summaries"),
        ("/sessionprogress", "Current activity and recap timing"),
        ("/motm close", "Manager: close the open MOTM vote now"),
    ]),
    ("🧑‍💼 Lineups & rotation", [
        ("/formation show", "Current lineup on a pitch"),
        ("/lineup builder", "Manager: build & post the lineup with buttons (or 🧑‍💼 on the panel)"),
        ("/rotation check", "Who's been stuck in one position"),
        ("/rotation history", "Where someone has played lately"),
        ("/rotation stats", "Games per role for each player"),
        ("/position", "Fix where you played in your last game"),
    ]),
    ("📡 Match tracking", [
        ("/matchday start", "Manager: choose where results auto-post (one time)"),
        ("/announce set", "Manager: send all automatic posts to this channel"),
        ("/announce show", "Where automatic posts go"),
        ("/panel", "Manager: post a pinned button panel"),
        ("/matchday status", "Private: what the tracker is doing"),
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
        from clubs import monitored_clubs
        names = " & ".join(c["name"] for c in monitored_clubs()) or CLUB_NAME
        embed = discord.Embed(title=f"⚽ {names} Bot"[:256], colour=CLUB_COLOUR,
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
