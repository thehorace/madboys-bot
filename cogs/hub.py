"""
Button & dropdown menus, for people who'd rather click than type commands.

  /stats   - Opens a private stats menu (only you see it). Everything is
             buttons and dropdowns; each click updates the same message.
  /panel   - Manager: posts a permanent "control panel" message in the
             channel (and pins it). Anyone can click its buttons at any time,
             even after the bot restarts, to open their own private menu.

Menu layout (Discord allows 5 rows; a dropdown takes a whole row):
  row 0  [📡 Last game] [📊 Club] [📈 Form] [🗓️ Recap] [👤 Me]
  row 1  ▾ Player stats…
  row 2  ▾ Leaderboards…
  row 3  ▾ Compare with…          (on a player page)
         ▾ Head-to-head vs…       (elsewhere, once some matches are tracked)
         ▾ Which player are you?  (when "Me" is clicked by someone not linked yet)
  row 4  [📅 Season ⇄ 🕰️ Career] [🏠 Home] [📢 Share]

All screens come from the same build_* functions the slash commands use
(cogs/stats.py), so the menu and the commands always agree.
"""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import match_data as md
from cogs import stats as S
from cogs.link import get_link, set_link
from config import CLUB_COLOUR, CLUB_ID, CLUB_NAME
from utils import is_manager

log = logging.getLogger("madboys-bot.hub")

MENU_TIMEOUT = 14 * 60  # Discord lets us edit an interaction's reply for 15 min

# pages where the Season/Career toggle means something
CAREER_PAGES = {"player", "compare", "leaderboard", "me"}


def error_embed(msg: str) -> discord.Embed:
    return discord.Embed(description=f"⚠️ {msg}", colour=0xE67E22)


