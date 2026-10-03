"""Online SQLite backups and private, deduplicated operational alerts."""
import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import BOT_TZ, DB_PATH, GUILD_ID
from db import connect

log = logging.getLogger("madboys-bot.operations")
BACKUP_KEEP = max(1, int(os.getenv("BACKUP_KEEP_DAYS", "7")))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR") or str(Path(DB_PATH).parent / "backups"))
ALERT_USER_ID = os.getenv("ALERT_USER_ID", "").strip()


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
        with connect() as conn:
            problems = conn.execute("SELECT service,problem FROM service_health WHERE guild_id=? AND problem IS NOT NULL",
                                    (str(interaction.guild_id),)).fetchall()
        copies = sorted(BACKUP_DIR.glob("madboys-????-??-??.sqlite3"), reverse=True)
        text = "\n".join(f"⚠️ **{r['service']}**: {r['problem']}" for r in problems) or "✅ No recorded service failures."
        text += f"\nBackups saved: **{len(copies)}** · newest: **{copies[0].stem if copies else 'None yet'}**"
        text += "\nDaily backup after 3am; `/maintenance backup` saves and downloads a copy now."
        await interaction.response.send_message(text[:1900], ephemeral=True)

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
