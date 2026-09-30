"""
Lineup & formation management (single club: MADBOYS FC).

  /formation set <formation>      - Manager: set the formation (clears slots)
  /formation show                 - Current lineup, drawn on a pitch
  /prefer                         - Pick the positions you're happy to play
  /lineup suggest                 - Manager: auto-fill the lineup (see below)
  /lineup assign <slot> <player>  - Manager: put a player in a slot
  /lineup clear [slot]            - Manager: empty one slot, or the whole lineup
  /lineup confirm                 - Manager: lock it in + log to rotation history

How /lineup suggest works now:
  - If there's a /session today, only players who clicked ✅ are used.
  - Each player can only go in a slot they prefer (/prefer). In session mode,
    RSVP'd players without preferences can still fill leftover gaps.
  - Among those, it picks the assignment that best spreads roles around: a
    slot costs more for a player the more they've played that role (GK/DEF/
    MID/FWD) in their last 5 games, and much more if /rotation check would
    flag them as stuck there. It solves this optimally (Hungarian algorithm),
    rather than giving the first slot to whoever happens to be checked first.
"""

import json
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.rotation import broad_role, get_all_recent, log_lineup
from config import CLUB_COLOUR, CLUB_NAME, ROTATION_THRESHOLD
from db import connect, now_iso
from pitch import render_lineup
from utils import is_manager, resolve_name

log = logging.getLogger("madboys-bot.lineup")

FORMATIONS = {
    "4-3-3": ["GK", "RB", "CB1", "CB2", "LB", "CM1", "CM2", "CM3", "RW", "ST", "LW"],
    "4-4-2": ["GK", "RB", "CB1", "CB2", "LB", "RM", "CM1", "CM2", "LM", "ST1", "ST2"],
    "4-2-3-1": ["GK", "RB", "CB1", "CB2", "LB", "CDM1", "CDM2", "RAM", "CAM", "LAM", "ST"],
    "3-5-2": ["GK", "CB1", "CB2", "CB3", "RWB", "CM1", "CM2", "CM3", "LWB", "ST1", "ST2"],
    "5-3-2": ["GK", "RWB", "CB1", "CB2", "CB3", "LWB", "CM1", "CM2", "CM3", "ST1", "ST2"],
    "4-1-2-1-2": ["GK", "RB", "CB1", "CB2", "LB", "CDM", "CM1", "CM2", "CAM", "ST1", "ST2"],
}

POSITION_GROUPS = ["GK", "RB", "CB", "LB", "RWB", "LWB", "CDM", "CM", "CAM", "RM", "LM", "RW", "LW", "ST"]

# Slots whose base name isn't itself a preference option
SLOT_ALIASES = {"RAM": ["CAM", "RM"], "LAM": ["CAM", "LM"]}

# assignment costs
COST_PREF = 10
COST_PER_RECENT = 5
COST_STUCK = 30
COST_NO_PREF = 200      # only allowed in session mode (RSVP'd, no matching preference)
COST_EMPTY = 1000       # leaving the slot empty
COST_FORBIDDEN = 10**6


# --------------------------------------------------------------------------- #
#  DB helpers
# --------------------------------------------------------------------------- #
def get_formation(guild_id: str, club: str) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT formation FROM active_formation WHERE guild_id=? AND club=?",
                           (guild_id, club)).fetchone()
    return row["formation"] if row else None


def set_formation(guild_id: str, club: str, formation: str):
    with connect() as conn:
        conn.execute("""
            INSERT INTO active_formation (guild_id, club, formation, updated_at) VALUES (?,?,?,?)
            ON CONFLICT(guild_id, club) DO UPDATE SET formation=excluded.formation, updated_at=excluded.updated_at
        """, (guild_id, club, formation, now_iso()))
        conn.execute("DELETE FROM lineup_slots WHERE guild_id=? AND club=?", (guild_id, club))


def get_slots(guild_id: str, club: str) -> dict[str, Optional[str]]:
    formation = get_formation(guild_id, club)
    if not formation or formation not in FORMATIONS:
        return {}
    slots = {pos: None for pos in FORMATIONS[formation]}
    with connect() as conn:
        for row in conn.execute("SELECT position, discord_id FROM lineup_slots WHERE guild_id=? AND club=?",
                                (guild_id, club)):
            if row["position"] in slots:
                slots[row["position"]] = row["discord_id"]
    return slots


