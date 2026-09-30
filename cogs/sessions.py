"""
Play sessions + RSVPs ("who's on tonight?").

  /session create <day> <time> [note]  - Post a sign-up with ✅ In / 🤔 Maybe / ❌ Out buttons
  /session list                        - Upcoming sessions
  /session cancel                      - Manager: cancel the next session

Extras:
  - 30 minutes before kick-off the bot pings everyone who said In/Maybe and
    says how many more players are needed for a full XI.
  - /lineup suggest only uses players who clicked ✅ for the current session.
  - While a session is on, the match tracker checks EA more often.
  - Buttons keep working after a bot restart (persistent view).

Times are entered in BOT_TZ (default Asia/Singapore) and shown to everyone
with Discord timestamps, which display in each viewer's own timezone.
"""

import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import BOT_TZ, CLUB_COLOUR, CLUB_NAME
from db import connect, now_iso
from utils import is_manager, resolve_name

log = logging.getLogger("madboys-bot.sessions")

SQUAD_SIZE = 11
REMIND_MINUTES = 30
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


# --------------------------------------------------------------------------- #
#  DB
# --------------------------------------------------------------------------- #
def get_session_by_message(message_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE message_id=?", (str(message_id),)).fetchone()
    return dict(row) if row else None


def get_rsvps(session_id: int) -> dict[str, list[str]]:
    out = {"yes": [], "maybe": [], "no": []}
    with connect() as conn:
        for r in conn.execute("SELECT discord_id, status FROM session_rsvps WHERE session_id=? ORDER BY updated_at",
                              (session_id,)):
            out.setdefault(r["status"], []).append(r["discord_id"])
    return out


def set_rsvp(session_id: int, discord_id: str, status: str):
    with connect() as conn:
        conn.execute("""
            INSERT INTO session_rsvps (session_id, discord_id, status, updated_at) VALUES (?,?,?,?)
            ON CONFLICT(session_id, discord_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at
        """, (session_id, discord_id, status, now_iso()))


def upcoming_sessions(guild_id: str, include_recent_hours: float = 0) -> list[dict]:
    since = int(time.time() - include_recent_hours * 3600)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM sessions WHERE guild_id=? AND cancelled=0 AND starts_at>=? ORDER BY starts_at",
                            (guild_id, since)).fetchall()
    return [dict(r) for r in rows]


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
    if s.get("note"):
        desc += f"\n{s['note']}"
    if s.get("cancelled"):
        desc = "~~" + desc.replace("\n", " ") + "~~\n**Cancelled.**"
    embed = discord.Embed(title=title, description=desc, colour=0x95A5A6 if s.get("cancelled") else CLUB_COLOUR)
    for key in ("yes", "maybe", "no"):
        names = [await resolve_name(guild, d) for d in rsvps[key]]
        embed.add_field(name=f"{STATUS_LABEL[key]} ({len(names)})", value="\n".join(names)[:1024] or "—", inline=True)
    need = max(0, SQUAD_SIZE - len(rsvps["yes"]))
    embed.set_footer(text=("Full XI! 🔥" if need == 0 else f"Need {need} more for a full XI"))
    return embed


class SessionView(discord.ui.View):
    """Persistent: fixed custom_ids, session looked up from the message the button is on."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _rsvp(self, interaction: discord.Interaction, status: str):
        s = get_session_by_message(interaction.message.id)
        if not s:
            await interaction.response.send_message("This session no longer exists.", ephemeral=True)
            return
        if s["cancelled"]:
            await interaction.response.send_message("This session was cancelled.", ephemeral=True)
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
        self.reminders.start()

    def cog_unload(self):
        self.reminders.cancel()

    session_group = app_commands.Group(name="session", description="Plan play sessions and see who's on")

    @session_group.command(name="create", description="Post a 'who's on?' sign-up for a play session")
    @app_commands.describe(day="Which day", time="Kick-off time, e.g. 21:00 or 9pm", note="Optional note")
    @app_commands.choices(day=[app_commands.Choice(name=d.capitalize(), value=d) for d in DAYS])
    async def session_create(self, interaction: discord.Interaction, day: app_commands.Choice[str], time: str,
                             note: Optional[str] = None):
        hm = parse_time(time)
        if not hm:
            await interaction.response.send_message("Couldn't read that time — try `21:00` or `9pm`.", ephemeral=True)
            return
        start = resolve_start(day.value, hm)
        if start.timestamp() < datetime.now(_tz()).timestamp() - 3600:
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
        await interaction.response.send_message(embed=await session_embed(interaction.guild, s), view=SessionView())
        msg = await interaction.original_response()
        with connect() as conn:
            conn.execute("UPDATE sessions SET message_id=? WHERE id=?", (str(msg.id), sid))

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

    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=1)
    async def reminders(self):
        now = int(time.time())
        with connect() as conn:
            due = [dict(r) for r in conn.execute(
                "SELECT * FROM sessions WHERE cancelled=0 AND reminded=0 AND starts_at BETWEEN ? AND ?",
                (now, now + REMIND_MINUTES * 60)).fetchall()]
            for s in due:
                conn.execute("UPDATE sessions SET reminded=1 WHERE id=?", (s["id"],))
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

    @reminders.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    bot.add_view(SessionView())  # re-attach button handlers after a restart
    await bot.add_cog(SessionsCog(bot))
