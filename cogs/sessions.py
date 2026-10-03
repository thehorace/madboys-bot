"""
Play sessions + RSVPs ("who's on tonight?").

  /session create <day> <time> [note]  - Post a sign-up with ✅ In / 🤔 Maybe / ❌ Out buttons
  /session list                        - Upcoming sessions
  /session cancel                      - Manager: cancel the next session

Extras:
  - 30 minutes before kick-off the bot pings everyone who said In/Maybe and
    says how many more players are needed for a full XI.
  - Late arrivals: a squad member who joins voice around session time is marked ✅
    automatically (shown with 🎧), so nobody has to remember to click.
  - Lineup suggestions use everyone who's actually around: ✅ sign-ups, squad
    members in voice, and anyone who played a game in the last 2 hours.
  - While a session is on, the match tracker checks EA more often.
  - Buttons keep working after a bot restart (persistent view).

Times are entered in BOT_TZ (default Asia/Singapore) and shown to everyone
with Discord timestamps, which display in each viewer's own timezone.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import BOT_TZ, CLUB_COLOUR, CLUB_NAME, DAILY_SESSIONS, GUILD_ID, SESSION_CHANNEL_ID
from db import connect, now_iso, set_setting
from utils import is_manager, resolve_name
from interaction_tracking import TrackedView, failed
from cogs.operations import report_health
from cogs.usage import can_view

log = logging.getLogger("madboys-bot.sessions")

SQUAD_SIZE = 11
REMIND_MINUTES = 30
STICKY_AFTER_MESSAGES = 10
STATUS_LABEL = {"yes": "✅ In", "maybe": "🤔 Maybe", "no": "❌ Out"}
DAYS = ["today", "tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(BOT_TZ)
    except Exception:
        return ZoneInfo("UTC")


def parse_time(text: str) -> Optional[tuple[int, int]]:
    """'21:00', '9pm', '9:30 pm', '2130', '21' -> (hour, minute)."""
    t = text.strip().lower().replace(".", ":").replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(am|pm)?", t)
    if not m:
        return None
    h, mi, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if ampm == "pm" else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h, mi


def resolve_start(day: str, hm: tuple[int, int], now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(_tz())
    base = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if day == "today":
        return base
    if day == "tomorrow":
        return base + timedelta(days=1)
    target = DAYS.index(day) - 2  # monday=0
    ahead = (target - now.weekday()) % 7
    start = base + timedelta(days=ahead)
    if start <= now:
        start += timedelta(days=7)
    return start


def session_settings(guild_id: str) -> dict:
    with connect() as conn:
        saved = {r["key"][8:]: r["value"] for r in conn.execute(
            "SELECT key,value FROM settings WHERE guild_id=? AND key LIKE 'session:%'", (guild_id,))}
    def setting(key, default):
        return saved.get(key, default)
    return {
        "enabled": setting("enabled", "1" if DAILY_SESSIONS else "0") == "1",
        "post_time": setting("post_time", "11:00"),
        "kickoff_time": setting("kickoff_time", "18:30"),
        "days": setting("days", "monday,tuesday,wednesday,thursday,friday,saturday,sunday").split(","),
        "channel_id": setting("channel_id", SESSION_CHANNEL_ID or ""),
        "cooldown": int(setting("cooldown", "300")),
        "waitlist": setting("waitlist", "1") == "1",
        "skip_date": setting("skip_date", ""),
    }


def daily_start(now: datetime, settings: Optional[dict] = None) -> Optional[int]:
    """Today's 18:30 session is due from 11:00 until kick-off, in BOT_TZ."""
    now = now.astimezone(_tz())
    settings = settings or {"enabled": True, "post_time": "11:00", "kickoff_time": "18:30",
                            "days": DAYS[2:], "skip_date": ""}
    if not settings["enabled"] or DAYS[now.weekday() + 2] not in settings["days"] or settings["skip_date"] == now.date().isoformat():
        return None
    ph, pm = parse_time(settings["post_time"])
    sh, sm = parse_time(settings["kickoff_time"])
    post_at = now.replace(hour=ph, minute=pm, second=0, microsecond=0)
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    return int(start.timestamp()) if post_at <= now < start else None


