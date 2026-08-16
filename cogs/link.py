"""
Links a Discord member to their EA Pro Clubs persona name, so match data
pulled from EA can be matched back to a discord_id for auto-logging
rotation history.

Commands:
  /link me <ea_name>              - Self-serve: link your own EA name
  /link set <member> <ea_name>    - Manager override: link on someone's behalf
  /link show [member]             - Show the current link for yourself or someone else
  /link list                      - Manager: list all links for the server
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils import resolve_name

log = logging.getLogger("madboys-bot.link")

DB_PATH = os.getenv("DB_PATH", "madboys.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ea_links (
                guild_id   TEXT NOT NULL,
                discord_id TEXT NOT NULL,
                ea_name    TEXT NOT NULL,
                linked_by  TEXT NOT NULL,   -- 'self' or the discord_id of the manager who set it
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, discord_id)
            );

            -- lets us go the other direction: EA name -> discord_id, case-insensitively
            CREATE INDEX IF NOT EXISTS idx_ea_links_name
                ON ea_links (guild_id, ea_name COLLATE NOCASE);
        """)


def set_link(guild_id: str, discord_id: str, ea_name: str, linked_by: str):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO ea_links (guild_id, discord_id, ea_name, linked_by, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, discord_id) DO UPDATE SET
                ea_name=excluded.ea_name, linked_by=excluded.linked_by, updated_at=excluded.updated_at
        """, (guild_id, discord_id, ea_name, linked_by, now))


def get_link(guild_id: str, discord_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT ea_name FROM ea_links WHERE guild_id=? AND discord_id=?",
            (guild_id, discord_id),
        ).fetchone()
        return row["ea_name"] if row else None


def get_all_links(guild_id: str) -> dict[str, str]:
    """Returns {discord_id: ea_name} for the whole server."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT discord_id, ea_name FROM ea_links WHERE guild_id=?",
            (guild_id,),
        ).fetchall()
        return {row["discord_id"]: row["ea_name"] for row in rows}


def find_discord_id_by_ea_name(guild_id: str, ea_name: str) -> str | None:
    """Case-insensitive lookup, EA name -> discord_id. Used by the auto-poller."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT discord_id FROM ea_links WHERE guild_id=? AND ea_name = ? COLLATE NOCASE",
            (guild_id, ea_name),
        ).fetchone()
        return row["discord_id"] if row else None


def _is_manager(member: discord.Member) -> bool:
    if member.guild_permissions.manage_channels:
        return True
    return any(r.name.lower() in ("manager", "admin", "coach") for r in member.roles)


class LinkCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()

    link_group = app_commands.Group(name="link", description="Link Discord accounts to EA Pro Clubs personas")

    @link_group.command(name="me", description="Link your own EA Pro Clubs persona name")
    @app_commands.describe(ea_name="Your exact EA persona name (case doesn't matter)")
    async def link_me(self, interaction: discord.Interaction, ea_name: str):
        set_link(str(interaction.guild_id), str(interaction.user.id), ea_name, linked_by="self")
        await interaction.response.send_message(
            f"✅ Linked your Discord account to EA persona **{ea_name}**.\n"
            f"Once you're in a logged league match, `/rotation` will start tracking your position automatically.",
            ephemeral=True,
        )

    @link_group.command(name="set", description="Manager: link a member to an EA persona on their behalf")
    @app_commands.describe(member="The Discord member", ea_name="Their exact EA persona name")
    async def link_set(self, interaction: discord.Interaction, member: discord.Member, ea_name: str):
        if not _is_manager(interaction.user):
            await interaction.response.send_message(
                "You need the Manager/Admin role to link on someone else's behalf. "
                "They can do it themselves with `/link me`.",
                ephemeral=True,
            )
            return

        set_link(str(interaction.guild_id), str(member.id), ea_name, linked_by=str(interaction.user.id))
        await interaction.response.send_message(
            f"✅ Linked **{member.display_name}** to EA persona **{ea_name}**."
        )

    @link_group.command(name="show", description="Show the linked EA persona for yourself or someone else")
    @app_commands.describe(member="Leave blank to check yourself")
    async def link_show(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        ea_name = get_link(str(interaction.guild_id), str(target.id))
        if not ea_name:
            await interaction.response.send_message(
                f"**{target.display_name}** isn't linked yet. Use `/link me` to set it.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"**{target.display_name}** → EA persona **{ea_name}**", ephemeral=True
        )

    @link_group.command(name="list", description="Manager: list all EA persona links for this server")
    async def link_list(self, interaction: discord.Interaction):
        if not _is_manager(interaction.user):
            await interaction.response.send_message(
                "You need the Manager/Admin role for this.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        links = get_all_links(str(interaction.guild_id))
        if not links:
            await interaction.followup.send("No links set up yet.", ephemeral=True)
            return

        lines = []
        for discord_id, ea_name in links.items():
            name = await resolve_name(interaction.guild, discord_id)
            lines.append(f"**{name}** → {ea_name}")

        embed = discord.Embed(
            title="🔗 EA Persona Links",
            description="\n".join(lines),
            colour=0x1E90FF,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LinkCog(bot))
