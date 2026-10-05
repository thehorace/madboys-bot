"""
Stats commands.

Every screen is built by a `build_*` function that returns either an Embed or
an error string. The slash commands below and the button/dropdown menu in
cogs/hub.py both call the same builders, so they always show the same thing.

  /lastgame                  - Latest result (league OR playoff) with full ratings table
  /clubstats                 - Season record, division, form
  /playerstats [player]      - A player's season or career stats (defaults to you, if linked)
  /me                        - Your own stats + your last few games
  /leaderboard <stat>        - Club ranking for a stat
  /compare <a> <b>           - Two players side by side
  /form [games]              - Recent results from the bot's match history
  /h2h <opponent>            - Record against a specific club
  /status                    - Bot + EA relay health
  /debug <what>              - Manager: dump raw EA JSON (to spot FC 27 field changes)
"""

import asyncio
import io
import json
import logging
import time
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

import match_data as md
from cogs.link import get_link
from config import CLUB_COLOUR, CLUB_ID, CLUB_NAME
from db import connect
from utils import clip, is_manager, to_float, to_int
from interaction_tracking import failed

log = logging.getLogger("madboys-bot.stats")

# What a builder returns: an embed, an error message, or an embed plus a PNG
# (the result card) that the embed shows as attachment://result.png
Screen = Union[discord.Embed, str, tuple[discord.Embed, bytes]]

SCOPES = [
    app_commands.Choice(name="This Season", value="season"),
    app_commands.Choice(name="Career (All-Time)", value="career"),
]

# key -> (label, format)
LEADERBOARD_STATS = {
    "goals":           ("Goals", "{:.0f}"),
    "assists":         ("Assists", "{:.0f}"),
    "ga":              ("Goals + Assists", "{:.0f}"),
    "gpg":             ("Goals per Game", "{:.2f}"),
    "ratingAve":       ("Avg Rating", "{:.2f}"),
    "manOfTheMatch":   ("MOTMs", "{:.0f}"),
    "gamesPlayed":     ("Games Played", "{:.0f}"),
    "passSuccessRate": ("Pass %", "{:.0f}%"),
    "passesPerGame":   ("Passes per Game", "{:.1f}"),
    "tacklesMade":     ("Tackles", "{:.0f}"),
    "tackleSuccessRate": ("Tackle %", "{:.0f}%"),
    "cleanSheets":     ("Clean Sheets", "{:.0f}"),
}

# "Per game" / percentage stats are meaningless for someone who's played twice, so they need a
# minimum number of games: 20% of the most-played squad member's games, and never fewer than 5.
RATE_STATS = {"ratingAve", "gpg", "passSuccessRate", "passesPerGame", "tackleSuccessRate", "shotSuccessRate", "winRate"}

# Filter by the position each player plays most (EA's "favoritePosition")
POSITION_FILTERS = {"all": ("All positions", None), "GK": ("Goalkeepers", "goalkeeper"),
                    "DEF": ("Defenders", "defender"), "MID": ("Midfielders", "midfielder"),
                    "FWD": ("Forwards", "forward")}
POSITION_ORDER = ["all", "DEF", "MID", "FWD", "GK"]


def min_games_for(members: list[dict], stat: str) -> int:
    if stat not in RATE_STATS:
        return 1
    most = max((to_int(m.get("gamesPlayed")) for m in members), default=0)
    return max(5, round(most * 0.2))


def in_position(m: dict, position: str) -> bool:
    want = POSITION_FILTERS.get(position, (None, None))[1]
    return want is None or (m.get("favoritePosition") or "").lower() == want
LEADERBOARD_EMOJI = {"goals": "⚽", "assists": "🅰️", "ga": "🔥", "gpg": "🎯", "ratingAve": "📈",
                     "manOfTheMatch": "⭐", "gamesPlayed": "🎮", "passSuccessRate": "🎯", "passesPerGame": "🔁",
                     "tacklesMade": "🛡️", "tackleSuccessRate": "🛡️", "cleanSheets": "🧱"}


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def stat_value(m: dict, key: str) -> Optional[float]:
    if key == "ga":
        return to_int(m.get("goals")) + to_int(m.get("assists"))
    if key == "gpg":
        gp = to_int(m.get("gamesPlayed"))
        return (to_int(m.get("goals")) / gp) if gp else None
    if key == "passesPerGame":
        gp = to_int(m.get("gamesPlayed"))
        return (to_int(m.get("passesMade")) / gp) if gp else None
    if key == "cleanSheets":
        # EA counts keeper and outfield clean sheets separately; a player only earns the one
        # matching where they played that game, so the total is the sum.
        if m.get("cleanSheetsGK") is None and m.get("cleanSheetsDef") is None:
            return None
        return to_int(m.get("cleanSheetsGK")) + to_int(m.get("cleanSheetsDef"))
    return to_float(m.get(key))