# --------------------------------------------------------------------------- #
#  DB
# --------------------------------------------------------------------------- #
def get_session_by_message(message_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE message_id=?", (str(message_id),)).fetchone()
    return dict(row) if row else None


def get_rsvps(session_id: int) -> dict[str, list[str]]:
    out = {"yes": [], "maybe": [], "no": [], "waitlist": []}
    with connect() as conn:
        session = conn.execute("SELECT guild_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        for r in conn.execute("SELECT discord_id, status FROM session_rsvps WHERE session_id=? ORDER BY updated_at, rowid",
                              (session_id,)):
            out.setdefault(r["status"], []).append(r["discord_id"])
    if session and session_settings(session["guild_id"])["waitlist"]:
        out["waitlist"], out["yes"] = out["yes"][SQUAD_SIZE:], out["yes"][:SQUAD_SIZE]
    return out


def set_rsvp(session_id: int, discord_id: str, status: str, source: str = "button"):
    with connect() as conn:
        conn.execute("""
            INSERT INTO session_rsvps (session_id, discord_id, status, updated_at, source) VALUES (?,?,?,?,?)
            ON CONFLICT(session_id, discord_id) DO UPDATE SET
                status=excluded.status,
                updated_at=CASE WHEN session_rsvps.status=excluded.status THEN session_rsvps.updated_at ELSE excluded.updated_at END,
                source=excluded.source
        """, (session_id, discord_id, status, now_iso(), source))


def rsvp_sources(session_id: int) -> dict[str, str]:
    with connect() as conn:
        return {r["discord_id"]: r["source"] or "button" for r in
                conn.execute("SELECT discord_id, source FROM session_rsvps WHERE session_id=?", (session_id,))}


def upcoming_sessions(guild_id: str, include_recent_hours: float = 0) -> list[dict]:
    since = int(time.time() - include_recent_hours * 3600)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM sessions WHERE guild_id=? AND cancelled=0 AND starts_at>=? ORDER BY starts_at",
                            (guild_id, since)).fetchall()
    return [dict(r) for r in rows]


def active_session(guild_id: str, before_minutes: int = 12 * 60) -> Optional[dict]:
    """The session happening 'now': started up to 3h ago, or starting within `before_minutes`."""
    now = time.time()
    with connect() as conn:
        row = conn.execute("""
            SELECT * FROM sessions WHERE guild_id=? AND cancelled=0 AND starts_at BETWEEN ? AND ?
            ORDER BY ABS(starts_at - ?) LIMIT 1
        """, (guild_id, int(now - 3 * 3600), int(now + before_minutes * 60), int(now))).fetchone()
    return dict(row) if row else None


def squad_ids(guild_id: str) -> set[str]:
    """Everyone who's clearly part of the squad: linked to an EA name, or has builds set."""
    with connect() as conn:
        a = {r[0] for r in conn.execute("SELECT discord_id FROM ea_links WHERE guild_id=?", (guild_id,))}
        b = {r[0] for r in conn.execute("SELECT discord_id FROM position_prefs WHERE guild_id=?", (guild_id,))}
    return a | b


def voice_squad(guild: discord.Guild) -> list[str]:
    """Squad members sitting in any voice channel right now."""
    squad = squad_ids(str(guild.id))
    return [str(m.id) for vc in getattr(guild, "voice_channels", []) for m in vc.members
            if not m.bot and str(m.id) in squad]


def played_recently(guild_id: str, hours: float = 2) -> list[str]:
    """Linked players who appeared in a tracked match in the last few hours."""
    from datetime import timezone as _tz_utc
    cutoff = datetime.fromtimestamp(time.time() - hours * 3600, _tz_utc.utc).isoformat()
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT discord_id FROM rotation_log WHERE guild_id=? AND match_id IS NOT NULL "
                            "AND logged_at>=?", (guild_id, cutoff)).fetchall()
    return [r[0] for r in rows]


