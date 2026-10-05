"""Online SQLite backups and private, deduplicated operational alerts."""
import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import BOT_TZ, DB_PATH, GUILD_ID
from db import connect, get_setting

log = logging.getLogger("madboys-bot.operations")
BACKUP_KEEP = max(1, int(os.getenv("BACKUP_KEEP_DAYS", "7")))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR") or str(Path(DB_PATH).parent / "backups"))
ALERT_USER_ID = os.getenv("ALERT_USER_ID", "").strip()


def next_session_post(gid, now=None):
    from cogs.sessions import session_settings
    settings = session_settings(gid)
    if not settings["enabled"]:
        return None
    now = now or datetime.now(ZoneInfo(settings["timezone"]))
    now = now.astimezone(ZoneInfo(settings["timezone"]))
    for offset in range(8):
        day = now + timedelta(days=offset)
        if day.strftime("%A").lower() not in settings["days"] or day.date().isoformat() == settings["skip_date"]:
            continue
        post = day.replace(hour=int(settings["post_time"][:2]), minute=int(settings["post_time"][3:]), second=0, microsecond=0)
        start = day.replace(hour=int(settings["kickoff_time"][:2]), minute=int(settings["kickoff_time"][3:]), second=0, microsecond=0)
        if start <= now:
            continue
        midnight = day.replace(hour=0, minute=0, second=0, microsecond=0)
        with connect() as conn:
            existing = conn.execute("SELECT 1 FROM sessions WHERE guild_id=? AND starts_at>=? AND starts_at<? AND (message_id IS NOT NULL OR cancelled=1)",
                (gid, int(midnight.timestamp()), int((midnight + timedelta(days=1)).timestamp()))).fetchone()
        if not existing:
            return int(max(post, now).timestamp())
    return None


def status_embed(bot, gid):
    from cogs.patchnotes import patch_settings
    from cogs.session_reports import report_settings
    from cogs.sessions import session_settings
    from config import CLUB_ID, CLUB_COLOUR
    import match_data
    embed = discord.Embed(title="Bot status", colour=CLUB_COLOUR)
    tracker = bot.get_cog("MatchdayCog")
    def stamp(value):
        return f"<t:{int(value)}:R>" if value else "Not checked yet"
    if tracker:
        state = "Stopped" if not tracker.ticker.is_running() else "Check failed" if tracker.last_poll_ok is False else "Waiting for first check" if tracker.last_poll_ok is None else "Running"
        cid = tracker.channel_id_for(gid)
        successful = getattr(tracker, "last_success_at", None) or get_setting(gid, "matchday:last_success")
        embed.add_field(name="Match tracker", value=f"**{state}**\nLast attempt: {stamp(tracker.last_poll_at)}\nLast successful check: {stamp(successful)}\nNext check: {stamp(tracker.next_poll_at)}\nResults: {'On' if tracker.posting_enabled(gid) else 'Off'} · {f'<#{cid}>' if cid else 'No channel'}\nLatest game: {stamp(match_data.latest_match_ts(CLUB_ID))}", inline=False)
    else:
        embed.add_field(name="Match tracker", value="Unavailable", inline=False)
    sessions = bot.get_cog("SessionsCog")
    settings = session_settings(gid)
    running = sessions and all(getattr(sessions, name).is_running() for name in ("daily_sessions", "reminders", "session_updates"))
    next_post = next_session_post(gid)
    embed.add_field(name="Session scheduling", value=f"Tasks: {'Running' if running else 'Stopped / unavailable'} · Daily posts: {'On' if settings['enabled'] else 'Off'}\nNext post: {f'<t:{next_post}:F>' if next_post else 'None scheduled'}\nKick-off: {settings['kickoff_time']} ({settings['timezone']})", inline=False)
    news = patch_settings(gid)
    news_cog = bot.get_cog("PatchNotesCog")
    embed.add_field(name="EA news", value=f"Posts: {'On' if news['enabled'] else 'Off'} · Task: {'Running' if news_cog and news_cog.ticker.is_running() else 'Stopped / unavailable'}\nLast successful check: {news['last_checked']}", inline=False)
    copies = sorted(BACKUP_DIR.glob("madboys-????-??-??.sqlite3"), reverse=True)
    embed.add_field(name="Backups", value=f"{len(copies)} saved · Newest: {copies[0].stem if copies else 'None yet'}\nDaily after 3am ({BOT_TZ})", inline=False)
    reports = report_settings(gid)
    with connect() as conn:
        saved = conn.execute("SELECT COUNT(*) FROM session_history WHERE guild_id=?", (gid,)).fetchone()[0]
        has_health = conn.execute("SELECT 1 FROM sqlite_master WHERE name='service_health'").fetchone()
        problems = conn.execute("SELECT service,problem FROM service_health WHERE guild_id=? AND problem IS NOT NULL", (gid,)).fetchall() if has_health else []
    embed.add_field(name="Session summaries", value=f"Club posts: {'On' if reports['enabled'] else 'Off'} · Personal DMs: {'On' if reports['dms'] else 'Off'}\nFinish gap: {reports['gap']} minutes · {saved} sessions saved\nOnly closes after a successful EA check", inline=False)
    embed.add_field(name="Needs attention", value=("\n".join(f"**{r['service']}**: {r['problem']}" for r in problems)[:1024] or "No recorded failures. Check the service states above."), inline=False)
    embed.set_footer(text="Private • Refresh to update these values")
    return embed