def find_member(members: list[dict], query: str) -> tuple[Optional[dict], list[str]]:
    """Exact (case-insensitive) match first, then partial. Returns (match, other candidates)."""
    q = query.lower().strip()
    exact = [m for m in members if (m.get("name") or "").lower() == q]
    if exact:
        return exact[0], []
    partial = [m for m in members if q in (m.get("name") or "").lower()]
    if len(partial) == 1:
        return partial[0], []
    return None, [m["name"] for m in partial]


def footer(embed: discord.Embed, ea, extra: str = "") -> discord.Embed:
    note = ea.stale_note() if ea else None
    parts = [f"{CLUB_NAME} • EA FC Pro Clubs"] + ([extra] if extra else []) + ([note] if note else [])
    embed.set_footer(text=" • ".join(parts))
    return embed


async def roster_names(ea, limit: int = 25) -> list[str]:
    """Squad names, most games this season first (Discord dropdowns hold 25)."""
    members = await ea.get_member_stats(CLUB_ID) or []
    members = [m for m in members if m.get("name")]
    members.sort(key=lambda m: -to_int(m.get("gamesPlayed")))
    names, seen = [], set()
    for m in members:
        key = m["name"][:100].lower()
        if key not in seen:
            seen.add(key)
            names.append(m["name"][:100])
    return names[:limit]


def recent_player_games(name: str, limit: int) -> list[str]:
    with connect() as conn:
        rows = conn.execute("""
            SELECT m.result, m.our_goals, m.opp_goals, m.opp_name, m.ts, mp.goals, mp.assists, mp.rating, mp.pos
            FROM match_players mp JOIN matches m ON m.club_id=mp.club_id AND m.match_id=mp.match_id
            WHERE mp.club_id=? AND mp.name=? COLLATE NOCASE ORDER BY m.ts DESC LIMIT ?
        """, (CLUB_ID, name, limit)).fetchall()
    out = []
    for r in rows:
        rating = f"{r['rating']:.1f}" if r["rating"] is not None else "-"
        pos = md.POS_SHORT.get((r["pos"] or "").lower(), (r["pos"] or "")[:3].upper())
        out.append(f"{md.RESULT_EMOJI[r['result']]} {r['our_goals']}–{r['opp_goals']} {r['opp_name']} • "
                   f"{pos} • {r['goals']}G {r['assists']}A • ⭐{rating}")
    return out


# --------------------------------------------------------------------------- #
#  screen builders (shared by slash commands and the menu)
# --------------------------------------------------------------------------- #
async def build_lastgame(ea, bot=None) -> Screen:
    raw_list = await ea.get_recent_matches_multi(CLUB_ID, count=1)
    raw = raw_list[0] if raw_list else md.get_raw_match(CLUB_ID)  # fall back to our own history
    if not raw:
        return "Couldn't fetch the last game — EA or the relay may be down."
    # Safety net: if someone looks up a game the tracker hasn't posted yet, post it now
    # instead of waiting for the next scheduled check.
    mid = str(raw.get("matchId") or raw.get("timestamp") or "")
    if bot is not None and raw_list and mid and not md.is_stored(CLUB_ID, mid):
        tracker = bot.get_cog("MatchdayCog")
        if tracker:
            tracker.poll_soon("someone opened Last game")
    try:
        pm = md.parse_match(raw, CLUB_ID)
        embed, file = await md.match_post(pm)
    except Exception:
        log.exception("Error building last-game embed")
        return ("Got data from EA but couldn't read it — the format may have changed "
                "(a manager can run `/debug` to check).")
    if not raw_list:
        embed.set_footer(text=f"{CLUB_NAME} • ⚠️ EA unreachable — from the bot's saved history")
    elif ea.stale_note():
        embed.set_footer(text=f"{CLUB_NAME} • {ea.stale_note()}")
    if file:
        return embed, file.fp.read()
    return embed