def available_players(guild: discord.Guild) -> tuple[Optional[list[str]], dict[str, str]]:
    """
    Who's actually around to play, for lineup suggestions:
      ✅ clicked In on today's session
      🎧 is in a voice channel right now
      🎮 played a tracked game in the last 2 hours
    Someone who clicked ❌ but then turns up in voice or plays still counts (they showed up).
    -> (player ids, {id: reason emoji}), or (None, {}) when there's no sign of a session at
       all, so callers fall back to everyone with builds.
    """
    gid = str(guild.id)
    reasons: dict[str, str] = {}
    s = active_session(gid)
    if s:
        for did in get_rsvps(s["id"])["yes"]:
            reasons[did] = "✅"
    for did in voice_squad(guild):
        reasons.setdefault(did, "🎧")
    for did in played_recently(gid):
        reasons.setdefault(did, "🎮")
    if not reasons:
        return None, {}
    return list(reasons), reasons


def current_session_players(guild_id: str) -> Optional[list[str]]:
    """
    ✅ players for the session happening 'now' (started up to 3h ago, or
    starting within 12h). None if there's no such session — callers then fall
    back to everyone.
    """
    now = time.time()
    with connect() as conn:
        row = conn.execute("""
            SELECT id FROM sessions WHERE guild_id=? AND cancelled=0 AND starts_at BETWEEN ? AND ?
            ORDER BY ABS(starts_at - ?) LIMIT 1
        """, (guild_id, int(now - 3 * 3600), int(now + 12 * 3600), int(now))).fetchone()
    if not row:
        return None
    return get_rsvps(row["id"])["yes"]


async def session_embed(guild: discord.Guild, s: dict) -> discord.Embed:
    rsvps = get_rsvps(s["id"])
    title = f"🎮 {CLUB_NAME} — Who's on?"
    desc = f"**Kick-off:** <t:{s['starts_at']}:F> (<t:{s['starts_at']}:R>)"
    started = s["starts_at"] <= int(time.time())
    need = max(0, SQUAD_SIZE - len(rsvps["yes"]))
    status = "Session started" if started else "Full XI" if need == 0 else f"Need {need} more"
    desc = f"**{'🎮' if started else '✅' if need == 0 else '👥'} {status}**\n" + desc
    if s.get("note"):
        desc += f"\n{s['note']}"
    if s.get("cancelled"):
        desc = "~~" + desc.replace("\n", " ") + "~~\n**Cancelled.**"
    embed = discord.Embed(title=title, description=desc, colour=0x95A5A6 if s.get("cancelled") else CLUB_COLOUR)
    sources = rsvp_sources(s["id"])
    for key in ("yes", "maybe", "no"):
        names = [await resolve_name(guild, d) + (" 🎧" if sources.get(d) == "voice" else "") for d in rsvps[key]]
        embed.add_field(name=f"{STATUS_LABEL[key]} ({len(names)})", value="\n".join(names)[:1024] or "—", inline=True)
    if rsvps["waitlist"]:
        names = [await resolve_name(guild, d) for d in rsvps["waitlist"]]
        embed.add_field(name=f"⏳ Waitlist ({len(names)})", value="\n".join(names)[:1024], inline=False)
    need = max(0, SQUAD_SIZE - len(rsvps["yes"]))
    footer = "Full XI! 🔥" if need == 0 else f"Need {need} more for a full XI"
    if any(v == "voice" for v in sources.values()):
        footer += " • 🎧 = added automatically when they joined voice"
    embed.set_footer(text=footer)
    return embed


