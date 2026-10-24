"""
/announce set <channel>   - Manager: send ALL automatic posts to this channel
/announce show            - Show where automatic posts go (and what still falls back)
/announce clear           - Manager: go back to the old per-feature channels

Automatic posts = match results, MOTM votes, milestones, weekly recap, the final
squad list at kick-off, session summaries, EA news. Daily sign-ups (the check-ins)
stay in #general or the /session settings channel. Slash commands keep replying in
whatever channel they were used in.
"""
import discord
from discord import app_commands
from discord.ext import commands

from announce import announce_channel_id, refresh_named, set_announce_channel
from utils import is_manager

NEEDED = ("view_channel", "send_messages", "embed_links", "attach_files", "read_message_history")


class AnnounceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        for guild in bot.guilds:           # cog loaded after the guilds are cached (e.g. reload)
            refresh_named(guild)

    # keep the by-name lookup fresh as the server changes
    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            refresh_named(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        refresh_named(guild)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        refresh_named(channel.guild)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        refresh_named(channel.guild)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        if before.name != after.name:
            refresh_named(after.guild)

    group = app_commands.Group(name="announce", description="Where the bot's automatic posts go",
                               guild_only=True)

    @group.command(name="set", description="Manager: post all automatic messages in this channel")
    @app_commands.describe(channel="Your #announcements channel")
    async def set(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not is_manager(interaction.user):
            await interaction.response.send_message("You need the Manager/Admin role for this.", ephemeral=True)
            return
        perms = channel.permissions_for(interaction.guild.me)
        missing = [n.replace("_", " ") for n in NEEDED if not getattr(perms, n)]
        if missing:
            await interaction.response.send_message(
                f"I can't post properly in {channel.mention} yet — missing: **{', '.join(missing)}**. "
                "Fix the channel permissions and run this again. Nothing was changed.", ephemeral=True)
            return
        set_announce_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(
            f"📣 Automatic posts now go to {channel.mention}: results, MOTM votes, milestones, weekly recaps, "
            f"the final squad list at kick-off, session summaries and EA news. Daily sign-ups (the check-ins) "
            f"stay in #general — change that with `/session settings channel:`. Commands still reply where you "
            f"use them.", ephemeral=True)

    @group.command(name="show", description="Show where automatic posts go")
    async def show(self, interaction: discord.Interaction):
        cid = announce_channel_id(interaction.guild_id)
        await interaction.response.send_message(
            f"📣 Automatic posts and the final squad list go to <#{cid}>. Daily sign-ups stay in #general "
            f"(or the `/session settings` channel)." if cid else
            "No #announcements channel found — each feature uses its own channel. Create a channel with that "
            "name, or run `/announce set`.",
            ephemeral=True)

    @group.command(name="clear", description="Manager: stop using a shared announcements channel")
    async def clear(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            await interaction.response.send_message("You need the Manager/Admin role for this.", ephemeral=True)
            return
        set_announce_channel(interaction.guild_id, None)
        await interaction.response.send_message(
            "Cleared. Automatic posts use the channel named #announcements if there is one, otherwise each feature's own channel (or #general).", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AnnounceCog(bot))