async def build_clubstats(ea) -> Screen:
    stats = await ea.get_overall_stats(CLUB_ID)
    if not stats:
        return f"Couldn't fetch stats for {CLUB_NAME} right now."
    g = stats.get
    embed = discord.Embed(title=f"📊 {CLUB_NAME} — Season Stats", colour=CLUB_COLOUR)
    embed.add_field(name="Record", value=f"W{g('wins', '?')} D{g('ties', '?')} L{g('losses', '?')}", inline=True)
    embed.add_field(name="Games Played", value=str(g("gamesPlayed", "?")), inline=True)
    embed.add_field(name="Goals", value=f"{g('goals', '?')} scored / {g('goalsAgainst', '?')} conceded", inline=True)
    for label, key in (("Skill Rating", "skillRating"), ("Best Division", "bestDivision"),
                       ("Win Streak", "wstreak"), ("Unbeaten Streak", "unbeatenstreak")):
        if g(key) not in (None, ""):
            embed.add_field(name=label, value=str(g(key)), inline=True)
    embed.add_field(name="Promotions / Relegations", value=f"⬆️ {g('promotions', '?')} / ⬇️ {g('relegations', '?')}",
                    inline=True)
    if g("gamesPlayedPlayoff") not in (None, "", "0"):
        embed.add_field(name="Playoff Games", value=str(g("gamesPlayedPlayoff")), inline=True)
    recent = md.recent_results(CLUB_ID, 10)
    if recent:
        embed.add_field(name="Form (last 10)",
                        value=f"{md.form_string(recent)}  streak **{md.streak([r['result'] for r in recent])}**",
                        inline=False)
    return footer(embed, ea)


async def build_player(ea, name: str, career: bool = False, with_recent: bool = True) -> Screen:
    members = await ea.get_member_stats(CLUB_ID, career=career)
    if not members:
        return f"Couldn't fetch player stats for {CLUB_NAME} right now."
    p, candidates = find_member(members, name)
    if not p:
        return (f"**{name}** matches several players: {', '.join(candidates[:10])}. Be more specific."
                if candidates else f"No player matching **{name}** in {CLUB_NAME}.")

    embed = discord.Embed(title=f"👤 {p.get('name', 'Unknown')} — {CLUB_NAME}",
                          description="Career (All-Time)" if career else "This Season", colour=CLUB_COLOUR)

    def add(label: str, key: str, suffix: str = ""):
        v = p.get(key)
        if v not in (None, ""):
            embed.add_field(name=label, value=f"{v}{suffix}", inline=True)

    add("Games", "gamesPlayed")
    add("Goals", "goals")
    add("Assists", "assists")
    add("Avg Rating", "ratingAve")
    add("MOTM", "manOfTheMatch")
    gp = to_int(p.get("gamesPlayed"))
    if gp:
        embed.add_field(name="G+A / game", value=f"{(to_int(p.get('goals')) + to_int(p.get('assists'))) / gp:.2f}",
                        inline=True)
    if career:
        add("Favourite Position", "favoritePosition")
    else:
        add("Win Rate", "winRate", "%")
        add("Pass %", "passSuccessRate", "%")
        add("Shot %", "shotSuccessRate", "%")
        add("Tackles", "tacklesMade")
        add("Tackle %", "tackleSuccessRate", "%")
        cs_gk, cs_def = to_int(p.get("cleanSheetsGK")), to_int(p.get("cleanSheetsDef"))
        parts = ([f"{cs_gk} in goal"] if cs_gk else []) + ([f"{cs_def} outfield"] if cs_def else [])
        embed.add_field(name="Clean Sheets", value=f"{cs_gk + cs_def}" + (f" ({', '.join(parts)})" if parts else ""),
                        inline=True)
        if gp and p.get("passesMade") not in (None, ""):
            embed.add_field(name="Passes / game", value=f"{to_int(p.get('passesMade')) / gp:.1f}", inline=True)
        add("Red Cards", "redCards")
        add("Position", "favoritePosition")
        add("Overall", "proOverall")
    if with_recent:
        rows = recent_player_games(p.get("name", ""), 5)
        if rows:
            embed.add_field(name="Last games", value=clip("\n".join(rows)), inline=False)
    return footer(embed, ea)


