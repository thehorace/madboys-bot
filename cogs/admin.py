"""An ephemeral control panel for the private allowlist, with checked UI actions."""
import discord
from discord import app_commands
from discord.ext import commands

from cogs.sessions import session_settings
from cogs.patchnotes import patch_settings, SOURCE
from cogs.usage import can_view
from config import CLUB_COLOUR
from interaction_tracking import TrackedView, failed


class ScheduleModal(discord.ui.Modal, title="Daily session schedule"):
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        settings = session_settings(str(panel.guild.id))
        self.post = discord.ui.TextInput(label="Posting time", default=settings["post_time"], max_length=20)
        self.kickoff = discord.ui.TextInput(label="Kick-off time", default=settings["kickoff_time"], max_length=20)
        self.days = discord.ui.TextInput(label="Weekdays (comma-separated, or all)",
                                        default=",".join(settings["days"]), max_length=100,
                                        style=discord.TextStyle.paragraph)
        self.cooldown = discord.ui.TextInput(label="Sticky cooldown in seconds (30–3600)",
                                            default=str(settings["cooldown"]), max_length=4)
        self.tz = discord.ui.TextInput(label="Timezone (e.g. Australia/Sydney)", default=settings["timezone"],
                                       max_length=40)
        for item in (self.post, self.kickoff, self.days, self.cooldown, self.tz):
            self.add_item(item)

    async def on_submit(self, interaction):
        if not await self.panel.authorize(interaction):
            return
        try:
            cooldown = int(self.cooldown.value)
            if not 30 <= cooldown <= 3600:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Cooldown must be a whole number from 30 to 3600 seconds.", ephemeral=True)
            return
        await self.panel.session_action(interaction, post_time=self.post.value, kickoff_time=self.kickoff.value,
                                        days=self.days.value, cooldown=cooldown,
                                        timezone=self.tz.value)


