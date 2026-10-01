"""
Lineups, formations and builds (single club: MADBOYS FC).

For managers, the easy way is the 🧑‍💼 Manager button on the pinned panel
(or /lineup builder): pick a formation, ✨ auto-suggest, swap anyone with two
taps, 📢 post it to the chat. The posted lineup is what tells the bot each
player's exact position (LB vs CB...) for the matches that follow.

  /builds   (alias /prefer)        - Tick the positions you have a build for
  /lineup builder                  - Manager: the button-based lineup builder
  /lineup suggest                  - Manager: auto-fill (session RSVPs, builds, rotation)
  /lineup assign <slot> <player>   - Manager: put a player in a slot
  /lineup clear [slot]             - Manager: empty one slot, or the whole lineup
  /lineup post   (alias confirm)   - Manager: post the lineup to this channel
  /formation set / show            - Set or view the formation

How suggestions work:
  - If there's a /session today, only players who clicked ✅ are used.
  - A player only goes in a slot they have a build for. In session mode,
    RSVP'd players without a matching build can still fill leftover gaps.
  - Among those, it picks the assignment that best spreads positions around:
    a slot costs more the more a player has played that exact position (and,
    less so, that role) in their last 5 games, and much more if they're on a
    streak there. It's solved optimally (Hungarian algorithm), not first-come.
"""

import json
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.rotation import broad_role, current_streak, get_all_recent, is_exact, strip_number
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
COST_SAME_POSITION = 8   # per recent game in this exact position (LB...)
COST_SAME_ROLE = 2       # per recent game in this role (DEF...)
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
    prefs:  {discord_id: [positions they have builds for]}
    recent: {discord_id: [recent positions, newest first]} (exact like 'LB', or 'DEF' if unconfirmed)
    pool:   if given (session RSVPs), only these players, and players without a
            matching build may fill gaps at a high cost.
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
        base, role = strip_number(slot), broad_role(slot)
        hist = recent.get(pid, [])
        last5 = hist[:5]
        c += COST_SAME_POSITION * sum(1 for p in last5 if is_exact(p) and strip_number(p) == base)
        c += COST_SAME_ROLE * sum(1 for p in last5 if broad_role(p) == role)
        streak_pos, run = current_streak(hist)
        if run >= ROTATION_THRESHOLD and streak_pos in (base, role):
            c += COST_STUCK
        return c

    # columns: real players, then one "leave empty" dummy per slot
    n = len(slots)
    matrix = [[cost(s, pid) for pid in players] + [COST_EMPTY] * n for s in slots]
    picks = hungarian(matrix)
    out: dict[str, Optional[str]] = {}
    for i, (s, col) in enumerate(zip(slots, picks)):
        out[s] = players[col] if col < len(players) and matrix[i][col] < COST_EMPTY else None
    return out


def auto_suggest(guild_id: str, formation: str) -> tuple[dict[str, Optional[str]], Optional[list[str]]]:
    """Suggest for the current session (or everyone with builds). -> (slots, session pool or None)"""
    from cogs.sessions import current_session_players
    pool = current_session_players(guild_id)
    result = suggest_lineup(FORMATIONS[formation], get_all_prefs(guild_id),
                            get_all_recent(guild_id, CLUB_NAME, limit_per_player=10), pool)
    clear_slots(guild_id, CLUB_NAME)   # old picks must not linger in slots we couldn't fill
    for pos, did in result.items():
        if did:
            set_slot(guild_id, CLUB_NAME, pos, did)
    return result, pool


async def lineup_message(guild: discord.Guild, formation: str, slots: dict[str, Optional[str]],
                         title: str, footer: str) -> tuple[discord.Embed, Optional[bytes]]:
    names = {pos: (await resolve_name(guild, did) if did else None) for pos, did in slots.items()}
    lines = [f"**{strip_number(pos)}**: {names[pos] or '*empty*'}" for pos in slots]
    embed = discord.Embed(title=title, description="\n".join(lines), colour=CLUB_COLOUR)
    embed.set_footer(text=footer)
    png = render_lineup(formation, names, title=f"{CLUB_NAME} • {formation}")
    if png:
        embed.set_image(url="attachment://lineup.png")
        return embed, png.read()
    return embed, None


def png_file(png: Optional[bytes]) -> list[discord.File]:
    import io
    return [discord.File(io.BytesIO(png), "lineup.png")] if png else []