async def build_leaderboard(ea, stat: str, career: bool = False, position: str = "all") -> Screen:
    members = await ea.get_member_stats(CLUB_ID, career=career)
    if not members:
        return "Couldn't fetch player stats right now."
    label, fmt = LEADERBOARD_STATS[stat]
    min_games = min_games_for(members, stat)
    ranked = [(m["name"], v, to_int(m.get("gamesPlayed"))) for m in members
              if m.get("name") and in_position(m, position)
              and (v := stat_value(m, stat)) is not None
              and to_int(m.get("gamesPlayed")) >= min_games]
    pos_label = POSITION_FILTERS.get(position, ("All positions",))[0]
    if not ranked:
        return (f"Nobody qualifies for **{label}** ({pos_label.lower()}, min {min_games} games)."
                if min_games > 1 else f"EA doesn't provide **{label}** for {'career' if career else 'season'} stats.")
    ranked.sort(key=lambda x: (-x[1], -x[2]))   # ties: more games first
    medals = ["🥇", "🥈", "🥉"]
    show_games = stat in RATE_STATS
    lines = [f"{medals[i] if i < 3 else f'`{i + 1:>2}.`'} **{n}** — {fmt.format(v)}" + (f"  ·  {g} games" if show_games else "")
             for i, (n, v, g) in enumerate(ranked[:15])]
    title = f"🏆 {CLUB_NAME} — {label}" + (f" ({pos_label})" if position != "all" else "")
    embed = discord.Embed(title=title, description="\n".join(lines), colour=CLUB_COLOUR)
    return footer(embed, ea, ("Career" if career else "This season") + (f" • min {min_games} games" if min_games > 1 else ""))


async def build_passing(ea, position: str = "all") -> Screen:
    """
    Passing, the way the squad asked for it: accuracy vs how involved you are
    (EA has no "touches" stat, so passes attempted per game is the involvement
    measure), per player, per position played, and for the team per match.
    """
    members = await ea.get_member_stats(CLUB_ID)
    embed = discord.Embed(title=f"🎯 {CLUB_NAME} — Passing", colour=CLUB_COLOUR,
                          description="**Acc** = pass accuracy • **Att/g** = passes attempted per game "
                                      "(how involved you are — EA doesn't track touches)")
    if members:
        min_games = min_games_for(members, "passSuccessRate")
        rows = []
        for m in members:
            gp = to_int(m.get("gamesPlayed"))
            if not m.get("name") or gp < min_games or not in_position(m, position):
                continue
            acc = to_float(m.get("passSuccessRate"))
            made = to_int(m.get("passesMade"))
            att = made / (acc / 100) if acc else made        # EA gives made + %, so attempts = made / %
            pos = md.POS_SHORT.get((m.get("favoritePosition") or "").lower(), "")
            rows.append((m["name"], pos, gp, att / gp if gp else 0, acc or 0))
        rows.sort(key=lambda r: (-r[4], -r[2]))
        if rows:
            table = [f"{'':<13}{'Pos':<4}{'GP':>3}{'Att/g':>7}{'Acc':>6}"]
            table += [f"{n[:12]:<13}{p:<4}{g:>3}{a:>7.1f}{c:>5.0f}%" for n, p, g, a, c in rows[:15]]
            pos_label = POSITION_FILTERS.get(position, ("All positions",))[0]
            embed.add_field(name=f"This season — {pos_label.lower()} (min {min_games} games)",
                            value="```\n" + "\n".join(table) + "\n```", inline=False)

    # by position actually played, from the matches the bot has tracked
    with connect() as conn:
        by_role = conn.execute("""
            SELECT LOWER(pos) AS pos, COUNT(*) AS apps, SUM(passes_made) AS made, SUM(pass_attempts) AS att
            FROM match_players WHERE club_id=? AND pos IS NOT NULL GROUP BY LOWER(pos)
        """, (CLUB_ID,)).fetchall()
        per_match = conn.execute("""
            SELECT m.result, SUM(mp.passes_made) AS made, SUM(mp.pass_attempts) AS att
            FROM matches m JOIN match_players mp ON mp.club_id=m.club_id AND mp.match_id=m.match_id
            WHERE m.club_id=? GROUP BY m.match_id ORDER BY m.ts DESC LIMIT 20
        """, (CLUB_ID,)).fetchall()
    if by_role:
        order = {"goalkeeper": 0, "defender": 1, "midfielder": 2, "forward": 3}
        lines = []
        for r in sorted(by_role, key=lambda r: order.get(r["pos"], 9)):
            if r["att"]:
                lines.append(f"**{md.POS_SHORT.get(r['pos'], r['pos'])}** — {100 * r['made'] / r['att']:.0f}% "
                             f"• {r['att'] / r['apps']:.1f} att/g ({r['apps']} game{'s' if r['apps'] != 1 else ''})")
        if lines:
            embed.add_field(name="By position played (tracked games)", value="\n".join(lines), inline=True)
    if per_match:
        tot_m = sum(r["made"] or 0 for r in per_match)
        tot_a = sum(r["att"] or 0 for r in per_match)
        lines = [f"Team: **{100 * tot_m / tot_a:.0f}%** over last {len(per_match)} games" if tot_a else ""]
        for res, word in (("W", "wins"), ("D", "draws"), ("L", "losses")):
            sub_ = [r for r in per_match if r["result"] == res]
            m_, a_ = sum(r["made"] or 0 for r in sub_), sum(r["att"] or 0 for r in sub_)
            if a_:
                lines.append(f"In {word}: {100 * m_ / a_:.0f}% ({len(sub_)})")
        embed.add_field(name="Team passing", value="\n".join(l for l in lines if l) or "—", inline=True)
    if len(embed.fields) == 0:
        return "No passing data yet."
    return footer(embed, ea)


