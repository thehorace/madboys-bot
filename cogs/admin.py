"""An ephemeral control panel for the private allowlist, with checked UI actions."""
import discord
from discord import app_commands
from discord.ext import commands

from cogs.sessions import session_settings
from cogs.patchnotes import patch_settings, SOURCE
from cogs.usage import can_view
from config import CLUB_COLOUR
from interaction_tracking import TrackedView, failed
from db import get_setting, set_setting
from cogs.operations import status_embed
from cogs.session_reports import report_settings


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
        if self.page == 'clubs':
            from cogs.clubs import registry
            return discord.Embed(title='Monitored clubs', colour=CLUB_COLOUR, description='\n'.join(
                f"**{c['name']}** · ID {c['club_id']} · {'Enabled' if c['enabled'] else 'Disabled'}" for c in registry()) + '\n\nClubs are checked automatically. Toggle tracking below; history is retained. Changes apply on the next check without restarting.')
        if self.page == "home":
            return discord.Embed(title="🔒 Private admin panel", colour=CLUB_COLOUR,
                description="Your bot controls, visible only to you.\n\n"
                            "📊 **Usage** — people, lookups, trends and CSV\n"
                            "⚙️ **Settings** — sessions, tracker, summaries and EA News\n"
                            "🩺 **Bot status** — checks, schedules, backups and failures\n"
                            "💾 **Backup** — create and download a database snapshot\n"
                            "🗓️ **Session history** — results, actual players and sign-ups")
        if self.page == "status":
            return status_embed(self.bot, str(self.guild.id))
        if self.page == "settings":
            return discord.Embed(title="Bot settings", colour=CLUB_COLOUR,
                description="Choose a section below. All settings save across restarts.\n\n"
                    "**Sessions** — schedule, timezone, channel, sticky cooldown and waitlist\n"
                    "**Match tracker** — result posting and match channel\n"
                    "**Session summaries** — automatic recap, channel and 1–2 hour finish gap\n"
                    "**EA News** — automatic news and channel\n\nThese controls are private to your allowlist.")
        if self.page == "reports":
            settings = report_settings(str(self.guild.id))
            channel = f"<#{settings['channel']}>" if settings["channel"] else "#general"
            return discord.Embed(title="Session summary settings", colour=CLUB_COLOUR,
                description=f"Club recaps: **{'On' if settings['enabled'] else 'Off'}**\nPersonal DMs: **{'On' if settings['dms'] else 'Off'}**\nFinish after **{settings['gap']} minutes** without a game\nChannel: {channel}\n\n"
                    "One club recap and a personal DM for each linked player who played. No pings. Blocked DMs stay available in the panel. History saves even when posts are off. "
                    "Finishing waits for a successful EA check. First game must be within the selected gap after kick-off.")
        if self.page == "tracker_settings":
            tracker = self.bot.get_cog("MatchdayCog")
            gid = str(self.guild.id)
            cid = tracker.channel_id_for(gid) if tracker else None
            return discord.Embed(title="Match tracker settings", colour=CLUB_COLOUR,
                description=f"Result posts: **{'On' if get_setting(gid, 'matchday_enabled') != '0' else 'Off'}**\nChannel: {f'<#{cid}>' if cid else 'Choose a channel'}\n\nMatch data continues to be saved when posting is off. Weekly recaps use this channel too.")
        if self.page == "patchnotes":
            settings = patch_settings(str(self.guild.id))
            channel = f"<#{settings['channel_id']}>" if settings["channel_id"] else "#general"
            return discord.Embed(title="📰 Official FC 27 news & patch notes", colour=CLUB_COLOUR,
                description=f"Automatic posts: **{'On' if settings['enabled'] else 'Off'}**\nChannel: {channel}\n"
                            f"Checks every hour. Last successful check: {settings['last_checked']}\n\n"
                            "Patch notes, Pro Clubs and Grounds news post once, with details and the official link. No everyone ping.\n"
                            "Only newly published updates (within 24 hours) are announced. No startup backfill. Preview latest is private.\n\n"
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
        # Discord rejects a whole message if two components share an id ("Something went wrong").
        if any(getattr(c, "custom_id", None) == "admin:" + action for c in self.children):
            return
        button = discord.ui.Button(label=label, row=row, style=style, custom_id="admin:" + action)
        async def callback(interaction):
            await self.act(interaction, action)
        button.callback = callback
        self.add_item(button)

    def render(self):
        self.clear_items()
        if self.page == "home":
            for label, action in (("📊 Usage", "usage"), ("⚙️ Settings", "settings"), ("🩺 Bot status", "status"),
                                  ("💾 Backup", "backup"), ("🗓️ Session history", "history")):
                self.button(label, action)
        elif self.page == "status":
            pass   # "All settings" on the bottom row already links there
        elif self.page == "settings":
            for label, action in (("Sessions", "sessions"), ("Match tracker", "tracker_settings"),
                                  ("Session summaries", "reports"), ("EA News", "patchnotes"), ("Clubs", "clubs")):
                self.button(label, action)
        elif self.page == 'clubs':
            from cogs.clubs import registry
            self.button('Add club', 'add_club')
            picker = discord.ui.Select(placeholder='Toggle tracking for a club', row=1, options=[
                discord.SelectOption(label=c['name'][:100], value=str(c['club_id']), description='Enabled' if c['enabled'] else 'Disabled') for c in registry()])
            picker.callback = self.toggle_club
            self.add_item(picker)
        elif self.page in ("reports", "tracker_settings"):
            if self.page == "reports":
                settings = report_settings(str(self.guild.id))
                self.button("Disable recaps" if settings["enabled"] else "Enable recaps", "toggle_reports")
                self.button("Disable personal DMs" if settings["dms"] else "Enable personal DMs", "toggle_report_dms")
                self.button("Finish gap: 1 hour", "gap60")
                self.button("Finish gap: 2 hours", "gap120")
                self.button("Use #general", "reset_report_channel")
            else:
                self.button("Disable result posts" if get_setting(str(self.guild.id), "matchday_enabled") != "0" else "Enable result posts", "toggle_tracker")
            picker = discord.ui.ChannelSelect(placeholder="Summary channel" if self.page == "reports" else "Match result channel",
                channel_types=[discord.ChannelType.text], row=1, custom_id="admin:servicechannel")
            picker.callback = self.choose_service_channel
            self.add_item(picker)
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
        if self.page not in ("home", "settings"):
            self.button("All settings", "settings", row=2)
        self.button("Home", "home", row=2)

    async def show(self, interaction):
        self.render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def toggle_club(self, interaction):
        if not await self.authorize(interaction):
            return
        from cogs.clubs import toggle_club
        try:
            toggle_club(int(interaction.data['values'][0]))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.show(interaction)

    async def refresh_anchor(self, interaction=None):
        """
        Re-draw the panel after an action replied with its own message. Uses the current
        click's token: the /admin command's token expires after 15 minutes, and a panel that's
        been open that long used to show "Something went wrong" after every action.
        """
        self.render()
        message = getattr(interaction, "message", None)
        if message is not None:
            try:
                await interaction.followup.edit_message(message.id, embed=self.embed(), view=self)
                return
            except discord.HTTPException:
                pass
        try:
            await self.anchor.edit_original_response(embed=self.embed(), view=self)
        except discord.HTTPException:
            pass   # the action itself worked; Refresh re-draws the panel

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
        await self.refresh_anchor(interaction)

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
        if action == 'add_club':
            from cogs.clubs import ClubModal
            await interaction.response.send_modal(ClubModal(self))
        elif action in ("sessions", "patchnotes", "home", "refresh", "settings", "status", "reports", "tracker_settings", "clubs"):
            if action != "refresh":
                self.page = action
            await self.show(interaction)
        elif action == "history":
            reports = self.bot.get_cog("SessionReportsCog")
            if reports:
                await reports.show_history(interaction, private=True)
            else:
                await interaction.response.send_message("Session history is unavailable.", ephemeral=True)
        elif action in ("toggle_reports", "toggle_report_dms", "gap60", "gap120", "reset_report_channel", "toggle_tracker"):
            gid = str(self.guild.id)
            if action == "toggle_reports":
                enabled = not report_settings(gid)["enabled"]
                set_setting(gid, "reports:enabled", "1" if enabled else "0")
                if not enabled:
                    from db import connect
                    with connect() as conn:
                        conn.execute("UPDATE session_history SET message_id='suppressed' WHERE guild_id=? AND message_id IS NULL", (gid,))
            elif action == "toggle_report_dms":
                enabled = not report_settings(gid)["dms"]
                set_setting(gid, "reports:dms", "1" if enabled else "0")
                if not enabled:
                    from db import connect
                    with connect() as conn:
                        conn.execute("UPDATE session_summary_dms SET status='suppressed' WHERE status='pending' AND session_id IN (SELECT session_id FROM session_history WHERE guild_id=?)", (gid,))
            elif action.startswith("gap"):
                set_setting(gid, "reports:gap", action[3:])
            elif action == "reset_report_channel":
                set_setting(gid, "reports:channel", None)
            else:
                set_setting(gid, "matchday_enabled", "0" if get_setting(gid, "matchday_enabled") != "0" else "1")
            await self.show(interaction)
        elif action == "schedule":
            await interaction.response.send_modal(ScheduleModal(self))
        elif action in ("toggle_daily", "toggle_waitlist"):
            settings = session_settings(str(self.guild.id))
            key = "enabled" if action == "toggle_daily" else "waitlist"
            await self.session_action(interaction, **{key: not settings[key]})
        elif action == "skip":
            await self.run_command(interaction, "SessionsCog", "skip")
            await self.refresh_anchor(interaction)
        elif action == "toggle_patchnotes":
            settings = patch_settings(str(self.guild.id))
            await self.run_command(interaction, "PatchNotesCog", "settings", enabled=not settings["enabled"])
            await self.refresh_anchor(interaction)
        elif action in ("check_patchnotes", "preview_patchnotes"):
            await self.run_command(interaction, "PatchNotesCog", "check_command" if action == "check_patchnotes" else "preview")
            await self.refresh_anchor(interaction)
        else:
            cog, method = {"usage": ("UsageCog", "usage"), "health": ("OperationsCog", "status"),
                           "backup": ("OperationsCog", "backup"), "tracker": ("MatchdayCog", "matchday_status")}[action]
            await self.run_command(interaction, cog, method)

    async def choose_service_channel(self, interaction):
        if not await self.authorize(interaction):
            return
        channel = self.guild.get_channel(int(interaction.data["values"][0]))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Choose a text channel in this server.", ephemeral=True)
            return
        perms = channel.permissions_for(self.guild.me)
        if not (perms.send_messages and perms.embed_links and perms.view_channel):
            await interaction.response.send_message("Give the bot View Channel, Send Messages and Embed Links in that channel first.", ephemeral=True)
            return
        key = "reports:channel" if self.page == "reports" else "matchday_channel"
        set_setting(str(self.guild.id), key, str(channel.id))
        await self.show(interaction)

    async def choose_patch_channel(self, interaction):
        if not await self.authorize(interaction):
            return
        channel = self.guild.get_channel(int(interaction.data["values"][0]))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Choose a text channel in this server.", ephemeral=True)
            return
        await self.run_command(interaction, "PatchNotesCog", "settings", channel=channel)
        await self.refresh_anchor(interaction)

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
