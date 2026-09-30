"""
Links a Discord member to their EA Pro Clubs persona name, so match data from
EA can be matched back to a Discord account (auto rotation logging, /me,
milestone pings, lineup suggestions).

Commands:
  /link me <ea_name>            - Link your own EA name (autocompletes from the club roster)
  /link set <member> <ea_name>  - Manager: link on someone's behalf
  /link remove [member]         - Remove a link (yourself, or anyone if manager)
  /link show [member]           - Show a link
  /link list                    - Manager: list all links
"""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import CLUB_COLOUR, CLUB_ID, CLUB_NAME
from db import connect, now_iso
from utils import clip, is_manager, resolve_name

log = logging.getLogger("madboys-bot.link")


def set_link(guild_id: str, discord_id: str, ea_name: str, linked_by: str):
    with connect() as conn:
        conn.execute("""
            INSERT INTO ea_links (guild_id, discord_id, ea_name, linked_by, updated_at) VALUES (?,?,?,?,?)
            ON CONFLICT(guild_id, discord_id) DO UPDATE SET
                ea_name=excluded.ea_name, linked_by=excluded.linked_by, updated_at=excluded.updated_at
        """, (guild_id, discord_id, ea_name, linked_by, now_iso()))


def remove_link(guild_id: str, discord_id: str) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM ea_links WHERE guild_id=? AND discord_id=?",
                            (guild_id, discord_id)).rowcount > 0


def get_link(guild_id: str, discord_id: str) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT ea_name FROM ea_links WHERE guild_id=? AND discord_id=?",
                           (guild_id, discord_id)).fetchone()
    return row["ea_name"] if row else None


def get_all_links(guild_id: str) -> dict[str, str]:
    """{discord_id: ea_name}"""
    with connect() as conn:
        rows = conn.execute("SELECT discord_id, ea_name FROM ea_links WHERE guild_id=?", (guild_id,)).fetchall()
    return {r["discord_id"]: r["ea_name"] for r in rows}


def find_discord_id_by_ea_name(guild_id: str, ea_name: str) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT discord_id FROM ea_links WHERE guild_id=? AND ea_name=? COLLATE NOCASE",
                           (guild_id, ea_name)).fetchone()
    return row["discord_id"] if row else None


async def roster_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Suggest EA persona names from the club's member list (cached, so this is cheap)."""
    ea = getattr(interaction.client, "ea", None)
    names: list[str] = []
    if ea is not None:
        members = await ea.get_member_stats(CLUB_ID) or []
        names = [m.get("name") for m in members if m.get("name")]
    cur = current.lower()
    return [app_commands.Choice(name=n, value=n) for n in sorted(names, key=str.lower) if cur in n.lower()][:25]


class LinkCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    link_group = app_commands.Group(name="link", description="Link Discord accounts to EA Pro Clubs personas")

    async def _roster_note(self, ea_name: str) -> str:
        ea = getattr(self.bot, "ea", None)
        members = await ea.get_member_stats(CLUB_ID) if ea else None
        if members and not any((m.get("name") or "").lower() == ea_name.lower() for m in members):
            return (f"\n⚠️ **{ea_name}** isn't in {CLUB_NAME}'s current EA roster — double-check the spelling, "
                    f"or ignore this if they just joined.")
        return ""

    @link_group.command(name="me", description="Link your own EA Pro Clubs persona name")
    @app_commands.describe(ea_name="Your EA persona name — start typing to pick from the club roster")
    @app_commands.autocomplete(ea_name=roster_autocomplete)
    async def link_me(self, interaction: discord.Interaction, ea_name: str):
        await interaction.response.defer(ephemeral=True)
        set_link(str(interaction.guild_id), str(interaction.user.id), ea_name, linked_by="self")
        await interaction.followup.send(
            f"✅ Linked you to EA persona **{ea_name}**. Your positions will be tracked automatically "
            f"after each match, and `/me` now shows your stats." + await self._roster_note(ea_name),
            ephemeral=True)

    @link_group.command(name="set", description="Manager: link a member to an EA persona")
    @app_commands.describe(member="The Discord member", ea_name="Their EA persona name")
    @app_commands.autocomplete(ea_name=roster_autocomplete)
    async def link_set(self, interaction: discord.Interaction, member: discord.Member, ea_name: str):
        if not is_manager(interaction.user):
            await interaction.response.send_message(
                "You need the Manager/Admin role to link someone else. They can use `/link me`.", ephemeral=True)
            return
        await interaction.response.defer()
        set_link(str(interaction.guild_id), str(member.id), ea_name, linked_by=str(interaction.user.id))
        await interaction.followup.send(f"✅ Linked **{member.display_name}** to EA persona **{ea_name}**."
                                        + await self._roster_note(ea_name))

    @link_group.command(name="remove", description="Remove an EA link (yours, or anyone's if you're a manager)")
    @app_commands.describe(member="Leave blank for yourself")
    async def link_remove(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        if target.id != interaction.user.id and not is_manager(interaction.user):
            await interaction.response.send_message("Only managers can remove someone else's link.", ephemeral=True)
            return
        ok = remove_link(str(interaction.guild_id), str(target.id))
        await interaction.response.send_message(
            f"🗑️ Removed link for **{target.display_name}**." if ok else f"**{target.display_name}** wasn't linked.",
            ephemeral=True)

    @link_group.command(name="show", description="Show the linked EA persona for yourself or someone else")
    @app_commands.describe(member="Leave blank to check yourself")
    async def link_show(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        ea_name = get_link(str(interaction.guild_id), str(target.id))
        msg = (f"**{target.display_name}** → EA persona **{ea_name}**" if ea_name
               else f"**{target.display_name}** isn't linked yet. Use `/link me` to set it.")
        await interaction.response.send_message(msg, ephemeral=True)

    @link_group.command(name="list", description="Manager: list all EA persona links, and who in the roster isn't linked")
    async def link_list(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            await interaction.response.send_message("You need the Manager/Admin role for this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        links = get_all_links(str(interaction.guild_id))
        lines = [f"**{await resolve_name(interaction.guild, did)}** → {name}" for did, name in links.items()]
        embed = discord.Embed(title="🔗 EA Persona Links", description=clip("\n".join(lines) or "No links yet.", 4096),
                              colour=CLUB_COLOUR)

        members = await self.bot.ea.get_member_stats(CLUB_ID) if getattr(self.bot, "ea", None) else None
        if members:
            linked = {n.lower() for n in links.values()}
            missing = [m["name"] for m in members if m.get("name") and m["name"].lower() not in linked]
            if missing:
                embed.add_field(name=f"In the EA roster but not linked ({len(missing)})",
                                value=clip(", ".join(sorted(missing, key=str.lower))), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LinkCog(bot))
