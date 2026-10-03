"""A private, resumable EA link → positions → buttons setup flow."""
import discord
from discord import app_commands
from discord.ext import commands

from cogs.link import get_link, set_link
from cogs.lineup import POSITION_GROUPS, get_prefs, set_prefs
from config import CLUB_COLOUR, CLUB_ID
from interaction_tracking import TrackedView, failed


class NameModal(discord.ui.Modal, title="Link your EA player"):
    name = discord.ui.TextInput(label="EA player name", max_length=100)

    def __init__(self, view):
        super().__init__()
        self.setup_view = view

    async def on_submit(self, interaction):
        if interaction.user.id != self.setup_view.user.id or not self.name.value.strip():
            failed(interaction)
            await interaction.response.send_message("Enter your EA player name.", ephemeral=True)
            return
        set_link(self.setup_view.gid, str(interaction.user.id), self.name.value.strip(), "self")
        self.setup_view.step = 1
        await self.setup_view.show(interaction)


class SetupView(TrackedView):
    def __init__(self, bot, user, guild_id, roster):
        super().__init__(timeout=840)
        self.bot, self.user, self.gid = bot, user, guild_id
        self.roster = sorted(set(roster), key=str.lower)
        self.page = 0
        linked = get_link(guild_id, str(user.id))
        self.step = 0 if not linked else 1 if not get_prefs(guild_id, str(user.id)) else 2
        self.render()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Open your own setup with `/setup`.", ephemeral=True)
            return False
        return True

    def embed(self):
        name = get_link(self.gid, str(self.user.id))
        positions = get_prefs(self.gid, str(self.user.id))
        if self.step == 0:
            text = "**1 / 3 · Link your EA player**\nPick your name from the squad roster, or enter it manually."
        elif self.step == 1:
            text = f"**2 / 3 · Choose your positions**\nLinked as **{name}**. Tick every position you have a build for."
        else:
            text = (f"**3 / 3 · You're ready**\nEA player: **{name}**\nPositions: **{', '.join(positions)}**\n\n"
                    "📊 **Stats** opens your private menu. **Me** shows your numbers; dropdowns show players and leaderboards.\n"
                    "✅ **In**, 🤔 **Maybe**, ❌ **Out** answer the daily session post. Extra players join the waitlist.\n"
                    "📢 **Share** posts a stats screen for the squad. 🛠️ **My builds** changes your positions later.")
        return discord.Embed(title="⚽ Welcome to the squad", description=text, colour=CLUB_COLOUR)

    def button(self, label, callback, row=1):
        button = discord.ui.Button(label=label, row=row)
        button.callback = callback
        self.add_item(button)

    def render(self):
        self.clear_items()
        if self.step == 0:
            chunk = self.roster[self.page * 25:(self.page + 1) * 25]
            if chunk:
                picker = discord.ui.Select(placeholder="Which EA player are you?", row=0,
                                           options=[discord.SelectOption(label=n[:100], value=n[:100]) for n in chunk])
                picker.callback = self.pick_name
                self.add_item(picker)
            self.button("Enter name manually", self.manual)
            if self.page:
                self.button("Previous players", self.previous)
            if (self.page + 1) * 25 < len(self.roster):
                self.button("More players", self.next_page)
        elif self.step == 1:
            prefs = get_prefs(self.gid, str(self.user.id))
            picker = discord.ui.Select(placeholder="Tick your build positions", row=0, min_values=1,
                                       max_values=len(POSITION_GROUPS), options=[
                                           discord.SelectOption(label=p, value=p, default=p in prefs) for p in POSITION_GROUPS])
            picker.callback = self.pick_positions
            self.add_item(picker)
            self.button("Change EA player", self.relink)
        else:
            self.button("Open stats", self.stats, row=0)
            self.button("Change positions", self.positions, row=0)
            self.button("Change EA player", self.relink, row=0)

    async def show(self, interaction):
        self.render()
        await interaction.response.edit_message(content=None, embed=self.embed(), view=self)

    async def pick_name(self, interaction):
        set_link(self.gid, str(self.user.id), interaction.data["values"][0], "self")
        self.step = 1
        await self.show(interaction)

    async def pick_positions(self, interaction):
        set_prefs(self.gid, str(self.user.id), interaction.data["values"])
        self.step = 2
        await self.show(interaction)

    async def manual(self, interaction):
        await interaction.response.send_modal(NameModal(self))

    async def relink(self, interaction):
        self.step, self.page = 0, 0
        await self.show(interaction)

    async def positions(self, interaction):
        self.step = 1
        await self.show(interaction)

    async def previous(self, interaction):
        self.page -= 1
        await self.show(interaction)

    async def next_page(self, interaction):
        self.page += 1
        await self.show(interaction)

    async def stats(self, interaction):
        from cogs.hub import StatsMenu
        await StatsMenu.open(self.bot, interaction)


async def open_setup(bot, interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    members = await bot.ea.get_member_stats(CLUB_ID) or []
    view = SetupView(bot, interaction.user, str(interaction.guild_id), [m["name"] for m in members if m.get("name")])
    await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)


class OnboardingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="setup", description="Link your EA player, choose positions and learn the buttons")
    @app_commands.guild_only()
    async def onboarding(self, interaction: discord.Interaction):
        await open_setup(self.bot, interaction)


async def setup(bot):
    await bot.add_cog(OnboardingCog(bot))