class SessionView(TrackedView):
    """Persistent: fixed custom_ids, session looked up from the message the button is on."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _rsvp(self, interaction: discord.Interaction, status: str):
        s = get_session_by_message(interaction.message.id)
        if not s:
            failed(interaction)
            await interaction.response.send_message("This session no longer exists.", ephemeral=True)
            return
        if s["cancelled"]:
            failed(interaction)
            await interaction.response.send_message("This session was cancelled.", ephemeral=True)
            return
        if s["starts_at"] <= int(time.time()):
            failed(interaction)
            await interaction.response.send_message("This session has started; sign-ups are closed.", ephemeral=True)
            return
        set_rsvp(s["id"], str(interaction.user.id), status)
        await interaction.response.edit_message(embed=await session_embed(interaction.guild, s), view=self)

    @discord.ui.button(label="In", emoji="✅", style=discord.ButtonStyle.success, custom_id="madboys:session:yes")
    async def yes(self, interaction: discord.Interaction, _):
        await self._rsvp(interaction, "yes")

    @discord.ui.button(label="Maybe", emoji="🤔", style=discord.ButtonStyle.secondary, custom_id="madboys:session:maybe")
    async def maybe(self, interaction: discord.Interaction, _):
        await self._rsvp(interaction, "maybe")

    @discord.ui.button(label="Out", emoji="❌", style=discord.ButtonStyle.danger, custom_id="madboys:session:no")
    async def no(self, interaction: discord.Interaction, _):
        await self._rsvp(interaction, "no")


class SessionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sticky_lock = asyncio.Lock()
        self.reminders.start()
        self.daily_sessions.start()
        self.session_updates.start()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.id == self.bot.user.id:
            return
        async with self._sticky_lock:
            with connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE guild_id=? AND channel_id=? AND cancelled=0 "
                    "AND starts_at>? AND message_id IS NOT NULL",
                    (str(message.guild.id), str(message.channel.id), int(time.time()))).fetchall()
                due = []
                for row in rows:
                    # Ignore delayed events from before the latest sign-up.
                    if message.id <= int(row["message_id"]):
                        continue
                    count = min(STICKY_AFTER_MESSAGES, row["sticky_messages"] + 1)
                    conn.execute("UPDATE sessions SET sticky_messages=? WHERE id=?", (count, row["id"]))
                    cooldown = session_settings(str(message.guild.id))["cooldown"]
                    if count >= STICKY_AFTER_MESSAGES and time.time() - row["sticky_at"] >= cooldown:
                        due.append(dict(row))
            for s in due:
                await self.repost_session(message.channel, message.guild, s)

    async def repost_session(self, channel, guild, s: dict):
        # Recheck after any wait: kick-off/cancellation must stop sticky posts.
        with connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=? AND cancelled=0 AND starts_at>?",
                               (s["id"], int(time.time()))).fetchone()
        if not row:
            return
        s = dict(row)
        try:
            new = await channel.send(embed=await session_embed(guild, s), view=SessionView(),
                                     allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Couldn't move session %s; will retry on the next message", s["id"])
            return
        with connect() as conn:
            conn.execute("UPDATE sessions SET message_id=?, sticky_messages=0, sticky_at=? WHERE id=?",
                         (str(new.id), int(time.time()), s["id"]))
            current = dict(conn.execute("SELECT * FROM sessions WHERE id=?", (s["id"],)).fetchone())
        # Keep votes/cancellations made while the new message was sending.
        try:
            await new.edit(embed=await session_embed(guild, current),
                           view=None if current["cancelled"] or current["starts_at"] <= int(time.time()) else SessionView(),
                           allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Couldn't refresh moved session %s", s["id"])
        old = channel.get_partial_message(int(s["message_id"]))
        try:
            await old.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            # Slash-command response messages can be undeletable by the bot.
            try:
                await old.edit(view=None, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.warning("Couldn't remove old session sign-up %s", s["message_id"])

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        """
        Late arrivals: a squad member who joins voice around session time is marked ✅
        automatically (even if they'd said ❌ — they showed up), so they're in the
        lineup suggestions without anyone having to click anything.
        """
        if member.bot or after.channel is None or before.channel is not None:
            return  # only care about "joined voice", not moving between channels / leaving
        gid = str(member.guild.id)
        s = active_session(gid, before_minutes=60)
        if not s or str(member.id) not in squad_ids(gid):
            return
        if str(member.id) in get_rsvps(s["id"])["yes"]:
            return
        set_rsvp(s["id"], str(member.id), "yes", source="voice")
        log.info(f"Auto-RSVP'd {member.display_name} to session {s['id']} (joined voice)")
        if s.get("message_id"):
            try:
                ch = self.bot.get_channel(int(s["channel_id"])) or await self.bot.fetch_channel(int(s["channel_id"]))
                msg = ch.get_partial_message(int(s["message_id"]))
                await msg.edit(embed=await session_embed(member.guild, s))
            except (discord.HTTPException, ValueError):
                pass

    def cog_unload(self):
        self.reminders.cancel()
        self.daily_sessions.cancel()
        self.session_updates.cancel()

    session_group = app_commands.Group(name="session", description="Plan play sessions and see who's on")

    @session_group.command(name="settings", description="Private: view or edit daily sessions and sticky settings")
    @app_commands.describe(post_time="Daily posting time, e.g. 11am", kickoff_time="Kick-off, e.g. 6:30pm",
                           days="Comma-separated weekdays, e.g. monday,wednesday,friday (or all)",
                           cooldown="Minimum seconds between sticky moves (default 300)",
                           waitlist="Queue extra sign-ups once 11 players are In")
    async def settings(self, interaction: discord.Interaction, enabled: Optional[bool] = None,
                       channel: Optional[discord.TextChannel] = None, post_time: Optional[str] = None,
                       kickoff_time: Optional[str] = None, days: Optional[str] = None,
                       cooldown: Optional[app_commands.Range[int, 30, 3600]] = None,
                       waitlist: Optional[bool] = None):
        if not can_view(interaction.user, interaction.guild):
            failed(interaction)
            await interaction.response.send_message("Session settings are private to the bot's authorized users.", ephemeral=True)
            return
        gid = str(interaction.guild_id)
        current = session_settings(gid)
        changes = {}
        for key, text in (("post_time", post_time), ("kickoff_time", kickoff_time)):
            if text is not None:
                hm = parse_time(text)
                if hm is None:
                    failed(interaction)
                    await interaction.response.send_message(f"Couldn't read {key}; try `11am` or `18:30`.", ephemeral=True)
                    return
                changes[key] = f"{hm[0]:02d}:{hm[1]:02d}"
        if parse_time(changes.get("post_time", current["post_time"])) >= parse_time(changes.get("kickoff_time", current["kickoff_time"])):
            failed(interaction)
            await interaction.response.send_message("Posting time must be earlier than kick-off on the same day.", ephemeral=True)
            return
        if days is not None:
            selected = DAYS[2:] if days.strip().lower() == "all" else [d.strip().lower() for d in days.split(",")]
            if not selected or any(d not in DAYS[2:] for d in selected):
                failed(interaction)
                await interaction.response.send_message("Use full weekday names separated by commas, or `all`.", ephemeral=True)
                return
            changes["days"] = ",".join(d for d in DAYS[2:] if d in selected)
        for key, value in (("enabled", enabled), ("waitlist", waitlist)):
            if value is not None:
                changes[key] = "1" if value else "0"
        if channel is not None:
            changes["channel_id"] = str(channel.id)
        if cooldown is not None:
            changes["cooldown"] = str(cooldown)
        with connect() as conn:
            for key, value in changes.items():
                conn.execute("INSERT INTO settings (guild_id,key,value) VALUES (?,?,?) "
                             "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value", (gid, "session:" + key, value))
        current = session_settings(gid)
        embed = discord.Embed(title="🎮 Session settings", colour=CLUB_COLOUR,
            description=f"Daily posts: **{'On' if current['enabled'] else 'Off'}**\n"
                        f"Post **{current['post_time']}** · Kick-off **{current['kickoff_time']}** ({BOT_TZ})\n"
                        f"Days: {', '.join(d.capitalize() for d in current['days'])}\n"
                        f"Channel: {('<#' + current['channel_id'] + '>') if current['channel_id'] else '#general'}\n"
                        f"Sticky: every 10 messages, at least **{current['cooldown']} seconds** apart\n"
                        f"Waitlist: **{'On' if current['waitlist'] else 'Off'}**\n"
                        f"Skipped date: {current['skip_date'] or 'None'}")
        embed.set_footer(text="Changes apply to future daily posts; existing session times stay as posted. /session skip skips today.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @session_group.command(name="skip", description="Private: skip today's daily session and cancel it if already posted")
    async def skip(self, interaction: discord.Interaction):
        if not can_view(interaction.user, interaction.guild):
            failed(interaction)
            await interaction.response.send_message("Skipping daily sessions is private to the bot's authorized users.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        local = datetime.now(_tz())
        gid = str(interaction.guild_id)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        set_setting(gid, "session:skip_date", local.date().isoformat())
        with connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM sessions WHERE guild_id=? AND created_by='daily' "
                                                "AND cancelled=0 AND starts_at>=? AND starts_at<?",
                                                (gid, int(midnight.timestamp()), int((midnight + timedelta(days=1)).timestamp())))]
            for s in rows:
                conn.execute("UPDATE sessions SET cancelled=1 WHERE id=?", (s["id"],))
        for s in rows:
            if not s["message_id"]:
                continue
            s["cancelled"] = 1
            try:
                ch = self.bot.get_channel(int(s["channel_id"])) or await self.bot.fetch_channel(int(s["channel_id"]))
                await ch.get_partial_message(int(s["message_id"])).edit(
                    embed=await session_embed(interaction.guild, s), view=None,
                    allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.warning("Couldn't update skipped session %s", s["id"])
        await interaction.followup.send("Skipped today's daily session. Future scheduled days continue normally.", ephemeral=True)

    @session_group.command(name="create", description="Post a 'who's on?' sign-up for a play session")
    @app_commands.describe(day="Which day", time="Kick-off time, e.g. 21:00 or 9pm", note="Optional note")
    @app_commands.choices(day=[app_commands.Choice(name=d.capitalize(), value=d) for d in DAYS])
    async def session_create(self, interaction: discord.Interaction, day: app_commands.Choice[str], time: str,
                             note: Optional[str] = None):
        hm = parse_time(time)
        if not hm:
            failed(interaction)
            await interaction.response.send_message("Couldn't read that time — try `21:00` or `9pm`.", ephemeral=True)
            return
        start = resolve_start(day.value, hm)
        if start.timestamp() < datetime.now(_tz()).timestamp() - 3600:
            failed(interaction)
            await interaction.response.send_message("That time has already passed today — pick Tomorrow?",
                                                    ephemeral=True)
            return
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (guild_id, channel_id, starts_at, note, created_by) VALUES (?,?,?,?,?)",
                (str(interaction.guild_id), str(interaction.channel_id), int(start.timestamp()), note,
                 str(interaction.user.id)))
            sid = cur.lastrowid
        set_rsvp(sid, str(interaction.user.id), "yes")  # the organiser is in
        s = {"id": sid, "starts_at": int(start.timestamp()), "note": note, "cancelled": 0}
        await interaction.response.send_message(content="@everyone", embed=await session_embed(interaction.guild, s),
                                                view=SessionView(),
                                                allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False))
        msg = await interaction.original_response()
        with connect() as conn:
            conn.execute("UPDATE sessions SET message_id=?, sticky_at=? WHERE id=?", (str(msg.id), int(datetime.now(_tz()).timestamp()), sid))

    @session_group.command(name="list", description="Upcoming sessions")
    async def session_list(self, interaction: discord.Interaction):
        sessions = upcoming_sessions(str(interaction.guild_id), include_recent_hours=3)
        if not sessions:
            await interaction.response.send_message("No upcoming sessions. Create one with `/session create`.",
                                                    ephemeral=True)
            return
        lines = []
        for s in sessions[:10]:
            r = get_rsvps(s["id"])
            link = (f" — [sign-up](https://discord.com/channels/{s['guild_id']}/{s['channel_id']}/{s['message_id']})"
                    if s.get("message_id") else "")
            lines.append(f"<t:{s['starts_at']}:F> • ✅ {len(r['yes'])} 🤔 {len(r['maybe'])}{link}")
        await interaction.response.send_message(
            embed=discord.Embed(title="🗓️ Upcoming sessions", description="\n".join(lines), colour=CLUB_COLOUR))

    @session_group.command(name="cancel", description="Manager: cancel the next upcoming session")
    async def session_cancel(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            failed(interaction)
            await interaction.response.send_message("Managers only.", ephemeral=True)
            return
        sessions = upcoming_sessions(str(interaction.guild_id), include_recent_hours=1)
        if not sessions:
            await interaction.response.send_message("No upcoming session to cancel.", ephemeral=True)
            return
        s = sessions[0]
        with connect() as conn:
            conn.execute("UPDATE sessions SET cancelled=1 WHERE id=?", (s["id"],))
        s["cancelled"] = 1
        try:
            ch = self.bot.get_channel(int(s["channel_id"])) or await self.bot.fetch_channel(int(s["channel_id"]))
            msg = await ch.fetch_message(int(s["message_id"]))
            await msg.edit(embed=await session_embed(interaction.guild, s), view=None)
        except (discord.HTTPException, TypeError, ValueError):
            pass
        await interaction.response.send_message(f"🚫 Cancelled the session at <t:{s['starts_at']}:F>.")

    async def post_daily_session(self, guild: discord.Guild, starts_at: int):
        settings = session_settings(str(guild.id))
        if settings["channel_id"]:
            channel = guild.get_channel(int(settings["channel_id"]))
        else:
            matches = [ch for ch in guild.text_channels if ch.name.lower() == "general"]
            channel = matches[0] if len(matches) == 1 else None
        if not isinstance(channel, discord.TextChannel):
            log.warning("Daily session: no unique #general in guild %s; set SESSION_CHANNEL_ID", guild.id)
            await report_health(self.bot, guild.id, "Daily session posting", "No session channel found. Set the channel with `/session settings`.")
            return False
        with connect() as conn:
            # Include cancelled sessions: cancelling today must not recreate it.
            rows = conn.execute("SELECT * FROM sessions WHERE guild_id=? AND starts_at=?",
                                (str(guild.id), starts_at)).fetchall()
            start = datetime.fromtimestamp(starts_at, _tz()).replace(hour=0, minute=0, second=0, microsecond=0)
            # Changing kick-off settings must not create a second daily session today.
            other = conn.execute("SELECT * FROM sessions WHERE guild_id=? AND created_by='daily' "
                                 "AND starts_at>=? AND starts_at<? AND starts_at<>?",
                                 (str(guild.id), int(start.timestamp()), int((start + timedelta(days=1)).timestamp()), starts_at)).fetchone()
            if other:
                if other["message_id"] or other["cancelled"]:
                    return
                # A failed, unsent post can adopt the new schedule without creating a second row.
                pending = dict(other)
                pending["starts_at"] = starts_at
                conn.execute("UPDATE sessions SET starts_at=? WHERE id=?", (starts_at, other["id"]))
                rows = [pending]
            if any(r["cancelled"] or r["message_id"] or r["created_by"] != "daily" for r in rows):
                return
            if rows:
                s = dict(rows[0])  # retry an unsent daily sign-up after a send failure
            else:
                cur = conn.execute(
                    "INSERT INTO sessions (guild_id, channel_id, starts_at, created_by) VALUES (?,?,?,?)",
                    (str(guild.id), str(channel.id), starts_at, "daily"))
                s = {"id": cur.lastrowid, "starts_at": starts_at, "note": None, "cancelled": 0}
        # Nobody is automatically signed up on behalf of the bot.
        msg = await channel.send(content="@everyone", embed=await session_embed(guild, s), view=SessionView(),
                                 allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False))
        with connect() as conn:
            conn.execute("UPDATE sessions SET channel_id=?, message_id=?, sticky_at=? WHERE id=?",
                         (str(channel.id), str(msg.id), int(time.time()), s["id"]))
        log.info("Posted daily session %s in #%s", s["id"], channel.name)

    @tasks.loop(minutes=1)
    async def daily_sessions(self):
        guild = (self.bot.get_guild(int(GUILD_ID)) if GUILD_ID else
                 self.bot.guilds[0] if len(self.bot.guilds) == 1 else None)
        if guild is None:
            log.warning("Daily session: set GUILD_ID to select the server")
            return
        starts_at = daily_start(datetime.now(_tz()), session_settings(str(guild.id)))
        if starts_at is None:
            return
        try:
            result = await self.post_daily_session(guild, starts_at)
        except discord.HTTPException:
            log.exception("Couldn't post daily session; will retry next minute")
            await report_health(self.bot, guild.id, "Daily session posting", "Couldn't send the daily sign-up. Check the bot's channel permissions.")
        else:
            if result is not False:
                await report_health(self.bot, guild.id, "Daily session posting")

    @daily_sessions.before_loop
    async def _before_daily(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def session_updates(self):
        now = int(time.time())
        with connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM sessions WHERE cancelled=0 AND started_shown=0 "
                                                "AND message_id IS NOT NULL AND starts_at BETWEEN ? AND ?", (now - 3 * 3600, now))]
        for s in rows:
            guild = self.bot.get_guild(int(s["guild_id"]))
            if not guild:
                continue
            try:
                channel = self.bot.get_channel(int(s["channel_id"])) or await self.bot.fetch_channel(int(s["channel_id"]))
                await channel.get_partial_message(int(s["message_id"])).edit(
                    embed=await session_embed(guild, s), view=None, allowed_mentions=discord.AllowedMentions.none())
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.warning("Couldn't show started status for session %s", s["id"])
                continue
            with connect() as conn:
                conn.execute("UPDATE sessions SET started_shown=1 WHERE id=?", (s["id"],))

    @session_updates.before_loop
    async def _before_updates(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=1)
    async def reminders(self):
        now = int(time.time())
        with connect() as conn:
            due = [dict(r) for r in conn.execute(
                "SELECT * FROM sessions WHERE cancelled=0 AND reminded=0 AND message_id IS NOT NULL "
                "AND starts_at BETWEEN ? AND ?",
                (now, now + REMIND_MINUTES * 60)).fetchall()]
        for s in due:
            r = get_rsvps(s["id"])
            ids = r["yes"] + r["maybe"]
            need = max(0, SQUAD_SIZE - len(r["yes"]))
            text = f"⏰ Kick-off <t:{s['starts_at']}:R>! " + " ".join(f"<@{i}>" for i in ids)
            text += " — full XI, let's go 🔥" if need == 0 else f"\nStill need **{need}** more — click ✅ on the sign-up if you can play."
            try:
                ch = self.bot.get_channel(int(s["channel_id"])) or await self.bot.fetch_channel(int(s["channel_id"]))
                ref = None
                if s.get("message_id"):
                    ref = discord.MessageReference(message_id=int(s["message_id"]), channel_id=int(s["channel_id"]),
                                                   fail_if_not_exists=False)
                await ch.send(text[:2000], reference=ref)
            except discord.HTTPException:
                log.warning(f"Couldn't send reminder for session {s['id']}")
                await report_health(self.bot, s["guild_id"], "Session reminders", "Couldn't send a session reminder. Check channel permissions.")
            else:
                with connect() as conn:
                    conn.execute("UPDATE sessions SET reminded=1 WHERE id=?", (s["id"],))
                await report_health(self.bot, s["guild_id"], "Session reminders")

    @reminders.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    bot.add_view(SessionView())  # re-attach button handlers after a restart
    await bot.add_cog(SessionsCog(bot))