async def build_compare(ea, a_name: str, b_name: str, career: bool = False) -> Screen:
    members = await ea.get_member_stats(CLUB_ID, career=career)
    if not members:
        return "Couldn't fetch player stats right now."
    a, _ = find_member(members, a_name)
    b, _ = find_member(members, b_name)
    if not a or not b:
        return f"Couldn't find **{a_name if not a else b_name}**."
    keys = ["gamesPlayed", "goals", "assists", "gpg", "ratingAve", "manOfTheMatch"]
    if not career:
        keys += ["passSuccessRate", "tacklesMade", "winRate"]
    labels = {k: v[0] for k, v in LEADERBOARD_STATS.items()} | {"winRate": "Win %"}
    na, nb = a["name"][:12], b["name"][:12]
    rows = [f"{'':<14}{na:>12}  {nb:>12}"]
    for k in keys:
        va, vb = stat_value(a, k), stat_value(b, k)
        if va is None and vb is None:
            continue
        f = (lambda v: "-" if v is None else (f"{v:.2f}" if k in ("gpg", "ratingAve") else f"{v:.0f}"))
        both = va is not None and vb is not None
        star_a = "◀" if both and va > vb else " "
        star_b = "◀" if both and vb > va else " "
        rows.append(f"{labels[k][:13]:<14}{f(va):>11}{star_a}  {f(vb):>11}{star_b}")
    embed = discord.Embed(title=f"⚔️ {a['name']} vs {b['name']}", description="```\n" + "\n".join(rows) + "\n```",
                          colour=CLUB_COLOUR)
    return footer(embed, ea, "Career" if career else "This season")


def build_form(games: int = 10) -> Screen:
    rows = md.recent_results(CLUB_ID, games)
    if not rows:
        return "No matches tracked yet — they're recorded automatically from now on."
    w = sum(r["result"] == "W" for r in rows)
    d = sum(r["result"] == "D" for r in rows)
    l = sum(r["result"] == "L" for r in rows)
    gf = sum(r["our_goals"] for r in rows)
    ga = sum(r["opp_goals"] for r in rows)
    lines = [f"{md.RESULT_EMOJI[r['result']]} **{r['our_goals']}–{r['opp_goals']}** {r['opp_name']} "
             f"• {md.MATCH_TYPE_LABEL.get(r['match_type'], r['match_type'])} • <t:{r['ts']}:R>" for r in rows]
    return discord.Embed(
        title=f"📈 {CLUB_NAME} — Last {len(rows)} games",
        description=f"{md.form_string(rows)}\n**W{w} D{d} L{l}** • {gf} scored, {ga} conceded "
                    f"({gf / len(rows):.1f} / {ga / len(rows):.1f} per game) • "
                    f"streak **{md.streak([r['result'] for r in rows])}**\n\n" + "\n".join(lines),
        colour=CLUB_COLOUR)


