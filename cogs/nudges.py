"""
Nudging regulars: an hour before kick-off, a private DM to regular players who haven't
answered today's sign-up yet, with ✅ In / 🤔 Maybe / ❌ Out buttons right in the DM.

A "regular" is a linked player who actually played in at least REGULAR_MIN of the last
REGULAR_OF finished sessions. Anyone who already answered (even Out) is left alone, each
player gets at most one nudge per session, and 🔕 in the DM (or /reminders off) stops them.
Admins can switch the whole thing off in /admin → Sessions.

/reminders on|off   - your own nudges
"""
import json
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from db import connect, get_setting, set_setting
from interaction_tracking import failed
from utils import survive

log = logging.getLogger("madboys-bot.nudges")

NUDGE_MINUTES = 60          # how long before kick-off
LATEST_MINUTES = 10         # don't nudge if kick-off is closer than this (e.g. after a restart)
REGULAR_OF, REGULAR_MIN = 4, 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_nudges (
    session_id INTEGER NOT NULL,
    discord_id TEXT NOT NULL,          -- '*' marks the session as handled
    sent_at    INTEGER NOT NULL,
    PRIMARY KEY (session_id, discord_id)
);
"""


def init_nudges():
    with connect() as conn:
        conn.executescript(SCHEMA)


def nudges_enabled(guild_id: str) -> bool:
    return get_setting(str(guild_id), "session:nudges") != "0"


def opted_out(guild_id: str, discord_id: str) -> bool:
    return get_setting(str(guild_id), f"nudge:optout:{discord_id}") == "1"


def regulars(guild_id: str) -> dict[str, int]:
    """{discord_id: sessions played} for players in REGULAR_MIN+ of the last REGULAR_OF finished sessions."""
    with connect() as conn:
        rows = conn.execute("""SELECT h.players FROM session_history h JOIN sessions s ON s.id=h.session_id
                               WHERE h.guild_id=? AND h.outcome='completed' ORDER BY s.starts_at DESC LIMIT ?""",
                            (str(guild_id), REGULAR_OF)).fetchall()
    played: dict[str, int] = {}
    for r in rows:
        for did in {d for p in json.loads(r["players"]) for d in p.get("discord_ids", [])}:
            played[did] = played.get(did, 0) + 1
    return {d: n for d, n in played.items() if n >= REGULAR_MIN}


def due_sessions(now: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT * FROM sessions s WHERE cancelled=0 AND message_id IS NOT NULL
              AND starts_at BETWEEN ? AND ?
              AND NOT EXISTS (SELECT 1 FROM session_nudges n WHERE n.session_id=s.id AND n.discord_id='*')""",
            (now + LATEST_MINUTES * 60, now + NUDGE_MINUTES * 60))]


def to_nudge(s: dict) -> list[tuple[str, int]]:
    """[(discord_id, sessions played)] who should get a DM for this session."""
    from cogs.sessions import get_rsvps
    answered = {d for ids in get_rsvps(s["id"]).values() for d in ids}
    with connect() as conn:
        sent = {r[0] for r in conn.execute("SELECT discord_id FROM session_nudges WHERE session_id=?", (s["id"],))}
    return [(d, n) for d, n in sorted(regulars(s["guild_id"]).items())
            if d not in answered and d not in sent and not opted_out(s["guild_id"], d)]


def mark(session_id: int, discord_id: str):
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO session_nudges VALUES (?,?,?)", (session_id, discord_id, int(time.time())))


class NudgeButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"nudge:(?P<status>yes|maybe|no|stop):(?P<sid>\d+)"):
    """Buttons in the DM. Persistent: they keep working after a restart."""
    LOOK = {"yes": ("In", "✅", discord.ButtonStyle.success), "maybe": ("Maybe", "🤔", discord.ButtonStyle.secondary),
            "no": ("Out", "❌", discord.ButtonStyle.danger),
            "stop": ("Stop these reminders", "🔕", discord.ButtonStyle.secondary)}

    def __init__(self, status: str, session_id: int):
        label, emoji, style = self.LOOK[status]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style,
                                           custom_id=f"nudge:{status}:{int(session_id)}"))
        self.status, self.session_id = status, int(session_id)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["status"], int(match["sid"]))

    async def callback(self, interaction: discord.Interaction):
        from cogs.sessions import set_rsvp
        with connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (self.session_id,)).fetchone()
        s = dict(row) if row else None
        if s is None:
            failed(interaction)
            await interaction.response.edit_message(content="That session no longer exists.", view=None)
            return
        uid = str(interaction.user.id)
        if self.status == "stop":
            set_setting(s["guild_id"], f"nudge:optout:{uid}", "1")
            await interaction.response.edit_message(
                content="🔕 Got it — no more reminders. Turn them back on any time with `/reminders on`.", view=None)
            return
        if s["cancelled"] or s["starts_at"] <= int(time.time()):
            failed(interaction)
            await interaction.response.edit_message(
                content="Sign-ups for that session are closed." if not s["cancelled"] else "That session was cancelled.",
                view=None)
            return
        set_rsvp(s["id"], uid, self.status)
        words = {"yes": "✅ You're **In**", "maybe": "🤔 Marked as **Maybe**", "no": "❌ Marked as **Out**"}[self.status]
        await interaction.response.edit_message(
            content=f"{words} for kick-off <t:{s['starts_at']}:t>. Change it any time on the sign-up post.", view=None)
        sessions = interaction.client.get_cog("SessionsCog")
        guild = interaction.client.get_guild(int(s["guild_id"]))
        if sessions and guild:
            await sessions.refresh_today(guild, s["id"])   # update the counts on the sign-up post


def nudge_view(session_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for status in ("yes", "maybe", "no", "stop"):
        view.add_item(NudgeButton(status, session_id))
    return view


def nudge_text(s: dict, played: int, signup_url: Optional[str]) -> str:
    text = (f"👋 Kick-off is <t:{s['starts_at']}:R> (<t:{s['starts_at']}:t>) and you haven't answered tonight's "
            f"sign-up yet. You played {played} of the last {REGULAR_OF} sessions — are you in?")
    if signup_url:
        text += f"\n[Open the sign-up]({signup_url})"
    return text


class NudgesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_nudges()

    async def cog_load(self):
        self.bot.add_dynamic_items(NudgeButton)
        self.nudge_loop.start()

    def cog_unload(self):
        self.nudge_loop.cancel()

    async def send_nudges(self, s: dict) -> int:
        guild = self.bot.get_guild(int(s["guild_id"]))
        if guild is None or not nudges_enabled(s["guild_id"]):
            mark(s["id"], "*")
            return 0
        url = f"https://discord.com/channels/{guild.id}/{s['channel_id']}/{s['message_id']}"
        sent = 0
        for did, played in to_nudge(s):
            member = guild.get_member(int(did))
            mark(s["id"], did)   # once per player per session, even if their DMs are closed
            if member is None or member.bot:
                continue
            try:
                await member.send(nudge_text(s, played, url), view=nudge_view(s["id"]),
                                  allowed_mentions=discord.AllowedMentions.none())
                sent += 1
            except discord.HTTPException:
                log.info("Couldn't DM a nudge to %s (DMs closed?)", did)
        mark(s["id"], "*")
        if sent:
            log.info("Nudged %s regular(s) for session %s", sent, s["id"])
        return sent

    @tasks.loop(minutes=1)
    @survive
    async def nudge_loop(self):
        for s in due_sessions(int(time.time())):
            await self.send_nudges(s)

    @nudge_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="reminders", description="Turn your 'are you playing tonight?' DMs on or off")
    @app_commands.choices(setting=[app_commands.Choice(name="On", value="on"), app_commands.Choice(name="Off", value="off")])
    @app_commands.guild_only()
    async def reminders(self, interaction: discord.Interaction, setting: app_commands.Choice[str]):
        on = setting.value == "on"
        set_setting(str(interaction.guild_id), f"nudge:optout:{interaction.user.id}", None if on else "1")
        await interaction.response.send_message(
            "🔔 You'll get a DM before kick-off if you're a regular and haven't answered the sign-up."
            if on else "🔕 No more sign-up reminders by DM.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(NudgesCog(bot))
