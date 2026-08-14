"""
Lineup & Formation management cog.

Commands:
  /formation set <formation>   - Manager sets the active formation (e.g. 4-3-3)
  /formation show              - Shows current formation and who's in each slot
  /position prefer             - Player picks their preferred positions via select menu
  /lineup suggest              - Auto-fills formation based on player preferences
  /lineup confirm              - Manager confirms the lineup (locks it in + logs it)
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("madboys-bot.lineup")

DB_PATH = os.getenv("DB_PATH", "madboys.db")

# Available formations and their position slots
FORMATIONS = {
    "4-3-3": ["GK", "RB", "CB1", "CB2", "LB", "CM1", "CM2", "CM3", "RW", "ST", "LW"],
    "4-4-2": ["GK", "RB", "CB1", "CB2", "LB", "RM", "CM1", "CM2", "LM", "ST1", "ST2"],
    "4-2-3-1": ["GK", "RB", "CB1", "CB2", "LB", "CDM1", "CDM2", "RAM", "CAM", "LAM", "ST"],
    "3-5-2": ["GK", "CB1", "CB2", "CB3", "RWB", "CM1", "CM2", "CM3", "LWB", "ST1", "ST2"],
    "5-3-2": ["GK", "RWB", "CB1", "CB2", "CB3", "LWB", "CM1", "CM2", "CM3", "ST1", "ST2"],
    "4-1-2-1-2": ["GK", "RB", "CB1", "CB2", "LB", "CDM", "CM1", "CM2", "CAM", "ST1", "ST2"],
}

# Friendly display names for positions
POSITION_LABELS = {
    "GK": "🧤 GK", "RB": "🔵 RB", "CB1": "🔵 CB", "CB2": "🔵 CB",
    "CB3": "🔵 CB", "LB": "🔵 LB", "RWB": "🔵 RWB", "LWB": "🔵 LWB",
    "CDM": "🟡 CDM", "CDM1": "🟡 CDM", "CDM2": "🟡 CDM",
    "CM1": "🟢 CM", "CM2": "🟢 CM", "CM3": "🟢 CM",
    "RAM": "🟠 RAM", "CAM": "🟠 CAM", "LAM": "🟠 LAM",
    "RM": "🟠 RM", "LM": "🟠 LM",
    "RW": "🔴 RW", "LW": "🔴 LW",
    "ST": "🔴 ST", "ST1": "🔴 ST", "ST2": "🔴 ST",
}

# Broad position groups players can set preferences for
POSITION_GROUPS = [
    "GK", "RB", "CB", "LB", "RWB", "LWB",
    "CDM", "CM", "CAM", "RM", "LM",
    "RW", "LW", "ST",
]


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS active_formation (
                guild_id TEXT PRIMARY KEY,
                club     TEXT NOT NULL,
                formation TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lineup_slots (
                guild_id   TEXT NOT NULL,
                club       TEXT NOT NULL,
                position   TEXT NOT NULL,
                discord_id TEXT,
                PRIMARY KEY (guild_id, club, position)
            );

            CREATE TABLE IF NOT EXISTS position_prefs (
                guild_id   TEXT NOT NULL,
                discord_id TEXT NOT NULL,
                positions  TEXT NOT NULL,  -- JSON list of preferred position groups
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, discord_id)
            );
        """)


def get_formation(guild_id: str, club: str) -> Optional[str]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT formation FROM active_formation WHERE guild_id=? AND club=?",
            (guild_id, club)
        ).fetchone()
        return row["formation"] if row else None


def set_formation(guild_id: str, club: str, formation: str):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO active_formation (guild_id, club, formation, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, club) DO UPDATE SET formation=excluded.formation, updated_at=excluded.updated_at
        """, (guild_id, club, formation, now))
        # Clear old slots when formation changes
        conn.execute("DELETE FROM lineup_slots WHERE guild_id=? AND club=?", (guild_id, club))


def get_slots(guild_id: str, club: str) -> dict[str, Optional[str]]:
    formation = get_formation(guild_id, club)
    if not formation:
        return {}
    slots = {pos: None for pos in FORMATIONS[formation]}
    with get_db() as conn:
        rows = conn.execute(
            "SELECT position, discord_id FROM lineup_slots WHERE guild_id=? AND club=?",
            (guild_id, club)
        ).fetchall()
        for row in rows:
            slots[row["position"]] = row["discord_id"]
    return slots


def set_slot(guild_id: str, club: str, position: str, discord_id: str):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO lineup_slots (guild_id, club, position, discord_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, club, position) DO UPDATE SET discord_id=excluded.discord_id
        """, (guild_id, club, position, discord_id))