def build_h2h(opponent: str) -> Screen:
    rows = md.head_to_head(CLUB_ID, opponent)
    if not rows:
        return f"No tracked matches against **{opponent}**."
    w = sum(r["result"] == "W" for r in rows)
    d = sum(r["result"] == "D" for r in rows)
    l = sum(r["result"] == "L" for r in rows)
    lines = [f"{md.RESULT_EMOJI[r['result']]} {r['our_goals']}–{r['opp_goals']} • <t:{r['ts']}:d>" for r in rows[:10]]
    return discord.Embed(title=f"🤝 {CLUB_NAME} vs {rows[0]['opp_name']}",
                         description=f"**W{w} D{d} L{l}** • {sum(r['our_goals'] for r in rows)}–"
                                     f"{sum(r['opp_goals'] for r in rows)} on aggregate\n\n" + "\n".join(lines),
                         colour=CLUB_COLOUR)


# --------------------------------------------------------------------------- #
#  autocomplete
# --------------------------------------------------------------------------- #
def quick_squad_names(ea) -> list[str]:
    """
    Names for autocomplete without waiting on the relay (autocomplete can't defer, so a
    slow relay meant an empty list). Uses the cached EA member list, else everyone seen
    in tracked matches, and refreshes the member list in the background.
    """
    names = ea.cached_member_names(CLUB_ID) if ea is not None else None
    if names is None:
        if ea is not None and ea.configured:
            asyncio.get_running_loop().create_task(ea.get_member_stats(CLUB_ID))
        with connect() as conn:
            names = [r[0] for r in conn.execute(
                "SELECT DISTINCT name FROM match_players WHERE club_id=? AND name IS NOT NULL", (CLUB_ID,))]
    return names


async def player_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower()
    names = sorted({n for n in quick_squad_names(getattr(interaction.client, "ea", None)) if cur in n.lower()},
                   key=str.lower)
    return [app_commands.Choice(name=n[:100], value=n[:100]) for n in names][:25]


async def opponent_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=n[:100], value=n[:100]) for n in md.opponents(CLUB_ID, current)]


def card_file(png: bytes) -> discord.File:
    return discord.File(io.BytesIO(png), "result.png")


async def send_screen(interaction: discord.Interaction, screen: Screen):
    """Send a builder's result as a reply to a (deferred) slash command."""
    if isinstance(screen, str):
        from interaction_tracking import failed
        failed(interaction)
        await interaction.followup.send(screen, ephemeral=True)
    elif isinstance(screen, tuple):
        await interaction.followup.send(embed=screen[0], file=card_file(screen[1]))
    else:
        await interaction.followup.send(embed=screen)


