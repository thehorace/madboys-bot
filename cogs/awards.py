"""
Monthly awards on Discord.

  - In the first week of each month, one combined awards card covers all monitored
    clubs. Player totals and ratings are computed from individual match rows.
  - Player of the Month gets the gold "👑 Player of the Month" role (taken off last month's
    winners) and the 👑 badge on their nickname.
  - /awards shows this month so far, or any past month (e.g. 2026-10).

The role needs the bot to have Manage Roles; the bot creates the role if it's missing.
"""
import asyncio
import io
import json
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
from db import connect, get_setting, set_setting
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
    clubs = A.award_clubs()
    title = ' & '.join(c['name'] for c in clubs)
    a = await asyncio.to_thread(A.compute_combined, year, mon, tz)
    if not a:
        return (f"The squad needs at least {A.MIN_CLUB_GAMES} saved games in "
                f"{A.month_label(f'{year:04d}-{mon:02d}')} for awards.")
    so_far = (year, mon) == (now.year, now.month)
    png = await asyncio.to_thread(A.render_card, title, a)
    return awards_embed(guild_id, title, a, so_far), png


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
        """Post one combined monthly card, even when legacy per-club cards already exist."""
        gid = str(guild.id)
        tz = _tz(gid)
        now = (now or datetime.now(tz)).astimezone(tz)
        if now.day > CATCH_UP_DAYS or (now.day == 1 and now.hour < POST_HOUR):
            return 0
        year, month = A.previous_month(now.year, now.month)
        return await self.broadcast(guild, year, month)

    async def broadcast(self, guild, year, month, force=False):
        locks = getattr(self, '_broadcast_locks', None)
        if locks is None:
            self._broadcast_locks = locks = {}
        async with locks.setdefault(str(guild.id), asyncio.Lock()):
            return await self._broadcast(guild, year, month, force)

    async def _broadcast(self, guild, year, month, force):
        from announce import refresh_named, announce_channel_id
        gid = str(guild.id)
        tz = _tz(gid)
        label = f"{year:04d}-{month:02d}"
        tracker = self.bot.get_cog("MatchdayCog")
        cid = tracker.channel_id_for(gid) if tracker else None
        problems = []
        channels = {}
        if hasattr(guild, 'text_channels'):
            refresh_named(guild)
            general = [c for c in guild.text_channels if c.name.lower() == 'general']
            if len(general) == 1:
                channels[str(general[0].id)] = general[0]
            else:
                problems.append('There must be one unambiguous #general channel.')
            cid = announce_channel_id(gid, cid)
        try:
            channel = (self.bot.get_channel(int(cid)) or await self.bot.fetch_channel(int(cid))) if cid else None
            if channel:
                channels[str(cid)] = channel
            else:
                problems.append('Choose an announcements channel with /announce set.')
        except (discord.HTTPException, ValueError):
            problems.append('The announcements channel is unavailable.')
        round_key = f'awards:broadcast:{label}:round'
        generation = int(get_setting(gid, round_key) or 0)
        def delivered(channel_id):
            return get_setting(gid, f'awards:broadcast:{label}:{generation}:{channel_id}') is not None
        if force and channels and not problems and all(delivered(c) for c in channels):
            generation += 1
            set_setting(gid, round_key, str(generation))
        pending = [(cid,channel) for cid,channel in channels.items() if not delivered(cid)]
        if not pending:
            if problems:
                await report_health(self.bot,gid,'Monthly awards',' '.join(problems))
            return 0
        previous_winners = self.month_winners(gid, label)
        snapshot_key = f'awards:broadcast:{label}:{generation}:snapshot'
        saved_snapshot = get_setting(gid,snapshot_key)
        a = json.loads(saved_snapshot) if saved_snapshot else await asyncio.to_thread(A.compute_combined, year, month, tz)
        if not a:
            A.save(gid, A.COMBINED_ID, {'month':label,'skipped':True}, None, int(time.time()))
            return 0
        if not saved_snapshot:
            set_setting(gid,snapshot_key,json.dumps(a))
        title = ' & '.join(a['club_names'])
        png = await asyncio.to_thread(A.render_card, title, a)
        winner = a.get('potm')
        did = find_discord_id_by_ea_name(gid,winner['name']) if winner else None
        who = f'<@{did}>' if did else f"**{discord.utils.escape_markdown(winner['name'])}**" if winner else None
        content = f'@everyone\n👑 Congratulations {who} — **Player of the Month for {A.month_label(label)}!**' if winner else f'🏆 {A.month_label(label)} squad awards'
        content += f'\nCombined performances across **{title}**.'
        if winner:
            content += f"\n{winner['games']} matches · {winner['rating']:.2f} average rating · {winner['goals']} goals · {winner['assists']} assists. Well played! 🎉"
        if previous_winners:
            content += '\nThis combined result supersedes the earlier awards for this month.'
        posted = 0
        for cid, channel in pending:
            if winner and hasattr(channel,'permissions_for') and getattr(guild,'me',None):
                perms = channel.permissions_for(guild.me)
                if not perms.mention_everyone:
                    problems.append(f'Missing Mention Everyone permission in <#{cid}>.')
                    continue
            try:
                msg = await channel.send(content=content,embed=awards_embed(gid,title,a),
                    file=discord.File(io.BytesIO(png),'awards.png'),
                    allowed_mentions=discord.AllowedMentions(everyone=bool(winner),users=[discord.Object(id=int(did))] if did else False,roles=False,replied_user=False))
            except discord.HTTPException:
                log.exception('Monthly awards delivery failed for channel %s',cid)
                problems.append(f'Could not send the awards to <#{cid}>; check channel permissions.')
                continue
            with connect() as conn:
                conn.execute('INSERT OR REPLACE INTO settings (guild_id,key,value) VALUES (?,?,?)',
                             (gid,f'awards:broadcast:{label}:{generation}:{cid}',str(msg.id)))
                A.save(gid,A.COMBINED_ID,a,str(msg.id),int(time.time()),connection=conn)
            posted += 1
        if posted:
            winners = self.month_winners(gid,label)
            await self.update_role(guild,winners)
            badges = self.bot.get_cog('BadgesCog')
            if badges:
                for did in set(previous_winners+winners):
                    await badges.refresh_member(guild,did)
        await report_health(self.bot,gid,'Monthly awards',' '.join(problems) if problems else None)
        return posted

    @app_commands.command(name='resendawards',description='Private: resend combined monthly congratulations with @everyone to general and announcements')
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1,60,key=lambda i:i.guild_id)
    async def resendawards(self,interaction:discord.Interaction,month:Optional[str]=None):
        from cogs.usage import can_view
        if not can_view(interaction.user,interaction.guild):
            await interaction.response.send_message('This broadcast tool is private.',ephemeral=True)
            return
        now = datetime.now(_tz(str(interaction.guild_id)))
        if month is None:
            year,mon = A.previous_month(now.year,now.month)
        else:
            match = re.fullmatch(r'(\d{4})-(\d{1,2})',month.strip())
            if not match or not 1<=int(match[1])<=9998 or not 1<=int(match[2])<=12:
                await interaction.response.send_message('Use a completed month such as 2026-10.',ephemeral=True)
                return
            year,mon = int(match[1]),int(match[2])
        if (year,mon)>=(now.year,now.month):
            await interaction.response.send_message('Choose a completed month.',ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True,thinking=True)
        posted = await self.broadcast(interaction.guild,year,mon,force=True)
        await interaction.followup.send(f'Sent monthly awards to {posted} channel(s). Each successful channel gets one @everyone ping. If a channel is missing, check Bot status for permissions or channel setup, then retry; successful channels will not be repeated while that delivery is incomplete.',ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    def month_winners(self, gid: str, label: str) -> list[str]:
        import json
        from db import connect
        with connect() as conn:
            rows = conn.execute("SELECT club_id,data FROM monthly_awards WHERE guild_id=? AND month=?", (gid, label)).fetchall()
        combined = [r for r in rows if r['club_id'] == A.COMBINED_ID]
        rows = combined or rows
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