def get_prefs(guild_id: str, discord_id: str) -> list[str]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT positions FROM position_prefs WHERE guild_id=? AND discord_id=?",
            (guild_id, discord_id)
        ).fetchone()
        return json.loads(row["positions"]) if row else []


def set_prefs(guild_id: str, discord_id: str, positions: list[str]):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO position_prefs (guild_id, discord_id, positions, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, discord_id) DO UPDATE SET positions=excluded.positions, updated_at=excluded.updated_at
        """, (guild_id, discord_id, json.dumps(positions), now))


def get_all_prefs(guild_id: str) -> dict[str, list[str]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT discord_id, positions FROM position_prefs WHERE guild_id=?",
            (guild_id,)
        ).fetchall()
        return {row["discord_id"]: json.loads(row["positions"]) for row in rows}


# Maps broad pref groups to specific formation slots
def prefs_match_slot(prefs: list[str], slot: str) -> bool:
    slot_base = slot.rstrip("123")  # CB1 -> CB, ST1 -> ST, etc.
    return slot_base in prefs or slot in prefs


class PositionPrefView(discord.ui.View):
    """Select menu for players to choose their preferred positions."""

    def __init__(self, guild_id: str, user: discord.Member):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.user = user
        current = get_prefs(guild_id, str(user.id))

        options = [
            discord.SelectOption(label=pos, value=pos, default=(pos in current))
            for pos in POSITION_GROUPS
        ]

        select = discord.ui.Select(
            placeholder="Pick your preferred positions (choose all that apply)",
            min_values=1,
            max_values=len(POSITION_GROUPS),
            options=options,
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This menu isn't for you!", ephemeral=True)
            return

        chosen = interaction.data["values"]
        set_prefs(self.guild_id, str(self.user.id), chosen)
        await interaction.response.edit_message(
            content=f"✅ Preferences saved: **{', '.join(chosen)}**",
            view=None,
        )


class AssignSlotView(discord.ui.View):
    """Let manager assign a player to a specific position slot."""

    def __init__(self, guild_id: str, club: str, position: str, members: list[discord.Member]):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.club = club
        self.position = position

        options = [
            discord.SelectOption(label=m.display_name, value=str(m.id))
            for m in members[:25]  # Discord select limit
        ]

        select = discord.ui.Select(
            placeholder=f"Assign player to {position}",
            options=options,
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        player_id = interaction.data["values"][0]
        set_slot(self.guild_id, self.club, self.position, player_id)
        member = interaction.guild.get_member(int(player_id))
        name = member.display_name if member else player_id
        await interaction.response.edit_message(
            content=f"✅ **{name}** assigned to **{self.position}**",
            view=None,
        )


class LineupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()

    formation_group = app_commands.Group(name="formation", description="Formation management")
    lineup_group = app_commands.Group(name="lineup", description="Lineup management")

    def club_choices(self):
        return [
            app_commands.Choice(name="MADBOYS", value="MADBOYS"),
            app_commands.Choice(name="GRASBOYS", value="GRASBOYS"),
        ]

    # ------------------------------------------------------------------ #
    #  /formation set
    # ------------------------------------------------------------------ #
    @formation_group.command(name="set", description="Set the active formation (manager only)")
    @app_commands.describe(club="Which club", formation="Formation to use")
    @app_commands.choices(
        club=[
            app_commands.Choice(name="MADBOYS", value="MADBOYS"),
            app_commands.Choice(name="GRASBOYS", value="GRASBOYS"),
        ],
        formation=[app_commands.Choice(name=f, value=f) for f in FORMATIONS],
    )
    async def formation_set(
        self,
        interaction: discord.Interaction,
        club: app_commands.Choice[str],
        formation: app_commands.Choice[str],
    ):
        # Basic permission check — must have Manage Channels or a "Manager" role
        if not interaction.user.guild_permissions.manage_channels:
            if not any(r.name.lower() in ("manager", "admin", "coach") for r in interaction.user.roles):
                await interaction.response.send_message(
                    "You need the Manager/Admin role to set the formation.", ephemeral=True
                )
                return

        set_formation(str(interaction.guild_id), club.value, formation.value)
        slots = FORMATIONS[formation.value]
        await interaction.response.send_message(
            f"✅ **{club.value}** formation set to **{formation.value}**\n"
            f"Positions: {', '.join(slots)}\n\n"
            f"Use `/lineup suggest` to auto-fill from preferences, or `/lineup assign` to manually slot players."
        )

    # ------------------------------------------------------------------ #
    #  /formation show
    # ------------------------------------------------------------------ #
    @formation_group.command(name="show", description="Show the current formation and lineup")
    @app_commands.describe(club="Which club")
    @app_commands.choices(club=[
        app_commands.Choice(name="MADBOYS", value="MADBOYS"),
        app_commands.Choice(name="GRASBOYS", value="GRASBOYS"),
    ])
    async def formation_show(self, interaction: discord.Interaction, club: app_commands.Choice[str]):
        guild_id = str(interaction.guild_id)
        formation = get_formation(guild_id, club.value)
        if not formation:
            await interaction.response.send_message(
                f"No formation set for {club.value} yet. Use `/formation set` first.", ephemeral=True
            )
            return

        slots = get_slots(guild_id, club.value)
        lines = []
        for pos, discord_id in slots.items():
            label = POSITION_LABELS.get(pos, pos)
            if discord_id:
                member = interaction.guild.get_member(int(discord_id))
                name = member.display_name if member else f"<{discord_id}>"
            else:
                name = "*empty*"
            lines.append(f"{label}: {name}")

        embed = discord.Embed(
            title=f"⚽ {club.value} — {formation}",
            description="\n".join(lines),
            colour=0x1E90FF if club.value == "MADBOYS" else 0x2ECC71,
        )
        filled = sum(1 for v in slots.values() if v)
        embed.set_footer(text=f"{filled}/{len(slots)} positions filled")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    #  /position prefer
    # ------------------------------------------------------------------ #
    @app_commands.command(name="prefer", description="Set your preferred positions")
    async def position_prefer(self, interaction: discord.Interaction):
        view = PositionPrefView(str(interaction.guild_id), interaction.user)
        current = get_prefs(str(interaction.guild_id), str(interaction.user.id))
        msg = "**Pick your preferred positions** — select everything you're happy playing."
        if current:
            msg += f"\nCurrent preferences: **{', '.join(current)}**"
        await interaction.response.send_message(msg, view=view, ephemeral=True)

    # ------------------------------------------------------------------ #
    #  /lineup suggest
    # ------------------------------------------------------------------ #
    @lineup_group.command(name="suggest", description="Auto-fill lineup based on player preferences")
    @app_commands.describe(club="Which club")
    @app_commands.choices(club=[
        app_commands.Choice(name="MADBOYS", value="MADBOYS"),
        app_commands.Choice(name="GRASBOYS", value="GRASBOYS"),
    ])
    async def lineup_suggest(self, interaction: discord.Interaction, club: app_commands.Choice[str]):
        guild_id = str(interaction.guild_id)
        formation = get_formation(guild_id, club.value)
        if not formation:
            await interaction.response.send_message(
                f"No formation set for {club.value}. Use `/formation set` first.", ephemeral=True
            )
            return

        all_prefs = get_all_prefs(guild_id)
        slots = {pos: None for pos in FORMATIONS[formation]}
        assigned = set()

        # Simple greedy assign: for each slot, find first unassigned player who prefers it
        for pos in slots:
            for discord_id, prefs in all_prefs.items():
                if discord_id not in assigned and prefs_match_slot(prefs, pos):
                    slots[pos] = discord_id
                    assigned.add(discord_id)
                    break

        # Save suggestions to DB
        for pos, discord_id in slots.items():
            if discord_id:
                set_slot(guild_id, club.value, pos, discord_id)

        lines = []
        for pos, discord_id in slots.items():
            label = POSITION_LABELS.get(pos, pos)
            if discord_id:
                member = interaction.guild.get_member(int(discord_id))
                name = member.display_name if member else f"<{discord_id}>"
            else:
                name = "*no preference match — assign manually*"
            lines.append(f"{label}: {name}")

        embed = discord.Embed(
            title=f"📋 {club.value} — Suggested Lineup ({formation})",
            description="\n".join(lines),
            colour=0x1E90FF if club.value == "MADBOYS" else 0x2ECC71,
        )
        filled = sum(1 for v in slots.values() if v)
        embed.set_footer(text=f"{filled}/{len(slots)} filled from preferences • Use /lineup assign to adjust")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    #  /lineup confirm
    # ------------------------------------------------------------------ #
    @lineup_group.command(name="confirm", description="Confirm and log the current lineup to rotation history")
    @app_commands.describe(club="Which club")
    @app_commands.choices(club=[
        app_commands.Choice(name="MADBOYS", value="MADBOYS"),
        app_commands.Choice(name="GRASBOYS", value="GRASBOYS"),
    ])
    async def lineup_confirm(self, interaction: discord.Interaction, club: app_commands.Choice[str]):
        if not interaction.user.guild_permissions.manage_channels:
            if not any(r.name.lower() in ("manager", "admin", "coach") for r in interaction.user.roles):
                await interaction.response.send_message(
                    "You need the Manager/Admin role to confirm a lineup.", ephemeral=True
                )
                return

        guild_id = str(interaction.guild_id)
        formation = get_formation(guild_id, club.value)
        if not formation:
            await interaction.response.send_message(
                f"No formation set for {club.value}.", ephemeral=True
            )
            return

        slots = get_slots(guild_id, club.value)
        filled = {pos: did for pos, did in slots.items() if did}
        if not filled:
            await interaction.response.send_message(
                "No players assigned yet. Use `/lineup suggest` or `/lineup assign` first.", ephemeral=True
            )
            return

        # Log to rotation history via rotation module
        from cogs.rotation import log_lineup
        log_lineup(guild_id, club.value, slots)

        lines = []
        for pos, discord_id in filled.items():
            member = interaction.guild.get_member(int(discord_id))
            name = member.display_name if member else f"<{discord_id}>"
            lines.append(f"**{pos}**: {name}")

        embed = discord.Embed(
            title=f"✅ {club.value} Lineup Confirmed — {formation}",
            description="\n".join(lines),
            colour=0x1E90FF if club.value == "MADBOYS" else 0x2ECC71,
        )
        embed.set_footer(text=f"Logged {len(filled)} players • {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')}")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    #  /lineup assign
    # ------------------------------------------------------------------ #
    @lineup_group.command(name="assign", description="Manually assign a player to a position")
    @app_commands.describe(club="Which club", position="Position slot to fill")
    @app_commands.choices(club=[
        app_commands.Choice(name="MADBOYS", value="MADBOYS"),
        app_commands.Choice(name="GRASBOYS", value="GRASBOYS"),
    ])
    async def lineup_assign(
        self,
        interaction: discord.Interaction,
        club: app_commands.Choice[str],
        position: str,
    ):
        guild_id = str(interaction.guild_id)
        formation = get_formation(guild_id, club.value)
        if not formation:
            await interaction.response.send_message(
                f"No formation set for {club.value}. Use `/formation set` first.", ephemeral=True
            )
            return

        valid_slots = FORMATIONS[formation]
        position = position.upper()
        if position not in valid_slots:
            await interaction.response.send_message(
                f"**{position}** isn't a slot in {formation}. Valid slots: {', '.join(valid_slots)}",
                ephemeral=True,
            )
            return

        members = [m for m in interaction.guild.members if not m.bot]
        view = AssignSlotView(guild_id, club.value, position, members)
        await interaction.response.send_message(
            f"Assigning player to **{position}** in {club.value} ({formation}):",
            view=view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(LineupCog(bot))
