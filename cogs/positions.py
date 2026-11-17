"""Private position management and opt-in manager rotation notes.

New matches resolve automatically. Legacy prompt handlers remain available
for deliberate corrections, but no post-match prompts are created or retried.
"""

from clubs import club_id, club_name, club_scope, club_scoped
import json
import logging
import asyncio
import delivery
import os
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import positions as P
from cogs.rotation import set_match_position
from db import connect, get_setting, set_setting
from interaction_tracking import TrackedView
from utils import resolve_name
from cogs.position_menu import club_autocomplete

log = logging.getLogger("madboys-bot.positions")

K_MANAGER_CHANNEL = "manager_channel"
K_NOTE_DMS = "rotation_dm"     # JSON list of managers who get the notes by DM


def note_dm_list(guild_id: str) -> list[str]:
    raw = get_setting(guild_id, K_NOTE_DMS)
    return json.loads(raw) if raw else []


def toggle_note_dm(guild_id: str, user_id: str) -> bool:
    """Add/remove a manager from the rotation-note DMs. Returns True if they're now on the list."""
    ids = note_dm_list(guild_id)
    if user_id in ids:
        ids.remove(user_id)
        on = False
    else:
        ids.append(user_id)
        on = True
    set_setting(guild_id, K_NOTE_DMS, json.dumps(ids) if ids else None)
    return on


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
        row = conn.execute("SELECT ts FROM matches WHERE club_id=? AND match_id=?", (club_id(), match_id)).fetchone()
    return datetime.fromtimestamp(row["ts"], timezone.utc).isoformat() if row and row["ts"] else None


def prompt_text(title: str, pending: dict) -> str:
    if not pending:
        return f"📍 **Positions — {title}**\n✅ All logged, thanks!"
    who = "  ".join(f"<@{did}> ({bucket})" for did, bucket in pending.items())
    return (f"📍 **Where did you play? — {title}**\n{who}\n"
            f"EA only says defender/midfielder/forward, so tap your exact spot (keeps rotation accurate).")


class PositionPromptView(TrackedView):
    def resolve_club(self, interaction):
        prompt = get_prompt(interaction.message.id)
        return prompt['club_id'] if prompt else None

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
        set_match_position(prompt["guild_id"], club_name(), prompt["match_id"], uid, position,
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
        self.bot=bot
        # No queued post-match prompts should replay after this update.
        with connect() as conn:
            conn.execute("DELETE FROM delivery_jobs WHERE kind='position'")

    @app_commands.command(name='position',description='Your saved position, or correct one recorded game')
    @app_commands.describe(position='Save a default for future games; omit to open your private controls',
                           match_id='Optional correction only: last, or an EA match ID',club='Club for this default or correction')
    @app_commands.choices(position=[app_commands.Choice(name=p,value=p) for p in P.ALL_POSITIONS]+[app_commands.Choice(name='Clear saved position',value='_clear')])
    @app_commands.autocomplete(club=club_autocomplete)
    @app_commands.guild_only()
    @club_scoped
    async def position(self,interaction:discord.Interaction,position:Optional[app_commands.Choice[str]]=None,
                       match_id:Optional[str]=None,club:Optional[str]=None):
        from cogs.position_menu import open_menu,selected_club
        gid,uid=str(interaction.guild_id),str(interaction.user.id)
        try:
            if match_id is not None:
                match_id=match_id.strip()
                if not match_id or position is None:
                    raise ValueError('Choose a position and a match ID (or last) for a correction.')
                if match_id.casefold()=='last':
                    match_id='last'
            cid=selected_club(club) if club else club_id()
            if match_id=='last' and not club:
                cid=P.last_played_club(gid,uid) or cid
            if position is None:
                await open_menu(self.bot,interaction,cid)
                return
            with club_scope(cid):
                if match_id:
                    text=P.correct_position(gid,uid,position.value,match_id.strip())
                else:
                    P.save_default(gid,uid,None if position.value=='_clear' else position.value)
                    text=('Saved position cleared' if position.value=='_clear' else f'{position.value} saved for future games')+f' — {club_name()}. Past games stay as recorded.'
        except ValueError as exc:
            await interaction.response.send_message(str(exc),ephemeral=True)
            return
        await interaction.response.send_message(text,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    def manager_channel_id(self, guild_id: str) -> Optional[int]:
        cid = get_setting(guild_id, K_MANAGER_CHANNEL)
        if cid == "off":          # turned off from the lineup builder (beats the env default too)
            return None
        cid = cid or os.getenv("MANAGER_CHANNEL_ID")
        return int(cid) if cid and cid.isdigit() else None

    async def check_notes(self, guild: Optional[discord.Guild], discord_ids: list[str]):
        """Privately tell the managers about anyone who's just hit a streak in one area."""
        if guild is None:
            return
        gid = str(guild.id)
        cid = self.manager_channel_id(gid)
        dms = note_dm_list(gid)
        if not cid and not dms:
            return   # nobody has asked for notes
        from cogs.lineup import get_prefs
        lines = []
        for did in discord_ids:
            note = P.rotation_note(gid, did, get_prefs(gid, did))
            if not note:
                continue
            key, text = note
            setting_key = f"rotnote:{club_id()}:{did}"
            if get_setting(gid, setting_key) == key:
                continue  # already told the managers about this streak
            set_setting(gid, setting_key, key)
            lines.append(f"• **{await resolve_name(guild, did)}** — {text}")
        if not lines:
            return
        text = (f"🔄 **Rotation notes — {club_name()}**\n" + "\n".join(lines))[:1900]
        for uid in dms:   # private DMs to the managers who opted in
            try:
                user = self.bot.get_user(int(uid)) or await self.bot.fetch_user(int(uid))
                await user.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.warning(f"Couldn't DM rotation notes to {uid} (DMs closed?)")
        if cid:
            try:
                ch = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
                await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.warning("Couldn't post rotation notes")


async def setup(bot: commands.Bot):
    bot.add_view(PositionPromptView())
    await bot.add_cog(PositionsCog(bot))