def set_slot(guild_id: str, club: str, position: str, discord_id: Optional[str]):
    """Put a player in a slot, removing them from any other slot first (no doubling up)."""
    with connect() as conn:
        if discord_id:
            conn.execute("DELETE FROM lineup_slots WHERE guild_id=? AND club=? AND discord_id=? AND position<>?",
                         (guild_id, club, discord_id, position))
        conn.execute("""
            INSERT INTO lineup_slots (guild_id, club, position, discord_id) VALUES (?,?,?,?)
            ON CONFLICT(guild_id, club, position) DO UPDATE SET discord_id=excluded.discord_id
        """, (guild_id, club, position, discord_id))


def clear_slots(guild_id: str, club: str, position: Optional[str] = None):
    with connect() as conn:
        if position:
            conn.execute("DELETE FROM lineup_slots WHERE guild_id=? AND club=? AND position=?", (guild_id, club, position))
        else:
            conn.execute("DELETE FROM lineup_slots WHERE guild_id=? AND club=?", (guild_id, club))


def get_prefs(guild_id: str, discord_id: str) -> list[str]:
    with connect() as conn:
        row = conn.execute("SELECT positions FROM position_prefs WHERE guild_id=? AND discord_id=?",
                           (guild_id, discord_id)).fetchone()
    return json.loads(row["positions"]) if row else []


def set_prefs(guild_id: str, discord_id: str, positions: list[str]):
    with connect() as conn:
        conn.execute("""
            INSERT INTO position_prefs (guild_id, discord_id, positions, updated_at) VALUES (?,?,?,?)
            ON CONFLICT(guild_id, discord_id) DO UPDATE SET positions=excluded.positions, updated_at=excluded.updated_at
        """, (guild_id, discord_id, json.dumps(positions), now_iso()))


def get_all_prefs(guild_id: str) -> dict[str, list[str]]:
    with connect() as conn:
        rows = conn.execute("SELECT discord_id, positions FROM position_prefs WHERE guild_id=?", (guild_id,)).fetchall()
    return {r["discord_id"]: json.loads(r["positions"]) for r in rows}


def prefs_match_slot(prefs: list[str], slot: str) -> bool:
    base = slot.rstrip("0123456789")
    return base in prefs or slot in prefs or any(a in prefs for a in SLOT_ALIASES.get(base, []))