class AdminView(TrackedView):
    def __init__(self, bot, user, guild, anchor):
        super().__init__(timeout=14 * 60)
        self.bot, self.user, self.guild, self.anchor = bot, user, guild, anchor
        self.page = "home"
        self.render()

    async def authorize(self, interaction):
        if (interaction.guild_id != self.guild.id or interaction.user.id != self.user.id
                or not can_view(interaction.user, interaction.guild)):
            failed(interaction)
            await interaction.response.send_message("This admin panel is private to its authorized owner.", ephemeral=True)
            return False
        return True

    async def interaction_check(self, interaction):
        return await self.authorize(interaction)

    def embed(self):
        if self.page == "home":
            return discord.Embed(title="🔒 Private admin panel", colour=CLUB_COLOUR,
                description="Your bot controls, visible only to you.\n\n"
                            "📊 **Usage** — people, lookups, trends and CSV\n"
                            "🎮 **Sessions** — daily schedule, channel, waitlist and skip today\n"
                            "🩺 **Health** — failures and saved backups\n"
                            "💾 **Backup** — create and download a database snapshot\n"
                            "📡 **Tracker status** — latest match check and posting state\n"
                            "📰 **EA News** — FC 27 patch notes, Pro Clubs and The Grounds")
        if self.page == "patchnotes":
            settings = patch_settings(str(self.guild.id))
            channel = f"<#{settings['channel_id']}>" if settings["channel_id"] else "#general"
            return discord.Embed(title="📰 Official FC 27 news & patch notes", colour=CLUB_COLOUR,
                description=f"Automatic posts: **{'On' if settings['enabled'] else 'Off'}**\nChannel: {channel}\n"
                            f"Checks every hour. Last successful check: {settings['last_checked']}\n\n"
                            "Patch notes, Pro Clubs and Grounds news post once, with details and the official link. No everyone ping.\n"
                            "First check posts only the newest current update. Preview latest is private.\n\n"
                            f"[Official EA source]({SOURCE})")
        settings = session_settings(str(self.guild.id))
        channel = f"<#{settings['channel_id']}>" if settings["channel_id"] else "#general"
        return discord.Embed(title="🎮 Private session controls", colour=CLUB_COLOUR,
            description=f"Daily posts: **{'On' if settings['enabled'] else 'Off'}**\n"
                        f"Post **{settings['post_time']}** · Kick-off **{settings['kickoff_time']}** ({settings['timezone']}, "
                        f"daylight saving handled automatically)\n"
                        f"Days: {', '.join(settings['days'])}\nChannel: {channel}\n"
                        f"Sticky: 10 messages, minimum **{settings['cooldown']} seconds**\n"
                        f"Waitlist: **{'On' if settings['waitlist'] else 'Off'}**\n"
                        f"Skipped date: {settings['skip_date'] or 'None'}\n\n"
                        "Changes also update today's sign-up if it's already posted (kick-off time, "
                        "reminder, channel). Use the channel picker to change where they post.")

    def button(self, label, action, row=0, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label, row=row, style=style, custom_id="admin:" + action)
        async def callback(interaction):
            await self.act(interaction, action)
        button.callback = callback
        self.add_item(button)

    def render(self):
        self.clear_items()
        if self.page == "home":
            for label, action in (("📊 Usage", "usage"), ("🎮 Sessions", "sessions"), ("🩺 Health", "health"),
                                  ("💾 Backup", "backup"), ("📡 Tracker status", "tracker")):
                self.button(label, action)
            self.button("📰 EA News", "patchnotes", row=1)
        elif self.page == "patchnotes":
            settings = patch_settings(str(self.guild.id))
            self.button("Disable patch posts" if settings["enabled"] else "Enable patch posts", "toggle_patchnotes")
            self.button("Check EA now", "check_patchnotes")
            self.button("Preview latest", "preview_patchnotes")
            picker = discord.ui.ChannelSelect(placeholder="Patch notes channel", channel_types=[discord.ChannelType.text],
                                              row=1, custom_id="admin:patchchannel")
            picker.callback = self.choose_patch_channel
            self.add_item(picker)
        else:
            settings = session_settings(str(self.guild.id))
            self.button("Disable daily posts" if settings["enabled"] else "Enable daily posts", "toggle_daily")
            self.button("Edit schedule", "schedule")
            self.button("Disable waitlist" if settings["waitlist"] else "Enable waitlist", "toggle_waitlist")
            self.button("Skip today", "skip", style=discord.ButtonStyle.danger)
            picker = discord.ui.ChannelSelect(placeholder="Daily session channel", channel_types=[discord.ChannelType.text],
                                              row=1, custom_id="admin:channel")
            picker.callback = self.choose_channel
            self.add_item(picker)
        self.button("Refresh", "refresh", row=2)
        self.button("Home", "home", row=2)

    async def show(self, interaction):
        self.render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def refresh_anchor(self):
        self.render()
        await self.anchor.edit_original_response(embed=self.embed(), view=self)

    async def run_command(self, interaction, cog_name, method, **kwargs):
        cog = self.bot.get_cog(cog_name)
        if not cog:
            await interaction.response.send_message("This tool isn't available right now. Check the bot logs.", ephemeral=True)
            return
        # Reuse the same command handlers, validation and permission checks.
        command = getattr(type(cog), method)
        await command.callback(cog, interaction, **kwargs)

    async def session_action(self, interaction, **kwargs):
        if not await self.authorize(interaction):
            return
        await self.run_command(interaction, "SessionsCog", "settings", **kwargs)
        await self.refresh_anchor()

    async def choose_channel(self, interaction):
        if not await self.authorize(interaction):
            return
        channel = self.guild.get_channel(int(interaction.data["values"][0]))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Choose a text channel in this server.", ephemeral=True)
            return
        await self.session_action(interaction, channel=channel)

    async def act(self, interaction, action):
        if not await self.authorize(interaction):
            return
        if action in ("sessions", "patchnotes", "home", "refresh"):
            if action != "refresh":
                self.page = action
            await self.show(interaction)
        elif action == "schedule":
            await interaction.response.send_modal(ScheduleModal(self))
        elif action in ("toggle_daily", "toggle_waitlist"):
            settings = session_settings(str(self.guild.id))
            key = "enabled" if action == "toggle_daily" else "waitlist"
            await self.session_action(interaction, **{key: not settings[key]})
        elif action == "skip":
            await self.run_command(interaction, "SessionsCog", "skip")
            await self.refresh_anchor()
        elif action == "toggle_patchnotes":
            settings = patch_settings(str(self.guild.id))
            await self.run_command(interaction, "PatchNotesCog", "settings", enabled=not settings["enabled"])
            await self.refresh_anchor()
        elif action in ("check_patchnotes", "preview_patchnotes"):
            await self.run_command(interaction, "PatchNotesCog", "check_command" if action == "check_patchnotes" else "preview")
            await self.refresh_anchor()
        else:
            cog, method = {"usage": ("UsageCog", "usage"), "health": ("OperationsCog", "status"),
                           "backup": ("OperationsCog", "backup"), "tracker": ("MatchdayCog", "matchday_status")}[action]
            await self.run_command(interaction, cog, method)

    async def choose_patch_channel(self, interaction):
        if not await self.authorize(interaction):
            return
        channel = self.guild.get_channel(int(interaction.data["values"][0]))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Choose a text channel in this server.", ephemeral=True)
            return
        await self.run_command(interaction, "PatchNotesCog", "settings", channel=channel)
        await self.refresh_anchor()

    async def on_timeout(self):
        try:
            await self.anchor.edit_original_response(view=None)
        except discord.HTTPException:
            pass


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="admin", description="Open your private bot admin panel")
    @app_commands.guild_only()
    async def admin(self, interaction: discord.Interaction):
        if not can_view(interaction.user, interaction.guild):
            await interaction.response.send_message("This admin panel is private.", ephemeral=True)
            return
        view = AdminView(self.bot, interaction.user, interaction.guild, interaction)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
