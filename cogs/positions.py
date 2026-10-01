"""
After-match position check + private rotation notes for managers.

After each result post, players whose exact position the bot can't be sure of
(no lineup was posted, they weren't in it, or EA's role disagrees with their
slot) get one small message:

    📍 Where did you play? — 3–1 vs Rival FC
    @Ali (defender) @Zak (midfielder)
    [↩️ Same as last game]  [▾ Pick your position…]

One tap each. Whoever doesn't answer keeps the broad role (DEF/MID/FWD) for
that game. The buttons are persistent, so they work after restarts.
Without a lineup, players are assumed to stay in the spot they played the
previous game this session (if EA's role still matches), so normally only
the first game of a session asks. /position fixes your last game any time.

Rotation notes: when someone has played the same exact position
ROTATION_THRESHOLD games in a row, a short note goes to the managers' channel
(once per streak), e.g. "Ali — LB 4 games in a row — has builds for CB, CM →
try CB next". Nothing is posted to players. No managers' channel set = no notes.
The channel is set from the 🧑‍💼 Manager menu ("Send rotation notes here") or
with MANAGER_CHANNEL_ID.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import positions as P
from cogs.rotation import set_match_position
from config import CLUB_NAME
from db import connect, get_setting, set_setting
from utils import resolve_name

log = logging.getLogger("madboys-bot.positions")

K_MANAGER_CHANNEL = "manager_channel"


def get_prompt(message_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM position_prompts WHERE message_id=?", (str(message_id),)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["pending"] = json.loads(d["pending"])
    return d


def _save_pending(message_id: str, pending: dict):
    with connect() as conn:
        conn.execute("UPDATE position_prompts SET pending=? WHERE message_id=?", (json.dumps(pending), message_id))


def _match_time_iso(match_id: str) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT ts FROM matches WHERE match_id=?", (match_id,)).fetchone()
    return datetime.fromtimestamp(row["ts"], timezone.utc).isoformat() if row and row["ts"] else None


def prompt_text(title: str, pending: dict) -> str:
    if not pending:
        return f"📍 **Positions — {title}**\n✅ All logged, thanks!"
    who = "  ".join(f"<@{did}> ({bucket})" for did, bucket in pending.items())
    return (f"📍 **Where did you play? — {title}**\n{who}\n"
            f"EA only says defender/midfielder/forward, so tap your exact spot (keeps rotation accurate).")


class PositionPromptView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        sel = discord.ui.Select(custom_id="madboys:pos:pick", placeholder="📍 Pick your position…",
                                options=[discord.SelectOption(label=p, value=p) for p in P.ALL_POSITIONS], row=1)
        sel.callback = self.on_pick
        self.add_item(sel)

    @discord.ui.button(label="Same as last game", emoji="↩️", style=discord.ButtonStyle.secondary,
                       custom_id="madboys:pos:same", row=0)
    async def same(self, interaction: discord.Interaction, _):
        prompt, bucket = await self._my_entry(interaction)
        if not prompt:
            return
        role = P.EA_BUCKET.get(bucket)
        last = P.last_exact_position(prompt["guild_id"], str(interaction.user.id), role)
        if not last:
            await interaction.response.send_message(
                "I don't have a confirmed position for you yet — pick one from the list.", ephemeral=True)
            return
        await self._record(interaction, prompt, last)

    async def on_pick(self, interaction: discord.Interaction):
        prompt, _ = await self._my_entry(interaction)
        if prompt:
            await self._record(interaction, prompt, interaction.data["values"][0])

    async def _my_entry(self, interaction: discord.Interaction) -> tuple[Optional[dict], Optional[str]]:
        prompt = get_prompt(interaction.message.id)
        if not prompt:
            await interaction.response.send_message("This prompt has expired.", ephemeral=True)
            return None, None
        uid = str(interaction.user.id)
        if uid not in prompt["pending"]:
            await interaction.response.send_message("You're all set for this game 👍", ephemeral=True)
            return None, None
        return prompt, prompt["pending"][uid]

    async def _record(self, interaction: discord.Interaction, prompt: dict, position: str):
        uid = str(interaction.user.id)
        set_match_position(prompt["guild_id"], CLUB_NAME, prompt["match_id"], uid, position,
                           logged_at=_match_time_iso(prompt["match_id"]))
        prompt["pending"].pop(uid, None)
        _save_pending(prompt["message_id"], prompt["pending"])
        await interaction.response.send_message(f"Logged **{position}** for {prompt['title']} ✅", ephemeral=True)
        try:
            await interaction.message.edit(content=prompt_text(prompt["title"], prompt["pending"]),
                                           view=None if not prompt["pending"] else discord.utils.MISSING,
                                           allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
        cog = interaction.client.get_cog("PositionsCog")
        if cog:
            await cog.check_notes(interaction.guild, [uid])


class PositionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="position", description="Set (or fix) where you played in your last game")
    @app_commands.choices(position=[app_commands.Choice(name=p, value=p) for p in P.ALL_POSITIONS])
    async def position(self, interaction: discord.Interaction, position: app_commands.Choice[str]):
        gid, uid = str(interaction.guild_id), str(interaction.user.id)
        with connect() as conn:
            row = conn.execute("""
                SELECT r.match_id, m.our_goals, m.opp_goals, m.opp_name FROM rotation_log r
                JOIN matches m ON m.match_id = r.match_id
                WHERE r.guild_id=? AND r.club=? AND r.discord_id=? ORDER BY m.ts DESC LIMIT 1""",
                (gid, CLUB_NAME, uid)).fetchone()
            prompts = conn.execute("SELECT message_id, pending FROM position_prompts WHERE guild_id=? AND match_id=?",
                                   (gid, row["match_id"] if row else "")).fetchall()
        if not row:
            await interaction.response.send_message(
                "I haven't tracked a game for you yet (are you linked? tap 👤 My stats on the panel).", ephemeral=True)
            return
        set_match_position(gid, CLUB_NAME, row["match_id"], uid, position.value,
                           logged_at=_match_time_iso(row["match_id"]))
        for pr in prompts:   # take them off that game's "where did you play?" list too
            pending = json.loads(pr["pending"])
            if pending.pop(uid, None) is not None:
                _save_pending(pr["message_id"], pending)
        await interaction.response.send_message(
            f"Logged **{position.value}** for your last game ({row['our_goals']}–{row['opp_goals']} vs {row['opp_name']}) ✅",
            ephemeral=True)
        await self.check_notes(interaction.guild, [uid])

    async def open_prompt(self, channel: discord.abc.Messageable, guild_id: str, match_id: str, title: str,
                          pending: dict[str, str]):
        """pending: {discord_id: EA bucket} for players whose exact spot is unknown."""
        if not pending:
            return
        try:
            msg = await channel.send(prompt_text(title, pending), view=PositionPromptView(),
                                     allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            log.warning("Couldn't post position prompt")
            return
        if msg is None:
            return
        with connect() as conn:
            conn.execute("INSERT OR REPLACE INTO position_prompts (message_id, guild_id, match_id, title, pending, created_at) "
                         "VALUES (?,?,?,?,?,?)", (str(msg.id), guild_id, match_id, title, json.dumps(pending),
                                                  int(time.time())))

    def manager_channel_id(self, guild_id: str) -> Optional[int]:
        cid = get_setting(guild_id, K_MANAGER_CHANNEL) or os.getenv("MANAGER_CHANNEL_ID")
        return int(cid) if cid else None

    async def check_notes(self, guild: Optional[discord.Guild], discord_ids: list[str]):
        """Post a private note for anyone who's just hit a streak in one exact position."""
        if guild is None:
            return
        gid = str(guild.id)
        cid = self.manager_channel_id(gid)
        if not cid:
            return
        from cogs.lineup import get_prefs
        lines = []
        for did in discord_ids:
            note = P.rotation_note(gid, did, get_prefs(gid, did))
            if not note:
                continue
            key, text = note
            if get_setting(gid, f"rotnote:{did}") == key:
                continue  # already told the managers about this streak
            set_setting(gid, f"rotnote:{did}", key)
            lines.append(f"• **{await resolve_name(guild, did)}** — {text}")
        if not lines:
            return
        try:
            ch = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
            await ch.send("🔄 **Rotation notes**\n" + "\n".join(lines)[:1900],
                          allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Couldn't post rotation notes")


async def setup(bot: commands.Bot):
    bot.add_view(PositionPromptView())
    await bot.add_cog(PositionsCog(bot))
