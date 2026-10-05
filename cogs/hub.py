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
  row 4  [📅 Season ⇄ 🕰️ Career] [🏠 Home] [📢 Share] [🔎 position filter, on leaderboards]

All screens come from the same build_* functions the slash commands use
(cogs/stats.py), so the menu and the commands always agree.
"""

import asyncio
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import match_data as md
from cogs import stats as S
from cogs.link import get_link, set_link
from config import CLUB_COLOUR, CLUB_ID, CLUB_NAME
from db import get_setting, set_setting
from utils import is_manager
from interaction_tracking import TrackedView, failed

STICKY_AFTER_MESSAGES = 8      # re-post the sticky panel once it's this many messages up
STICKY_MIN_SECONDS = 45        # ...but not more often than this

log = logging.getLogger("madboys-bot.hub")

MENU_TIMEOUT = 14 * 60  # Discord lets us edit an interaction's reply for 15 min

# pages where the Season/Career toggle means something
CAREER_PAGES = {"player", "compare", "leaderboard", "me"}  # (not the squad MOTM table — see render)


def error_embed(msg: str) -> discord.Embed:
    return discord.Embed(description=f"⚠️ {msg}", colour=0xE67E22)


class StatsMenu(TrackedView):
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
        self.position = "all"   # leaderboard / passing filter: all, DEF, MID, FWD, GK
        self.opponent: Optional[str] = None
        self.career = False
        self.embed: discord.Embed = discord.Embed()
        self.png: Optional[bytes] = None   # result card, when the page has one
        self.message_interaction: Optional[discord.Interaction] = None

    # ------------------------------------------------------------------ #
    @classmethod
    async def open(cls, bot: commands.Bot, interaction: discord.Interaction, page: str = "home"):
        """Send a new private menu in reply to a slash command or a panel button."""
        from cogs.lineup import get_prefs
        if not get_link(str(interaction.guild_id), str(interaction.user.id)) or not get_prefs(str(interaction.guild_id), str(interaction.user.id)):
            interaction.extras["usage_category"] = "navigation"
            from cogs.onboarding import open_setup
            await open_setup(bot, interaction)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        roster = await S.roster_names(bot.ea)
        menu = cls(bot, interaction.user, str(interaction.guild_id), roster, md.opponents(CLUB_ID, limit=25))
        await menu.go(page)
        menu.record_result(interaction)
        menu.message_interaction = interaction
        extra = {"file": S.card_file(menu.png)} if menu.png else {}
        await interaction.followup.send(embed=menu.embed, view=menu, ephemeral=True, **extra)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            failed(interaction)
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
            screen = await S.build_lastgame(self.ea, self.bot)
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
        elif page == "leaderboard" and self.stat == "passing":
            screen = await S.build_passing(self.ea, self.position)
        elif page == "leaderboard" and self.stat == "squad_motm":
            from cogs.motm import build_table_embed
            screen = build_table_embed()
        elif page == "leaderboard":
            screen = await S.build_leaderboard(self.ea, self.stat, self.career, self.position)
        elif page == "h2h":
            screen = S.build_h2h(self.opponent)
        self.png = None
        if isinstance(screen, tuple):
            screen, self.png = screen
        self.embed = error_embed(screen) if isinstance(screen, str) else screen
        self.screen_failed = isinstance(screen, str)
        self.render()

    def record_result(self, interaction):
        if self.screen_failed:
            failed(interaction)
        if self.page in {"lastgame", "club", "form", "recap", "me", "player", "compare", "leaderboard", "h2h"}:
            detail = (self.player or "") if self.page in {"player", "me"} else ""
            if self.page == "compare":
                detail = f"{self.player} vs {self.compare_with}"
            elif self.page == "leaderboard":
                detail = f"{self.stat}, {self.position}, {'career' if self.career else 'season'}"
            elif self.page == "h2h":
                detail = self.opponent or ""
            elif self.page in {"club", "lastgame", "form", "recap"}:
                detail = {"club": "Club overview", "lastgame": "Latest match",
                          "form": "Last 10 matches", "recap": "Last 7 days"}[self.page]
            interaction.extras["usage_lookup"] = ("Stats: " + self.page, detail)

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
            for key, (label, _) in S.LEADERBOARD_STATS.items()]
            + [discord.SelectOption(label="Passing breakdown", value="passing", emoji="📋",
                                    description="Accuracy vs involvement, by player, position & team",
                                    default=self.page == "leaderboard" and self.stat == "passing")]
            + [discord.SelectOption(label="Squad MOTM awards", value="squad_motm", emoji="🗳️",
                                    default=self.page == "leaderboard" and self.stat == "squad_motm")])
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
                                   style=discord.ButtonStyle.success,
                                   disabled=self.page not in CAREER_PAGES
                                   or (self.page == "leaderboard" and self.stat in ("squad_motm", "passing")))
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

        # position filter for leaderboards (cycles All -> DEF -> MID -> FWD -> GK)
        if self.page == "leaderboard" and self.stat != "squad_motm":
            label = S.POSITION_FILTERS[self.position][0]
            pos_btn = discord.ui.Button(label=label, emoji="🔎", row=4,
                                        style=discord.ButtonStyle.primary if self.position != "all"
                                        else discord.ButtonStyle.secondary)
            pos_btn.callback = self._cycle_position
            self.add_item(pos_btn)

    async def _update(self, interaction: discord.Interaction, page: str):
        await interaction.response.defer()  # EA can take a few seconds; Discord wants an answer within 3
        await self.go(page)
        self.record_result(interaction)
        self.message_interaction = interaction
        await interaction.edit_original_response(
            embed=self.embed, view=self, attachments=[S.card_file(self.png)] if self.png else [])

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

    async def _cycle_position(self, interaction: discord.Interaction):
        order = S.POSITION_ORDER
        self.position = order[(order.index(self.position) + 1) % len(order)]
        await self._update(interaction, "leaderboard")

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
        """
        Post what you're looking at. In a channel you can't type in yourself (e.g. a
        managers-only #announcements holding the panel), it goes to the results channel
        instead, so Share can't be used to get around the channel's permissions.
        """
        target = interaction.channel
        perms = target.permissions_for(interaction.user) if hasattr(target, "permissions_for") else None
        if perms is not None and not perms.send_messages:
            tracker = self.bot.get_cog("MatchdayCog")
            cid = tracker.channel_id_for(self.guild_id) if tracker else None
            target = (self.bot.get_channel(cid) if cid else None) or None
            if target is None:
                await interaction.response.send_message(
                    "You can't post in this channel, and no results channel is set to share to.", ephemeral=True)
                return
        try:
            extra = {"file": S.card_file(self.png)} if self.png else {}
            await target.send(content=f"📢 Shared by {interaction.user.mention}", embed=self.embed,
                              allowed_mentions=discord.AllowedMentions.none(), **extra)
            where = "" if target == interaction.channel else f" in {target.mention}"
            await interaction.response.send_message(f"Posted{where} ✅", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I'm not allowed to post there.", ephemeral=True)


class PanelView(TrackedView):
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

    @discord.ui.button(label="My builds", emoji="🛠️", style=discord.ButtonStyle.secondary,
                       custom_id="madboys:panel:builds", row=1)
    async def builds(self, interaction: discord.Interaction, _):
        from cogs.lineup import BuildsView, builds_prompt
        await interaction.response.send_message(builds_prompt(str(interaction.guild_id), str(interaction.user.id)),
                                                view=BuildsView(str(interaction.guild_id), interaction.user),
                                                ephemeral=True)

    @discord.ui.button(label="Manager", emoji="🧑‍💼", style=discord.ButtonStyle.secondary,
                       custom_id="madboys:panel:manager", row=1)
    async def manager(self, interaction: discord.Interaction, _):
        from cogs.lineup import open_builder
        await open_builder(self.bot, interaction)

    @discord.ui.button(label="Setup", emoji="🔗", style=discord.ButtonStyle.secondary,
                       custom_id="madboys:panel:setup", row=1)
    async def setup_player(self, interaction: discord.Interaction, _):
        from cogs.onboarding import open_setup
        await open_setup(self.bot, interaction)


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title=f"⚽ {CLUB_NAME} Bot",
        description="**Tap a button — no commands needed.**\n"
                    "Your menu opens privately (only you see it), and you can 📢 Share anything to the channel.\n\n"
                    "📊 **Stats** — everything: results, players, leaderboards, head-to-heads\n"
                    "📡 **Last game** — latest result with player ratings\n"
                    "👤 **My stats** — your numbers (first time: pick your EA name)\n"
                    "🏆 **Leaderboard** — who's top of the squad\n"
                    "🛠️ **My builds** — tick the positions you've got builds for\n"
                    "🧑‍💼 **Manager** — build & post the lineup (managers only)",
        colour=CLUB_COLOUR)


class HubCog(commands.Cog):
    """
    /panel sticky:True keeps the panel at the bottom of a busy channel: once the
    chat has moved on a few messages, the bot posts a fresh panel and deletes the
    old one, so nobody has to scroll up (or find the pins) to use it.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._since: dict[int, int] = {}       # channel -> messages since the panel
        self._last_repost: dict[int, float] = {}
        self._lock = asyncio.Lock()

    def _sticky(self, guild_id) -> tuple[Optional[int], Optional[int]]:
        ch = get_setting(str(guild_id), "panel_channel")
        msg = get_setting(str(guild_id), "panel_message")
        return (int(ch) if ch else None), (int(msg) if msg else None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return
        ch_id, panel_id = self._sticky(message.guild.id)
        if message.channel.id != ch_id or message.id == panel_id:
            return
        self._since[ch_id] = self._since.get(ch_id, 0) + 1
        if self._since[ch_id] < STICKY_AFTER_MESSAGES or \
                time.time() - self._last_repost.get(ch_id, 0) < STICKY_MIN_SECONDS:
            return
        async with self._lock:
            if self._since.get(ch_id, 0) < STICKY_AFTER_MESSAGES:
                return  # someone else just re-posted it
            await self._repost(message.channel, message.guild.id, panel_id)

    async def _repost(self, channel: discord.abc.Messageable, guild_id: int, old_id: Optional[int]):
        try:
            new = await channel.send(embed=panel_embed(), view=PanelView(self.bot))
        except discord.HTTPException:
            return
        set_setting(str(guild_id), "panel_message", str(new.id))
        self._since[channel.id] = 0
        self._last_repost[channel.id] = time.time()
        if old_id:
            try:
                await channel.get_partial_message(old_id).delete()
            except discord.HTTPException:
                pass

    @app_commands.command(name="stats", description="Open the stats menu — buttons and dropdowns, no typing")
    async def stats(self, interaction: discord.Interaction):
        await StatsMenu.open(self.bot, interaction)

    @app_commands.command(name="panel", description="Manager: post a button panel in this channel")
    @app_commands.describe(sticky="Keep it at the bottom of the channel (re-posts itself as the chat moves on)")
    async def panel(self, interaction: discord.Interaction, sticky: bool = False):
        if not is_manager(interaction.user):
            failed(interaction)
            await interaction.response.send_message("Managers only.", ephemeral=True)
            return
        gid = str(interaction.guild_id)
        old_ch, old_msg = self._sticky(gid)
        await interaction.response.send_message(embed=panel_embed(), view=PanelView(self.bot))
        msg = await interaction.original_response()
        if sticky:
            if old_msg and old_ch == interaction.channel_id:
                try:
                    await interaction.channel.get_partial_message(old_msg).delete()
                except discord.HTTPException:
                    pass
            set_setting(gid, "panel_channel", str(interaction.channel_id))
            set_setting(gid, "panel_message", str(msg.id))
            self._since[interaction.channel_id] = 0
            await interaction.followup.send("📌 Sticky panel on — it'll keep itself at the bottom of this channel.",
                                            ephemeral=True)
            return
        if old_ch == interaction.channel_id:   # a normal panel here turns sticky mode off for this channel
            set_setting(gid, "panel_channel", None)
            set_setting(gid, "panel_message", None)
        try:
            await msg.pin(reason="MADBOYS bot panel")
        except discord.HTTPException:
            await interaction.followup.send("Posted! (I couldn't pin it — give me Manage Messages, or pin it yourself.)",
                                            ephemeral=True)


async def setup(bot: commands.Bot):
    bot.add_view(PanelView(bot))  # keep old panels' buttons working after restarts
    await bot.add_cog(HubCog(bot))