class StatsMenu(discord.ui.View):
    def __init__(self, bot: commands.Bot, user: discord.abc.User, guild_id: str,
                 roster: list[str], opponents: list[str]):
        super().__init__(timeout=MENU_TIMEOUT)
        self.bot, self.ea = bot, bot.ea
        self.user, self.guild_id = user, guild_id
        self.roster, self.opponents = roster, opponents
        self.page = "home"
        self.player: Optional[str] = None
        self.compare_with: Optional[str] = None
        self.stat = "goals"
        self.opponent: Optional[str] = None
        self.career = False
        self.embed: discord.Embed = discord.Embed()
        self.message_interaction: Optional[discord.Interaction] = None

    # ------------------------------------------------------------------ #
    @classmethod
    async def open(cls, bot: commands.Bot, interaction: discord.Interaction, page: str = "home"):
        """Send a new private menu in reply to a slash command or a panel button."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        roster = await S.roster_names(bot.ea)
        menu = cls(bot, interaction.user, str(interaction.guild_id), roster, md.opponents(CLUB_ID, limit=25))
        await menu.go(page)
        menu.message_interaction = interaction
        await interaction.followup.send(embed=menu.embed, view=menu, ephemeral=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This menu is someone else's — use `/stats` to open your own.",
                                                    ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message_interaction:
            try:
                await self.message_interaction.edit_original_response(view=None)
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ #
    #  screens
    # ------------------------------------------------------------------ #
    def home_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"📊 {CLUB_NAME} — Stats",
            description="Tap a button or pick from a dropdown below.\n*Only you can see this menu — "
                        "use 📢 Share to post something to the channel.*",
            colour=CLUB_COLOUR)
        recent = md.recent_results(CLUB_ID, 5)
        if recent:
            r = recent[0]
            embed.add_field(name="Last result",
                            value=f"{md.RESULT_EMOJI[r['result']]} {r['our_goals']}–{r['opp_goals']} vs {r['opp_name']} "
                                  f"<t:{r['ts']}:R>", inline=False)
            embed.add_field(name="Form", value=md.form_string(recent), inline=True)
        linked = get_link(self.guild_id, str(self.user.id))
        embed.add_field(name="You", value=f"linked as **{linked}**" if linked else "not linked yet — tap 👤 Me",
                        inline=True)
        return embed

    async def go(self, page: str):
        """Switch to a page and build its embed."""
        self.page = page
        screen: Optional[S.Screen] = None
        if page == "home":
            screen = self.home_embed()
        elif page == "lastgame":
            screen = await S.build_lastgame(self.ea)
        elif page == "club":
            screen = await S.build_clubstats(self.ea)
        elif page == "form":
            screen = S.build_form(10)
        elif page == "recap":
            from cogs.matchday import build_recap_embed
            screen = build_recap_embed(7) or "No tracked matches in the last 7 days."
        elif page == "me":
            name = get_link(self.guild_id, str(self.user.id))
            if not name:
                self.page = "link"
                screen = discord.Embed(
                    title="👤 Which player are you?",
                    description="Pick your EA name from the dropdown below. You only need to do this once — "
                                "after that, 👤 Me shows your stats and the bot tracks your positions.",
                    colour=CLUB_COLOUR)
            else:
                self.player = name
                screen = await S.build_player(self.ea, name, self.career, with_recent=True)
        elif page == "player":
            screen = await S.build_player(self.ea, self.player, self.career, with_recent=True)
        elif page == "compare":
            screen = await S.build_compare(self.ea, self.player, self.compare_with, self.career)
        elif page == "leaderboard":
            screen = await S.build_leaderboard(self.ea, self.stat, self.career)
        elif page == "h2h":
            screen = S.build_h2h(self.opponent)
        self.embed = error_embed(screen) if isinstance(screen, str) else screen
        self.render()

    # ------------------------------------------------------------------ #
    #  components
    # ------------------------------------------------------------------ #
    def render(self):
        self.clear_items()

        for key, label, emoji in (("lastgame", "Last game", "📡"), ("club", "Club", "📊"), ("form", "Form", "📈"),
                                  ("recap", "Recap", "🗓️"), ("me", "Me", "👤")):
            active = self.page == key or (key == "me" and self.page == "link")
            btn = discord.ui.Button(label=label, emoji=emoji, row=0,
                                    style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary)
            btn.callback = self._nav(key)
            self.add_item(btn)

        if self.roster:
            sel = discord.ui.Select(placeholder="👤 Player stats…", row=1, options=[
                discord.SelectOption(label=n[:100], value=n[:100],
                                     default=self.page in ("player", "compare") and n == self.player)
                for n in self.roster])
            sel.callback = self._pick_player
            self.add_item(sel)

        lb = discord.ui.Select(placeholder="🏆 Leaderboards…", row=2, options=[
            discord.SelectOption(label=label, value=key, emoji=S.LEADERBOARD_EMOJI.get(key),
                                 default=self.page == "leaderboard" and key == self.stat)
            for key, (label, _) in S.LEADERBOARD_STATS.items()])
        lb.callback = self._pick_stat
        self.add_item(lb)

        if self.page == "link" and self.roster:
            sel = discord.ui.Select(placeholder="Pick your EA name…", row=3, options=[
                discord.SelectOption(label=n[:100], value=n[:100]) for n in self.roster])
            sel.callback = self._pick_self
            self.add_item(sel)
        elif self.page in ("player", "compare", "me") and self.player and len(self.roster) > 1:
            others = [n for n in self.roster if n != self.player][:25]
            sel = discord.ui.Select(placeholder=f"⚔️ Compare {self.player} with…", row=3, options=[
                discord.SelectOption(label=n[:100], value=n[:100],
                                     default=self.page == "compare" and n == self.compare_with)
                for n in others])
            sel.callback = self._pick_compare
            self.add_item(sel)
        elif self.opponents:
            sel = discord.ui.Select(placeholder="🤝 Head-to-head vs…", row=3, options=[
                discord.SelectOption(label=n[:100], value=n[:100], default=self.page == "h2h" and n == self.opponent)
                for n in self.opponents])
            sel.callback = self._pick_opponent
            self.add_item(sel)

        toggle = discord.ui.Button(label="Career" if self.career else "This season",
                                   emoji="🕰️" if self.career else "📅", row=4,
                                   style=discord.ButtonStyle.success, disabled=self.page not in CAREER_PAGES)
        toggle.callback = self._toggle_career
        self.add_item(toggle)

        home = discord.ui.Button(label="Home", emoji="🏠", row=4, style=discord.ButtonStyle.secondary,
                                 disabled=self.page == "home")
        home.callback = self._nav("home")
        self.add_item(home)

        share = discord.ui.Button(label="Share", emoji="📢", row=4, style=discord.ButtonStyle.secondary,
                                  disabled=self.page in ("home", "link"))
        share.callback = self._share
        self.add_item(share)

    async def _update(self, interaction: discord.Interaction, page: str):
        await interaction.response.defer()  # EA can take a few seconds; Discord wants an answer within 3
        await self.go(page)
        self.message_interaction = interaction
        await interaction.edit_original_response(embed=self.embed, view=self)

    def _nav(self, page: str):
        async def cb(interaction: discord.Interaction):
            await self._update(interaction, page)
        return cb

    async def _pick_player(self, interaction: discord.Interaction):
        self.player = interaction.data["values"][0]
        await self._update(interaction, "player")

    async def _pick_compare(self, interaction: discord.Interaction):
        self.compare_with = interaction.data["values"][0]
        await self._update(interaction, "compare")

    async def _pick_stat(self, interaction: discord.Interaction):
        self.stat = interaction.data["values"][0]
        await self._update(interaction, "leaderboard")

    async def _pick_opponent(self, interaction: discord.Interaction):
        self.opponent = interaction.data["values"][0]
        await self._update(interaction, "h2h")

    async def _pick_self(self, interaction: discord.Interaction):
        set_link(self.guild_id, str(self.user.id), interaction.data["values"][0], linked_by="self")
        await self._update(interaction, "me")

    async def _toggle_career(self, interaction: discord.Interaction):
        self.career = not self.career
        await self._update(interaction, self.page)

    async def _share(self, interaction: discord.Interaction):
        try:
            await interaction.channel.send(content=f"📢 Shared by {interaction.user.mention}", embed=self.embed,
                                           allowed_mentions=discord.AllowedMentions.none())
            await interaction.response.send_message("Posted to the channel ✅", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I'm not allowed to post in this channel.", ephemeral=True)


class PanelView(discord.ui.View):
    """
    The pinned control panel. Persistent: fixed custom_ids and no timeout, and
    registered with bot.add_view() at startup, so buttons keep working forever.
    Each click opens a private StatsMenu for whoever clicked.
    """

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Stats", emoji="📊", style=discord.ButtonStyle.primary, custom_id="madboys:panel:stats")
    async def stats(self, interaction: discord.Interaction, _):
        await StatsMenu.open(self.bot, interaction, "home")

    @discord.ui.button(label="Last game", emoji="📡", style=discord.ButtonStyle.secondary,
                       custom_id="madboys:panel:lastgame")
    async def lastgame(self, interaction: discord.Interaction, _):
        await StatsMenu.open(self.bot, interaction, "lastgame")

    @discord.ui.button(label="My stats", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="madboys:panel:me")
    async def me(self, interaction: discord.Interaction, _):
        await StatsMenu.open(self.bot, interaction, "me")

    @discord.ui.button(label="Leaderboard", emoji="🏆", style=discord.ButtonStyle.secondary,
                       custom_id="madboys:panel:leaderboard")
    async def leaderboard(self, interaction: discord.Interaction, _):
        await StatsMenu.open(self.bot, interaction, "leaderboard")


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title=f"⚽ {CLUB_NAME} Bot",
        description="**Tap a button — no commands needed.**\n"
                    "Your menu opens privately (only you see it), and you can 📢 Share anything to the channel.\n\n"
                    "📊 **Stats** — everything: results, players, leaderboards, head-to-heads\n"
                    "📡 **Last game** — latest result with player ratings\n"
                    "👤 **My stats** — your numbers (first time: pick your EA name)\n"
                    "🏆 **Leaderboard** — who's top of the squad",
        colour=CLUB_COLOUR)


class HubCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="stats", description="Open the stats menu — buttons and dropdowns, no typing")
    async def stats(self, interaction: discord.Interaction):
        await StatsMenu.open(self.bot, interaction)

    @app_commands.command(name="panel", description="Manager: post (and pin) a button panel in this channel")
    async def panel(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            await interaction.response.send_message("Managers only.", ephemeral=True)
            return
        await interaction.response.send_message(embed=panel_embed(), view=PanelView(self.bot))
        msg = await interaction.original_response()
        try:
            await msg.pin(reason="MADBOYS bot panel")
        except discord.HTTPException:
            await interaction.followup.send("Posted! (I couldn't pin it — give me Manage Messages, or pin it yourself.)",
                                            ephemeral=True)


async def setup(bot: commands.Bot):
    bot.add_view(PanelView(bot))  # keep old panels' buttons working after restarts
    await bot.add_cog(HubCog(bot))