# --------------------------------------------------------------------------- #
#  slash commands
# --------------------------------------------------------------------------- #
class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ea = bot.ea
        self.started_at = time.time()

    @app_commands.command(name="lastgame", description="Latest match result (league or playoffs)")
    async def lastgame(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await send_screen(interaction, await build_lastgame(self.ea, self.bot))

    @app_commands.command(name="clubstats", description="Season record, division and recent form")
    async def clubstats(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await send_screen(interaction, await build_clubstats(self.ea))

    @app_commands.command(name="playerstats", description="A player's season or career stats")
    @app_commands.describe(player="EA name — leave blank for yourself (if you've used /link me)",
                           scope="This season (default) or career")
    @app_commands.choices(scope=SCOPES)
    @app_commands.autocomplete(player=player_autocomplete)
    async def playerstats(self, interaction: discord.Interaction, player: Optional[str] = None,
                          scope: Optional[app_commands.Choice[str]] = None):
        await self._player(interaction, player, scope is not None and scope.value == "career")

    @app_commands.command(name="me", description="Your own stats and your last few games")
    @app_commands.choices(scope=SCOPES)
    async def me(self, interaction: discord.Interaction, scope: Optional[app_commands.Choice[str]] = None):
        await self._player(interaction, None, scope is not None and scope.value == "career")

    async def _player(self, interaction: discord.Interaction, player: Optional[str], career: bool):
        with_recent = player is None
        if not player:
            player = get_link(str(interaction.guild_id), str(interaction.user.id))
            if not player:
                from interaction_tracking import failed
                failed(interaction)
                await interaction.response.send_message(
                    "You're not linked yet — run `/link me` with your EA name first (or pass a player name).",
                    ephemeral=True)
                return
        interaction.extras["usage_lookup"] = ("Stats: player", player + (", career" if career else ", season"))
        await interaction.response.defer()
        await send_screen(interaction, await build_player(self.ea, player, career, with_recent))

    @app_commands.command(name="leaderboard", description="Rank the squad by a stat")
    @app_commands.describe(stat="What to rank by", scope="This season (default) or career",
                           position="Only players who mainly play this position")
    @app_commands.choices(stat=[app_commands.Choice(name=v[0], value=k) for k, v in LEADERBOARD_STATS.items()],
                          scope=SCOPES,
                          position=[app_commands.Choice(name=v[0], value=k) for k, v in POSITION_FILTERS.items()])
    async def leaderboard(self, interaction: discord.Interaction, stat: app_commands.Choice[str],
                          scope: Optional[app_commands.Choice[str]] = None,
                          position: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer()
        await send_screen(interaction, await build_leaderboard(self.ea, stat.value,
                                                               scope is not None and scope.value == "career",
                                                               position.value if position else "all"))

    @app_commands.command(name="passing", description="Pass accuracy vs involvement — per player, position and team")
    @app_commands.choices(position=[app_commands.Choice(name=v[0], value=k) for k, v in POSITION_FILTERS.items()])
    async def passing(self, interaction: discord.Interaction, position: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer()
        await send_screen(interaction, await build_passing(self.ea, position.value if position else "all"))

    @app_commands.command(name="compare", description="Compare two players side by side")
    @app_commands.choices(scope=SCOPES)
    @app_commands.autocomplete(player_a=player_autocomplete, player_b=player_autocomplete)
    async def compare(self, interaction: discord.Interaction, player_a: str, player_b: str,
                      scope: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer()
        await send_screen(interaction, await build_compare(self.ea, player_a, player_b,
                                                           scope is not None and scope.value == "career"))

    @app_commands.command(name="form", description="Recent results from the bot's match history")
    @app_commands.describe(games="How many games (default 10)")
    async def form(self, interaction: discord.Interaction, games: app_commands.Range[int, 1, 25] = 10):
        await interaction.response.defer()
        await send_screen(interaction, build_form(games))

    @app_commands.command(name="h2h", description="Your record against a specific club")
    @app_commands.autocomplete(opponent=opponent_autocomplete)
    async def h2h(self, interaction: discord.Interaction, opponent: str):
        await interaction.response.defer()
        await send_screen(interaction, build_h2h(opponent))

    @app_commands.command(name="status", description="Bot + EA relay health")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ok, ms, detail = await self.ea.ping()
        up = int(time.time() - self.started_at)
        lines = [
            f"**Discord latency:** {round(self.bot.latency * 1000)} ms",
            f"**Uptime:** {up // 3600}h {up % 3600 // 60}m",
            f"**EA relay:** {'🟢 online' if ok else '🔴 offline'} ({ms:.0f} ms)" + ("" if ok else f" — {detail}"),
        ]
        if self.ea.last_ok_at:
            lines.append(f"**Last successful EA fetch:** <t:{int(self.ea.last_ok_at)}:R>")
        lines.append(f"**Matches in history:** {md.match_count(CLUB_ID)}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="debug", description="Manager: raw EA data as a file (to check FC 27 field names)")
    @app_commands.choices(what=[
        app_commands.Choice(name="Latest match", value="match"),
        app_commands.Choice(name="Season member stats", value="members"),
        app_commands.Choice(name="Career member stats", value="career"),
        app_commands.Choice(name="Club overall stats", value="overall"),
    ])
    async def debug(self, interaction: discord.Interaction, what: app_commands.Choice[str]):
        if not is_manager(interaction.user):
            failed(interaction)
            await interaction.response.send_message("Managers only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if what.value == "match":
            data = await self.ea.get_recent_matches_multi(CLUB_ID, count=1)
            data = data[0] if data else md.get_raw_match(CLUB_ID)
        elif what.value in ("members", "career"):
            data = await self.ea.get_member_stats(CLUB_ID, career=what.value == "career")
        else:
            data = await self.ea.get_overall_stats(CLUB_ID)
        if data is None:
            await interaction.followup.send("No data came back.", ephemeral=True)
            return
        buf = io.BytesIO(json.dumps(data, indent=2).encode())
        await interaction.followup.send(f"Raw `{what.value}` payload:", file=discord.File(buf, f"{what.value}.json"),
                                        ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