def backup_database(source: str, directory: Path, date: str, keep: int = BACKUP_KEEP) -> Path:
    """SQLite's online backup includes committed WAL data; publish only checked copies."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"madboys-{date}.sqlite3"
    temporary = directory / f"madboys-{date}.sqlite3.tmp"
    try:
        with closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(temporary)) as dest:
                src.backup(dest)
                dest.commit()
                if dest.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    # Only prune this bot's dated backups, after a valid new copy exists.
    copies = sorted(directory.glob("madboys-????-??-??.sqlite3"), reverse=True)
    for old in copies[max(1, keep):]:
        old.unlink()
    return target


async def report_health(bot, guild_id, service: str, problem=None):
    cog = bot.get_cog("OperationsCog")
    if cog and guild_id:
        await cog.health(str(guild_id), service, problem)


class OperationsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._backup_lock = asyncio.Lock()
        self._health_lock = asyncio.Lock()
        with connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS service_health (guild_id TEXT, service TEXT, problem TEXT, "
                         "changed_at INTEGER, notified INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,service))")
        self.backups.start()
        self.monitor.start()

    def cog_unload(self):
        self.backups.cancel()
        self.monitor.cancel()

    def guild(self):
        return self.bot.get_guild(int(GUILD_ID)) if GUILD_ID else self.bot.guilds[0] if len(self.bot.guilds) == 1 else None

    async def recipients(self, guild_id):
        if ALERT_USER_ID:
            user = self.bot.get_user(int(ALERT_USER_ID)) or await self.bot.fetch_user(int(ALERT_USER_ID))
            return [user]
        from cogs.usage import can_view
        guild = self.bot.get_guild(int(guild_id))
        return [m for m in guild.members if not m.bot and can_view(m, guild)] if guild else []

    async def health(self, guild_id: str, service: str, problem=None):
        """Notify once per failure episode; no repeated reminders while unchanged."""
        async with self._health_lock:
            with connect() as conn:
                previous = conn.execute("SELECT * FROM service_health WHERE guild_id=? AND service=?",
                                        (guild_id, service)).fetchone()
                if problem is None and (previous is None or previous["problem"] is None):
                    return
                same_episode = previous is not None and bool(previous["problem"]) == bool(problem)
                if same_episode and previous["notified"]:
                    return
                # Do not send a recovery for a failure that never reached the owner.
                should_notify = problem is not None or (previous is not None and previous["notified"])
                conn.execute("INSERT INTO service_health (guild_id,service,problem,changed_at,notified) VALUES (?,?,?,?,0) "
                             "ON CONFLICT(guild_id,service) DO UPDATE SET problem=excluded.problem, changed_at=excluded.changed_at, notified=0",
                             (guild_id, service, str(problem)[:500] if problem else None, int(time.time())))
            if not should_notify:
                return
            text = (f"⚠️ **{service} needs attention**\n{problem}\nUse `/maintenance status` for health and backups."
                    if problem else f"✅ **{service} recovered.**")
            sent = False
            try:
                for user in await self.recipients(guild_id):
                    try:
                        await user.send(text[:1800], allowed_mentions=discord.AllowedMentions.none())
                        sent = True
                    except discord.HTTPException:
                        log.warning("Couldn't deliver private operational alert to %s", user.id)
            except discord.HTTPException:
                log.warning("Couldn't resolve private alert recipient")
            if sent:
                with connect() as conn:
                    conn.execute("UPDATE service_health SET notified=1 WHERE guild_id=? AND service=?", (guild_id, service))

    async def make_backup(self):
        async with self._backup_lock:
            date = datetime.now(ZoneInfo(BOT_TZ)).date().isoformat()
            return await asyncio.to_thread(backup_database, DB_PATH, BACKUP_DIR, date)

    @tasks.loop(minutes=30)
    async def backups(self):
        guild = self.guild()
        now = datetime.now(ZoneInfo(BOT_TZ))
        target = BACKUP_DIR / f"madboys-{now.date().isoformat()}.sqlite3"
        if now.hour < 3 or target.exists():
            return
        try:
            await self.make_backup()
        except Exception:
            log.exception("Database backup failed")
            if guild:
                await self.health(str(guild.id), "Database backups", "The daily backup failed. Check Railway storage and logs.")
        else:
            if guild:
                await self.health(str(guild.id), "Database backups")

    @tasks.loop(minutes=5)
    async def monitor(self):
        guild = self.guild()
        if not guild:
            return
        gid = str(guild.id)
        tracker = self.bot.get_cog("MatchdayCog")
        if tracker:
            stopped = not tracker.ticker.is_running()
            failing = tracker.last_poll_at and not tracker.last_poll_ok
            problem = "Match checks are failing. Check the EA relay and `/matchday status`." if failing else "The tracker loop stopped. Restart the bot and check logs." if stopped else None
            await self.health(gid, "Match tracking", problem)
        sessions = self.bot.get_cog("SessionsCog")
        if sessions:
            stopped = not sessions.daily_sessions.is_running() or not sessions.reminders.is_running() or not sessions.session_updates.is_running()
            await self.health(gid, "Session scheduling", "A session background task stopped. Restart the bot and check logs." if stopped else None)

    @backups.before_loop
    @monitor.before_loop
    async def before(self):
        await self.bot.wait_until_ready()

    maintenance = app_commands.Group(name="maintenance", description="Private bot health and database backups")

    @maintenance.command(name="status", description="Private: service health and saved backups")
    async def status(self, interaction: discord.Interaction):
        from cogs.usage import can_view
        if not can_view(interaction.user, interaction.guild):
            await interaction.response.send_message("This report is private.", ephemeral=True)
            return
        await interaction.response.send_message(embed=status_embed(self.bot, str(interaction.guild_id)), ephemeral=True)

    @maintenance.command(name="backup", description="Private: create and download a checked database backup")
    async def backup(self, interaction: discord.Interaction):
        from cogs.usage import can_view
        if not can_view(interaction.user, interaction.guild):
            await interaction.response.send_message("Backups are private.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        target = await self.make_backup()
        await self.health(str(interaction.guild_id), "Database backups")
        if target.stat().st_size > interaction.guild.filesize_limit:
            await interaction.followup.send("Backup saved, but it exceeds this server's upload limit. Download it from the Railway volume.", ephemeral=True)
        else:
            await interaction.followup.send("✅ Database backup checked and saved.", file=discord.File(target), ephemeral=True)


async def setup(bot):
    await bot.add_cog(OperationsCog(bot))