# --------------------------------------------------------------------------- #
#  Optimal assignment
# --------------------------------------------------------------------------- #
def hungarian(cost: list[list[float]]) -> list[int]:
    """
    Min-cost assignment for an n x m matrix with n <= m.
    Returns col index assigned to each row. O(n^2 * m).
    """
    n, m = len(cost), len(cost[0])
    INF = float("inf")
    u, v = [0.0] * (n + 1), [0.0] * (m + 1)
    p, way = [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    result = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            result[p[j] - 1] = j - 1
    return result


def suggest_lineup(slots: list[str], prefs: dict[str, list[str]], recent: dict[str, list[str]],
                   pool: Optional[list[str]] = None) -> dict[str, Optional[str]]:
    """
    slots:  formation slot names
    prefs:  {discord_id: [pref groups]}
    recent: {discord_id: [recent positions, newest first]}
    pool:   if given (session RSVPs), only these players, and players without a
            matching preference may fill gaps at a high cost.
    """
    session_mode = pool is not None
    players = list(pool) if session_mode else [p for p, pr in prefs.items() if pr]
    if not players:
        return {s: None for s in slots}

    def cost(slot: str, pid: str) -> float:
        pp = prefs.get(pid, [])
        if prefs_match_slot(pp, slot):
            c = COST_PREF
        elif session_mode:
            c = COST_NO_PREF
        else:
            return COST_FORBIDDEN
        role = broad_role(slot)
        hist = [broad_role(p) for p in recent.get(pid, [])[:5]]
        c += COST_PER_RECENT * sum(1 for r in hist if r == role)
        if len(hist) >= ROTATION_THRESHOLD and all(r == role for r in hist[:ROTATION_THRESHOLD]):
            c += COST_STUCK
        return c

    # columns: real players, then one "leave empty" dummy per slot
    n = len(slots)
    matrix = [[cost(s, pid) for pid in players] + [COST_EMPTY] * n for s in slots]
    picks = hungarian(matrix)
    out: dict[str, Optional[str]] = {}
    for s, col in zip(slots, picks):
        if col < len(players) and matrix[slots.index(s)][col] < COST_EMPTY:
            out[s] = players[col]
        else:
            out[s] = None
    return out


# --------------------------------------------------------------------------- #
#  UI
# --------------------------------------------------------------------------- #
class PositionPrefView(discord.ui.View):
    def __init__(self, guild_id: str, user: discord.abc.User):
        super().__init__(timeout=180)
        self.guild_id, self.user = guild_id, user
        current = get_prefs(guild_id, str(user.id))
        select = discord.ui.Select(
            placeholder="Pick every position you're happy to play",
            min_values=1, max_values=len(POSITION_GROUPS),
            options=[discord.SelectOption(label=p, value=p, default=p in current) for p in POSITION_GROUPS],
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This menu isn't for you!", ephemeral=True)
            return
        chosen = interaction.data["values"]
        set_prefs(self.guild_id, str(self.user.id), chosen)
        await interaction.response.edit_message(content=f"✅ Preferences saved: **{', '.join(chosen)}**", view=None)


async def slot_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    formation = get_formation(str(interaction.guild_id), CLUB_NAME)
    slots = FORMATIONS.get(formation, [])
    return [app_commands.Choice(name=s, value=s) for s in slots if current.upper() in s][:25]


class LineupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    formation_group = app_commands.Group(name="formation", description="Formation management")
    lineup_group = app_commands.Group(name="lineup", description="Lineup management")

    async def _names(self, guild: discord.Guild, slots: dict[str, Optional[str]]) -> dict[str, Optional[str]]:
        return {pos: (await resolve_name(guild, did) if did else None) for pos, did in slots.items()}

    async def _lineup_message(self, guild: discord.Guild, formation: str, slots: dict[str, Optional[str]],
                              title: str, footer: str) -> tuple[discord.Embed, Optional[discord.File]]:
        names = await self._names(guild, slots)
        lines = [f"**{pos.rstrip('0123456789')}**: {names[pos] or '*empty*'}" for pos in slots]
        embed = discord.Embed(title=title, description="\n".join(lines), colour=CLUB_COLOUR)
        embed.set_footer(text=footer)
        png = render_lineup(formation, names, title=f"{CLUB_NAME} • {formation}")
        file = None
        if png:
            file = discord.File(png, "lineup.png")
            embed.set_image(url="attachment://lineup.png")
        return embed, file

    async def _send(self, interaction: discord.Interaction, embed: discord.Embed, file: Optional[discord.File]):
        kwargs = {"embed": embed}
        if file:
            kwargs["file"] = file
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    # ------------------------------------------------------------------ #
    @formation_group.command(name="set", description="Set the active formation (manager only)")
    @app_commands.choices(formation=[app_commands.Choice(name=f, value=f) for f in FORMATIONS])
    async def formation_set(self, interaction: discord.Interaction, formation: app_commands.Choice[str]):
        if not is_manager(interaction.user):
            await interaction.response.send_message("You need the Manager/Admin role to set the formation.", ephemeral=True)
            return
        set_formation(str(interaction.guild_id), CLUB_NAME, formation.value)
        await interaction.response.send_message(
            f"✅ **{CLUB_NAME}** formation set to **{formation.value}** ({', '.join(FORMATIONS[formation.value])}).\n"
            f"Use `/lineup suggest` to auto-fill, or `/lineup assign` to slot players.")

    @formation_group.command(name="show", description="Show the current formation and lineup")
    async def formation_show(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        formation = get_formation(gid, CLUB_NAME)
        if not formation:
            await interaction.response.send_message("No formation set yet. Use `/formation set` first.", ephemeral=True)
            return
        await interaction.response.defer()
        slots = get_slots(gid, CLUB_NAME)
        filled = sum(1 for v in slots.values() if v)
        embed, file = await self._lineup_message(interaction.guild, formation, slots, f"⚽ {CLUB_NAME} — {formation}",
                                                 f"{filled}/{len(slots)} positions filled")
        await self._send(interaction, embed, file)

    # ------------------------------------------------------------------ #
    @app_commands.command(name="prefer", description="Set your preferred positions")
    async def position_prefer(self, interaction: discord.Interaction):
        current = get_prefs(str(interaction.guild_id), str(interaction.user.id))
        msg = "**Pick your preferred positions** — select everything you're happy playing."
        if current:
            msg += f"\nCurrent: **{', '.join(current)}**"
        await interaction.response.send_message(msg, view=PositionPrefView(str(interaction.guild_id), interaction.user),
                                                ephemeral=True)

    # ------------------------------------------------------------------ #
    @lineup_group.command(name="suggest", description="Manager: auto-fill the lineup (session RSVPs, prefs, rotation)")
    async def lineup_suggest(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            await interaction.response.send_message("You need the Manager/Admin role for this.", ephemeral=True)
            return
        gid = str(interaction.guild_id)
        formation = get_formation(gid, CLUB_NAME)
        if not formation:
            await interaction.response.send_message("No formation set. Use `/formation set` first.", ephemeral=True)
            return
        await interaction.response.defer()

        from cogs.sessions import current_session_players
        pool = current_session_players(gid)  # None if no session around now
        prefs = get_all_prefs(gid)
        recent = get_all_recent(gid, CLUB_NAME, limit_per_player=5)
        result = suggest_lineup(FORMATIONS[formation], prefs, recent, pool)

        clear_slots(gid, CLUB_NAME)  # old picks must not linger in slots we couldn't fill
        for pos, did in result.items():
            if did:
                set_slot(gid, CLUB_NAME, pos, did)

        filled = sum(1 for v in result.values() if v)
        source = (f"{len(pool)} players who RSVP'd ✅" if pool is not None else "everyone with /prefer set")
        bench = [did for did in (pool or []) if did not in result.values()]
        embed, file = await self._lineup_message(
            interaction.guild, formation, result, f"📋 {CLUB_NAME} — Suggested Lineup ({formation})",
            f"{filled}/{len(result)} filled from {source} • rotation-aware • adjust with /lineup assign")
        if bench:
            bench_names = [await resolve_name(interaction.guild, b) for b in bench]
            embed.add_field(name="Bench", value=", ".join(bench_names)[:1024], inline=False)
        await self._send(interaction, embed, file)

    @lineup_group.command(name="assign", description="Manager: put a player in a slot")
    @app_commands.describe(position="Slot to fill", player="Who plays there")
    @app_commands.autocomplete(position=slot_autocomplete)
    async def lineup_assign(self, interaction: discord.Interaction, position: str, player: discord.Member):
        if not is_manager(interaction.user):
            await interaction.response.send_message("You need the Manager/Admin role for this.", ephemeral=True)
            return
        gid = str(interaction.guild_id)
        formation = get_formation(gid, CLUB_NAME)
        if not formation:
            await interaction.response.send_message("No formation set. Use `/formation set` first.", ephemeral=True)
            return
        position = position.upper()
        if position not in FORMATIONS[formation]:
            await interaction.response.send_message(
                f"**{position}** isn't a slot in {formation}. Slots: {', '.join(FORMATIONS[formation])}", ephemeral=True)
            return
        if player.bot:
            await interaction.response.send_message("Bots don't play Pro Clubs 🙂", ephemeral=True)
            return
        set_slot(gid, CLUB_NAME, position, str(player.id))
        await interaction.response.send_message(f"✅ **{player.display_name}** → **{position}**")

    @lineup_group.command(name="clear", description="Manager: empty one slot, or the whole lineup")
    @app_commands.describe(position="Leave blank to clear everything")
    @app_commands.autocomplete(position=slot_autocomplete)
    async def lineup_clear(self, interaction: discord.Interaction, position: Optional[str] = None):
        if not is_manager(interaction.user):
            await interaction.response.send_message("You need the Manager/Admin role for this.", ephemeral=True)
            return
        clear_slots(str(interaction.guild_id), CLUB_NAME, position.upper() if position else None)
        await interaction.response.send_message(f"🧹 Cleared {position.upper() if position else 'the lineup'}.")

    @lineup_group.command(name="confirm", description="Manager: confirm the lineup and log it to rotation history")
    async def lineup_confirm(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            await interaction.response.send_message("You need the Manager/Admin role to confirm a lineup.", ephemeral=True)
            return
        gid = str(interaction.guild_id)
        formation = get_formation(gid, CLUB_NAME)
        slots = get_slots(gid, CLUB_NAME) if formation else {}
        if not any(slots.values()):
            await interaction.response.send_message(
                "No players assigned yet. Use `/lineup suggest` or `/lineup assign` first.", ephemeral=True)
            return
        await interaction.response.defer()
        log_lineup(gid, CLUB_NAME, slots)
        filled = sum(1 for v in slots.values() if v)
        embed, file = await self._lineup_message(interaction.guild, formation, slots,
                                                 f"✅ {CLUB_NAME} Lineup Confirmed — {formation}",
                                                 f"Logged {filled} players to rotation history")
        await self._send(interaction, embed, file)


async def setup(bot: commands.Bot):
    await bot.add_cog(LineupCog(bot))
