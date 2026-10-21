"""
Monthly awards on Discord.

  - In the first week of each month (from 10:00 in the session timezone), last month's
    awards card is posted for every club that played enough games, in the announcements
    channel. Each club/month is posted once, even across restarts.
  - Player of the Month gets the gold "👑 Player of the Month" role (taken off last month's
    winners) and the 👑 badge on their nickname.
  - /awards shows this month so far, or any past month (e.g. 2026-10).

The role needs the bot to have Manage Roles; the bot creates the role if it's missing.
"""
import asyncio
import io
import logging
import re
import time
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import awards as A
from clubs import club_id, club_name, club_scoped, monitored_clubs
from cogs.link import find_discord_id_by_ea_name
from cogs.operations import report_health
from cogs.stats import send_screen
from config import CLUB_COLOUR, GUILD_ID
from utils import survive

log = logging.getLogger("madboys-bot.awards")

ROLE_NAME = "👑 Player of the Month"
ROLE_COLOUR = discord.Colour(0xF1C40F)
POST_HOUR = 10          # local time on the 1st (catches up for the first week if the bot was down)
CATCH_UP_DAYS = 7


def _tz(gid: str):
    from cogs.sessions import _tz as session_tz
    return session_tz(gid)


def awards_embed(guild_id: str, club: str, a: dict, so_far: bool = False) -> discord.Embed:
    def who(entry):
        did = find_discord_id_by_ea_name(guild_id, entry["name"])
        return f"<@{did}>" if did else f"**{entry['name']}**"

    r = a["record"]
    title = f"🏆 {club} — {A.month_label(a['month'])}{' (so far)' if so_far else ''} awards"
    lines = [f"{r['games']} games · W{r['w']} D{r['d']} L{r['l']} · {r['gf']}–{r['ga']} goals", ""]
    if a.get("potm"):
        p = a["potm"]
        lines.append(f"👑 **Player of the Month:** {who(p)} — {p['rating']:.2f} avg in {p['games']} games, "
                     f"{p['goals']}G {p['assists']}A" + (f", {p['motm']} squad MOTM" if p.get("motm") else ""))
    for key, label, fmt in (("scorer", "⚽ Top scorer", "{value} goals"), ("assister", "🅰️ Top assists", "{value} assists"),
                            ("motm_king", "🗳️ Squad MOTM king", "{value} votes won"),
                            ("improved", "📈 Most improved", "+{value:.2f} rating"),
                            ("ever_present", "🎮 Ever-present", "{value} games")):
        if a.get(key):
            lines.append(f"{label}: {who(a[key])} — {fmt.format(**a[key])}")
    embed = discord.Embed(title=title, description="\n".join(lines), colour=CLUB_COLOUR)
    embed.set_image(url="attachment://awards.png")
    embed.set_footer(text=f"Player of the Month needs {a['min_games']}+ games • from every game the bot saved")
    return embed


async def build_awards(guild_id: str, month: Optional[str] = None):
    """Screen for the current club: (embed, png) or an error string."""
    tz = _tz(guild_id)
    now = datetime.now(tz)
    if month:
        m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})\s*", month)
        if not m or not 1 <= int(m.group(2)) <= 12:
            return "Use a month like `2026-10`."
        year, mon = int(m.group(1)), int(m.group(2))
    else:
        year, mon = now.year, now.month
    if (year, mon) > (now.year, now.month):
        return "That month hasn't happened yet."
    a = A.compute(club_id(), year, mon, tz)
    if not a:
        return (f"{club_name()} needs at least {A.MIN_CLUB_GAMES} saved games in "
                f"{A.month_label(f'{year:04d}-{mon:02d}')} for awards.")
    so_far = (year, mon) == (now.year, now.month)
    png = await asyncio.to_thread(A.render_card, club_name(), a)   # ~0.1s of drawing: keep it off the main loop
    return awards_embed(guild_id, club_name(), a, so_far), png


class AwardsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        A.init_awards()
        self.monthly.start()

    def cog_unload(self):
        self.monthly.cancel()

    def home_guild(self) -> Optional[discord.Guild]:
        if GUILD_ID:
            return self.bot.get_guild(int(GUILD_ID))
        return self.bot.guilds[0] if len(self.bot.guilds) == 1 else None

    @tasks.loop(minutes=30)
    @survive
    async def monthly(self):
        guild = self.home_guild()
        if guild:
            await self.post_due(guild)

    @monthly.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def post_due(self, guild: discord.Guild, now: Optional[datetime] = None) -> int:
        """Post last month's awards for any club that hasn't had them yet. Returns cards posted."""
        gid = str(guild.id)
        tz = _tz(gid)
        now = (now or datetime.now(tz)).astimezone(tz)
        if now.day > CATCH_UP_DAYS or (now.day == 1 and now.hour < POST_HOUR):
            return 0
        year, month = A.previous_month(now.year, now.month)
        label = f"{year:04d}-{month:02d}"
        tracker = self.bot.get_cog("MatchdayCog")
        cid = tracker.channel_id_for(gid) if tracker else None
        channel = (self.bot.get_channel(int(cid)) or await self.bot.fetch_channel(int(cid))) if cid else None
        posted = 0
        for club in monitored_clubs():
            if A.already_posted(gid, club["club_id"], label):
                continue
            a = A.compute(club["club_id"], year, month, tz)
            if not a:   # too few games: remember so we don't keep checking
                A.save(gid, club["club_id"], {"month": label, "skipped": True}, None, int(time.time()))
                continue
            if channel is None:
                await report_health(self.bot, gid, "Monthly awards", "No announcements/results channel to post the awards in.")
                break
            try:   # one club failing must not stop the other club's card (or the roles below)
                png = await asyncio.to_thread(A.render_card, club["name"], a)
                msg = await channel.send(content=f"🏆 **{club['name']} — {A.month_label(label)} awards!**",
                                         embed=awards_embed(gid, club["name"], a),
                                         file=discord.File(io.BytesIO(png), "awards.png"),
                                         allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.exception("Couldn't post %s's monthly awards", club["name"])
                await report_health(self.bot, gid, "Monthly awards", "Couldn't post a monthly awards card. Check channel permissions.")
                continue
            A.save(gid, club["club_id"], a, str(msg.id), int(time.time()))
            posted += 1
        if posted:
            # Every club's winner for this month (from what's saved, so a card posted on an earlier
            # run still counts) gets the role; anyone else loses it.
            winners = self.month_winners(gid, label)
            await self.update_role(guild, winners)
            badges = self.bot.get_cog("BadgesCog")
            for did in winners:
                if badges:
                    await badges.refresh_member(guild, did)
            await report_health(self.bot, gid, "Monthly awards")
        return posted

    def month_winners(self, gid: str, label: str) -> list[str]:
        import json
        from db import connect
        with connect() as conn:
            rows = conn.execute("SELECT data FROM monthly_awards WHERE guild_id=? AND month=?", (gid, label)).fetchall()
        out = []
        for r in rows:
            potm = json.loads(r["data"]).get("potm")
            did = find_discord_id_by_ea_name(gid, potm["name"]) if potm else None
            if did and did not in out:
                out.append(did)
        return out

    async def update_role(self, guild: discord.Guild, winner_ids: list[str]):
        """Give this month's Players of the Month the gold role, and take it off everyone else
        (including when nobody linked won, so last month's holder doesn't keep it forever)."""
        me = guild.me
        if not me.guild_permissions.manage_roles:
            await report_health(self.bot, str(guild.id), "Player of the Month role",
                                "Give the bot **Manage Roles** so it can hand out the 👑 Player of the Month role.")
            return
        role = discord.utils.get(guild.roles, name=ROLE_NAME)
        try:
            if role is None and not winner_ids:
                return
            if role is None:
                role = await guild.create_role(name=ROLE_NAME, colour=ROLE_COLOUR, hoist=True,
                                               reason="Monthly awards")
            if role >= me.top_role:
                await report_health(self.bot, str(guild.id), "Player of the Month role",
                                    f"Move the bot's role above **{ROLE_NAME}** in Server Settings → Roles.")
                return
            for member in list(role.members):
                if str(member.id) not in winner_ids:
                    await member.remove_roles(role, reason="New Player of the Month")
            for did in winner_ids:
                member = guild.get_member(int(did))
                if member and role not in member.roles:
                    await member.add_roles(role, reason="Player of the Month")
        except discord.Forbidden:
            await report_health(self.bot, str(guild.id), "Player of the Month role",
                                f"Move the bot's role above **{ROLE_NAME}** in Server Settings → Roles.")
            return
        except discord.HTTPException:
            log.exception("Couldn't update the Player of the Month role")
            return
        await report_health(self.bot, str(guild.id), "Player of the Month role")

    @app_commands.command(name="awards", description="Monthly awards card — this month so far, or any month")
    @app_commands.describe(month="A past month like 2026-10 (leave blank for this month so far)")
    @app_commands.guild_only()
    @club_scoped
    async def awards(self, interaction: discord.Interaction, month: Optional[str] = None):
        await interaction.response.defer()
        screen = await build_awards(str(interaction.guild_id), month)
        if isinstance(screen, tuple):
            await interaction.followup.send(embed=screen[0], file=discord.File(io.BytesIO(screen[1]), "awards.png"),
                                            allowed_mentions=discord.AllowedMentions.none())
        else:
            await send_screen(interaction, screen)


async def setup(bot: commands.Bot):
    await bot.add_cog(AwardsCog(bot))