async def post_lineup(channel: discord.abc.Messageable, guild: discord.Guild, formation: str,
                      slots: dict[str, Optional[str]], posted_by: str) -> None:
    """Save the lineup as the source of exact positions for the next matches, and post it with pings."""
    import positions as POS
    POS.save_plan(str(guild.id), formation, slots, posted_by)
    embed, png = await lineup_message(guild, formation, slots, f"📋 {CLUB_NAME} — Lineup ({formation})",
                                      "Positions from this lineup are logged automatically after each game")
    pings = " · ".join(f"{strip_number(pos)} <@{did}>" for pos, did in slots.items() if did)
    await channel.send(content=f"📋 **Lineup is up!**\n{pings}", embed=embed, files=png_file(png),
                       allowed_mentions=discord.AllowedMentions(users=True))


# --------------------------------------------------------------------------- #
#  UI: builds picker (players)
# --------------------------------------------------------------------------- #
class BuildsView(discord.ui.View):
    def __init__(self, guild_id: str, user: discord.abc.User):
        super().__init__(timeout=300)
        self.guild_id, self.user = guild_id, user
        current = get_prefs(guild_id, str(user.id))
        select = discord.ui.Select(
            placeholder="Tick every position you have a build for",
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
        await interaction.response.edit_message(
            content=f"✅ Builds saved: **{', '.join(chosen)}**\nLineup suggestions will only put you in these spots.",
            view=None)


def builds_prompt(guild_id: str, user_id: str) -> str:
    current = get_prefs(guild_id, user_id)
    msg = ("🛠️ **My builds** — tick every position you've got a build for (or can play well).\n"
           "The lineup suggestions only put you in these spots, and the rotation notes use them to suggest swaps.")
    if current:
        msg += f"\nCurrent: **{', '.join(current)}**"
    return msg


# --------------------------------------------------------------------------- #
#  UI: lineup builder (managers)
# --------------------------------------------------------------------------- #
class LineupBuilder(discord.ui.View):
    """
    Private manager menu. Layout:
      row 0  ▾ Formation
      row 1  ▾ Slot to change            (shows who's in each slot)
      row 2  ▾ Who plays there           (session sign-ups, or linked players)
      row 3  [✨ Auto-suggest] [🧹 Clear] [📢 Post lineup]
      row 4  [📝 Send rotation notes here]
    """

    def __init__(self, bot: commands.Bot, guild: discord.Guild, user: discord.abc.User):
        super().__init__(timeout=14 * 60)
        self.bot, self.guild, self.user = bot, guild, user
        self.gid = str(guild.id)
        self.formation = get_formation(self.gid, CLUB_NAME) or "4-3-3"
        if not get_formation(self.gid, CLUB_NAME):
            set_formation(self.gid, CLUB_NAME, self.formation)
        self.slot: Optional[str] = None
        self.note = ""
        self.embed = discord.Embed()
        self.png: Optional[bytes] = None
        self.names: dict[str, str] = {}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This builder belongs to someone else.", ephemeral=True)
            return False
        return True

    def slots(self) -> dict[str, Optional[str]]:
        return get_slots(self.gid, CLUB_NAME)

    async def candidates(self) -> list[str]:
        from cogs.link import get_all_links
        from cogs.sessions import current_session_players
        pool = current_session_players(self.gid)
        ids = list(pool) if pool is not None else list(get_all_links(self.gid))
        prefs = get_all_prefs(self.gid)
        ids += [d for d in self.slots().values() if d and d not in ids]
        for d in ids:
            if d not in self.names:
                self.names[d] = await resolve_name(self.guild, d)
        ids.sort(key=lambda d: (not prefs.get(d), self.names[d].lower()))   # people with builds first
        return ids[:24]   # + "empty" option = 25

    async def refresh(self):
        slots = self.slots()
        if self.slot not in slots:
            self.slot = next((s for s, d in slots.items() if not d), next(iter(slots), None))
        filled = sum(1 for d in slots.values() if d)
        from cogs.sessions import current_session_players
        pool = current_session_players(self.gid)
        src = f"{len(pool)} signed up for today's session" if pool is not None else "no session today — showing linked players"
        self.embed, self.png = await lineup_message(self.guild, self.formation, slots,
                                                    f"🧑‍💼 Lineup builder — {self.formation}",
                                                    f"{filled}/{len(slots)} filled • {src}")
        if self.note:
            self.embed.description = f"{self.note}\n\n{self.embed.description}"
        await self.render(slots)

    async def render(self, slots: dict[str, Optional[str]]):
        self.clear_items()
        fsel = discord.ui.Select(row=0, placeholder="Formation", options=[
            discord.SelectOption(label=f, value=f, default=f == self.formation) for f in FORMATIONS])
        fsel.callback = self._pick_formation
        self.add_item(fsel)

        ssel = discord.ui.Select(row=1, placeholder="Slot to change", options=[
            discord.SelectOption(label=f"{strip_number(s)} — {self.names.get(d, '…') if d else 'empty'}"[:100],
                                 value=s, default=s == self.slot) for s, d in slots.items()])
        ssel.callback = self._pick_slot
        self.add_item(ssel)

        prefs = get_all_prefs(self.gid)
        cands = await self.candidates()
        opts = [discord.SelectOption(label="— leave empty —", value="_empty")]
        for d in cands:
            builds = prefs.get(d, [])
            where = next((strip_number(s) for s, x in slots.items() if x == d), None)
            desc = (f"builds: {', '.join(builds)}" if builds else "no builds set") + (f" • now {where}" if where else "")
            opts.append(discord.SelectOption(label=self.names[d][:100], value=d, description=desc[:100]))
        psel = discord.ui.Select(row=2, placeholder=f"Who plays {strip_number(self.slot or '')}?", options=opts)
        psel.callback = self._pick_player
        self.add_item(psel)

        for label, emoji, style, cb in (("Auto-suggest", "✨", discord.ButtonStyle.primary, self._auto),
                                        ("Clear", "🧹", discord.ButtonStyle.secondary, self._clear),
                                        ("Post lineup", "📢", discord.ButtonStyle.success, self._post)):
            b = discord.ui.Button(label=label, emoji=emoji, style=style, row=3)
            b.callback = cb
            self.add_item(b)

        from cogs.positions import K_MANAGER_CHANNEL
        from db import get_setting
        here = get_setting(self.gid, K_MANAGER_CHANNEL)
        nb = discord.ui.Button(label="Rotation notes go here ✓" if here else "Send rotation notes here",
                               emoji="📝", style=discord.ButtonStyle.secondary, row=4)
        nb.callback = self._notes_here
        self.add_item(nb)

    async def _update(self, interaction: discord.Interaction, note: str = ""):
        self.note = note
        await self.refresh()
        await interaction.edit_original_response(embed=self.embed, view=self, attachments=png_file(self.png))

    async def _pick_formation(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.formation = interaction.data["values"][0]
        set_formation(self.gid, CLUB_NAME, self.formation)
        self.slot = None
        await self._update(interaction, f"Formation set to **{self.formation}** (slots cleared) — try ✨ Auto-suggest.")

    async def _pick_slot(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.slot = interaction.data["values"][0]
        await self._update(interaction)

    async def _pick_player(self, interaction: discord.Interaction):
        await interaction.response.defer()
        choice = interaction.data["values"][0]
        if not self.slot:
            await self._update(interaction, "Pick a slot first.")
            return
        if choice == "_empty":
            clear_slots(self.gid, CLUB_NAME, self.slot)
            await self._update(interaction, f"Emptied **{strip_number(self.slot)}**.")
            return
        slots = self.slots()
        old_slot = next((s for s, d in slots.items() if d == choice), None)
        displaced = slots.get(self.slot)
        set_slot(self.gid, CLUB_NAME, self.slot, choice)
        if old_slot and displaced and old_slot != self.slot:
            set_slot(self.gid, CLUB_NAME, old_slot, displaced)   # swap the two players
            note = f"Swapped **{self.names.get(choice)}** ↔ **{self.names.get(displaced)}**."
        else:
            note = f"**{self.names.get(choice)}** → **{strip_number(self.slot)}**."
        # jump to the next empty slot to keep filling quickly
        nxt = next((s for s, d in self.slots().items() if not d), None)
        if nxt:
            self.slot = nxt
        await self._update(interaction, note)

    async def _auto(self, interaction: discord.Interaction):
        await interaction.response.defer()
        result, pool = auto_suggest(self.gid, self.formation)
        bench = [d for d in (pool or []) if d not in result.values()]
        note = "✨ Suggested from " + ("today's sign-ups" if pool is not None else "everyone with builds set") + \
               ", spreading positions around."
        if bench:
            note += f" Bench: {', '.join([await resolve_name(self.guild, b) for b in bench])}."
        await self._update(interaction, note)

    async def _clear(self, interaction: discord.Interaction):
        await interaction.response.defer()
        clear_slots(self.gid, CLUB_NAME)
        await self._update(interaction, "🧹 Cleared.")

    async def _post(self, interaction: discord.Interaction):
        await interaction.response.defer()
        slots = self.slots()
        if not any(slots.values()):
            await self._update(interaction, "Nobody's in the lineup yet.")
            return
        try:
            await post_lineup(interaction.channel, self.guild, self.formation, slots, str(interaction.user.id))
        except discord.Forbidden:
            await self._update(interaction, "I can't post in this channel.")
            return
        await self._update(interaction, "📢 Posted! Exact positions from this lineup will be logged after each game.")

    async def _notes_here(self, interaction: discord.Interaction):
        await interaction.response.defer()
        from cogs.positions import K_MANAGER_CHANNEL
        from db import set_setting
        set_setting(self.gid, K_MANAGER_CHANNEL, str(interaction.channel_id))
        await self._update(interaction, f"📝 Rotation notes will be posted in <#{interaction.channel_id}> "
                                        f"(keep this a managers-only channel).")


async def open_builder(bot: commands.Bot, interaction: discord.Interaction):
    if not is_manager(interaction.user):
        await interaction.response.send_message("The lineup builder is for managers.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    view = LineupBuilder(bot, interaction.guild, interaction.user)
    await view.refresh()
    await interaction.followup.send(embed=view.embed, view=view, files=png_file(view.png), ephemeral=True)


async def slot_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    formation = get_formation(str(interaction.guild_id), CLUB_NAME)
    slots = FORMATIONS.get(formation, [])
    return [app_commands.Choice(name=s, value=s) for s in slots if current.upper() in s][:25]


class LineupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    formation_group = app_commands.Group(name="formation", description="Formation management")
    lineup_group = app_commands.Group(name="lineup", description="Lineup management")

    async def _send(self, interaction: discord.Interaction, embed: discord.Embed, png: Optional[bytes]):
        kwargs = {"embed": embed, "files": png_file(png)}
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
            f"Use `/lineup builder` (or the 🧑‍💼 Manager button) to fill it.")

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
        embed, png = await lineup_message(interaction.guild, formation, slots, f"⚽ {CLUB_NAME} — {formation}",
                                          f"{filled}/{len(slots)} positions filled")
        await self._send(interaction, embed, png)

    # ------------------------------------------------------------------ #
    @app_commands.command(name="builds", description="Tick the positions you have a build for")
    async def builds(self, interaction: discord.Interaction):
        await interaction.response.send_message(builds_prompt(str(interaction.guild_id), str(interaction.user.id)),
                                                view=BuildsView(str(interaction.guild_id), interaction.user),
                                                ephemeral=True)

    @app_commands.command(name="prefer", description="Same as /builds — the positions you can play")
    async def position_prefer(self, interaction: discord.Interaction):
        await self.builds.callback(self, interaction)

    # ------------------------------------------------------------------ #
    @lineup_group.command(name="builder", description="Manager: build and post the lineup with buttons")
    async def lineup_builder(self, interaction: discord.Interaction):
        await open_builder(self.bot, interaction)

    @lineup_group.command(name="suggest", description="Manager: auto-fill the lineup (session RSVPs, builds, rotation)")
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
        result, pool = auto_suggest(gid, formation)
        filled = sum(1 for v in result.values() if v)
        source = (f"{len(pool)} players who RSVP'd ✅" if pool is not None else "everyone with builds set")
        bench = [did for did in (pool or []) if did not in result.values()]
        embed, png = await lineup_message(
            interaction.guild, formation, result, f"📋 {CLUB_NAME} — Suggested Lineup ({formation})",
            f"{filled}/{len(result)} filled from {source} • rotation-aware • /lineup post to send it")
        if bench:
            bench_names = [await resolve_name(interaction.guild, b) for b in bench]
            embed.add_field(name="Bench", value=", ".join(bench_names)[:1024], inline=False)
        await self._send(interaction, embed, png)

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

    async def _post(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            await interaction.response.send_message("You need the Manager/Admin role to post a lineup.", ephemeral=True)
            return
        gid = str(interaction.guild_id)
        formation = get_formation(gid, CLUB_NAME)
        slots = get_slots(gid, CLUB_NAME) if formation else {}
        if not any(slots.values()):
            await interaction.response.send_message(
                "No players assigned yet. Use `/lineup builder` or `/lineup suggest` first.", ephemeral=True)
            return
        await interaction.response.send_message("📢 Posting the lineup…", ephemeral=True)
        await post_lineup(interaction.channel, interaction.guild, formation, slots, str(interaction.user.id))

    @lineup_group.command(name="post", description="Manager: post the lineup here (sets everyone's exact positions)")
    async def lineup_post(self, interaction: discord.Interaction):
        await self._post(interaction)

    @lineup_group.command(name="confirm", description="Same as /lineup post")
    async def lineup_confirm(self, interaction: discord.Interaction):
        await self._post(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(LineupCog(bot))
