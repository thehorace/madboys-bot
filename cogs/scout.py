"""
Scout any other club.

  /scout <club>        - Season record, form of their squad and our head-to-head for a club you've
                         played (autocomplete) or any EA club ID.
  🔍 Scout button      - on every automatic result post; opens that opponent's report privately.

Works from the opponent's club ID (EA sends it with every match), so it uses the same
relay endpoints as /clubstats. Searching a club by name that you've never played needs a
name-search route on the relay — type its numeric EA club ID instead.
"""
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from clubs import club_id, club_name, club_scope, club_scoped, club_for, monitored_clubs
from cogs.stats import send_screen
from config import CLUB_COLOUR
from db import connect
from interaction_tracking import failed
from utils import clip, to_int

log = logging.getLogger("madboys-bot.scout")


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def opponent_rows(current: str = "", limit: int = 25) -> list[dict]:
    """Clubs we've played, newest first: [{id, name, games}]."""
    ids = [c["club_id"] for c in monitored_clubs()]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT opp_id, opp_name, MAX(ts) AS last, COUNT(*) AS games FROM matches "
            f"WHERE club_id IN ({marks}) AND opp_id IS NOT NULL AND opp_id<>'' "
            f"AND opp_name LIKE ? ESCAPE '\\' GROUP BY opp_id ORDER BY last DESC LIMIT ?",
            [*ids, _like(current), limit]).fetchall()
    return [{"id": r["opp_id"], "name": r["opp_name"] or f"Club {r['opp_id']}", "games": r["games"]} for r in rows]


def resolve_club(value: str) -> tuple[Optional[int], Optional[str]]:
    """Autocomplete value (an id), a typed numeric id, or an exact opponent name -> (id, name hint)."""
    value = (value or "").strip()
    if value.isdigit() and int(value) > 0:
        known = next((o for o in opponent_rows("", 500) if o["id"] == value), None)
        return int(value), known["name"] if known else None
    exact = next((o for o in opponent_rows(value, 500) if o["name"].casefold() == value.casefold()), None)
    return (int(exact["id"]), exact["name"]) if exact and str(exact["id"]).isdigit() else (None, None)


def head_to_head_by_id(our_id: int, opp_id: int) -> Optional[str]:
    with connect() as conn:
        rows = conn.execute("SELECT result FROM matches WHERE club_id=? AND opp_id=? ORDER BY ts DESC",
                            (our_id, str(opp_id))).fetchall()
    if not rows:
        return None
    res = [r["result"] for r in rows]
    return (f"W{res.count('W')} D{res.count('D')} L{res.count('L')} in {len(res)} "
            f"· last meeting {'🟢' if res[0] == 'W' else '🟡' if res[0] == 'D' else '🔴'}")


async def build_scout(ea, opp_id: int, our_id: Optional[int] = None, name_hint: Optional[str] = None):
    """Embed for another club, or an error string."""
    stats = await ea.get_overall_stats(opp_id)
    members = await ea.get_member_stats(opp_id)
    if not stats and not members:
        return (f"Couldn't fetch stats for club **{opp_id}** — check the ID, or EA/the relay may be down. "
                "Club stats can also be hidden by EA for inactive clubs.")
    name = name_hint
    if not name:
        info = await ea.get_club_info(opp_id)
        name = (info or {}).get("name") or f"Club {opp_id}"

    embed = discord.Embed(title=f"🔍 Scouting report — {clip(name, 200)}", colour=CLUB_COLOUR,
                          description=f"EA club ID `{opp_id}`")
    g = (stats or {}).get
    if stats:
        embed.add_field(name="Record", value=f"W{g('wins', '?')} D{g('ties', '?')} L{g('losses', '?')}", inline=True)
        embed.add_field(name="Games Played", value=str(g("gamesPlayed", "?")), inline=True)
        embed.add_field(name="Goals", value=f"{g('goals', '?')} scored / {g('goalsAgainst', '?')} conceded", inline=True)
        for label, key in (("Skill Rating", "skillRating"), ("Best Division", "bestDivision"),
                           ("Win Streak", "wstreak"), ("Unbeaten Streak", "unbeatenstreak")):
            if g(key) not in (None, ""):
                embed.add_field(name=label, value=str(g(key)), inline=True)

    players = [m for m in (members or []) if m.get("name")]
    players.sort(key=lambda m: -(to_int(m.get("goals")) + to_int(m.get("assists"))))
    if players:
        lines = [f"**{clip(m['name'], 40)}** — {to_int(m.get('goals'))}G {to_int(m.get('assists'))}A "
                 f"in {to_int(m.get('gamesPlayed'))} • ⭐{m.get('ratingAve', '-')}" for m in players[:5]]
        embed.add_field(name="Danger men (this season, G+A)", value=clip("\n".join(lines)), inline=False)

    h2h = head_to_head_by_id(our_id, opp_id) if our_id else None
    if h2h:
        with club_scope(our_id):
            embed.add_field(name=f"{club_name()} vs them", value=h2h, inline=False)

    footer = "Scouting report • EA FC Pro Clubs"
    note = ea.stale_note()
    embed.set_footer(text=f"{footer} • {note}" if note else footer)
    return embed


class ScoutButton(discord.ui.DynamicItem[discord.ui.Button], template=r"scout:(?P<opp>\d+):(?P<ours>\d+)"):
    """Persistent: the ids live in the custom_id, so it keeps working after restarts/redeploys."""

    def __init__(self, opp_id: int, our_id: int, label: str = "Scout opponent"):
        super().__init__(discord.ui.Button(label=clip(label, 80), emoji="🔍", style=discord.ButtonStyle.secondary,
                                           custom_id=f"scout:{int(opp_id)}:{int(our_id)}"))
        self.opp_id, self.our_id = int(opp_id), int(our_id)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["opp"]), int(match["ours"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        name = next((o["name"] for o in opponent_rows("", 500) if o["id"] == str(self.opp_id)), None)
        screen = await build_scout(interaction.client.ea, self.opp_id, self.our_id, name)
        if isinstance(screen, str):
            failed(interaction)
            await interaction.followup.send(screen, ephemeral=True)
        else:
            await interaction.followup.send(embed=screen, ephemeral=True)


def scout_view(opp_id, opp_name: str, our_id: int) -> Optional[discord.ui.View]:
    """The 🔍 button for a result post, or None if EA didn't give us an opponent id."""
    if not str(opp_id or "").isdigit():
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(ScoutButton(int(opp_id), our_id, f"Scout {opp_name}"))
    return view


async def scout_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=f"{o['name']} ({o['games']} game{'s' if o['games'] != 1 else ''})"[:100],
                                value=str(o["id"])) for o in opponent_rows(current)]


class ScoutCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_dynamic_items(ScoutButton)

    def cog_unload(self):
        self.bot.remove_dynamic_items(ScoutButton)

    @app_commands.command(name="scout", description="Stats for another club — an opponent you've played, or any EA club ID")
    @app_commands.describe(club="Pick a club you've played, or type an EA club ID")
    @app_commands.autocomplete(club=scout_autocomplete)
    @club_scoped
    async def scout(self, interaction: discord.Interaction, club: str):
        await interaction.response.defer()
        opp_id, name = resolve_club(club)
        if opp_id is None:
            await send_screen(interaction, f"I don't know a club called **{clip(club, 60)}**. Pick one from the "
                                           "list (clubs you've played), or type its numeric EA club ID.")
            return
        await send_screen(interaction, await build_scout(self.bot.ea, opp_id, club_id(), name))


async def setup(bot: commands.Bot):
    await bot.add_cog(ScoutCog(bot))
