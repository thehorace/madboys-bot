"""
/lastgame  - shows the most recent league match result for MADBOYS or GRASBOYS
/clubstats - shows overall season stats for either club
/playerstats <name> - shows stats for a specific player in either club

All three commands take an optional `timeframe` dropdown:
  Last 24 Hours / Last Week / Last Month / Last 3 Months / All Time

IMPORTANT CAVEAT: EA's Pro Clubs API has no "matches since <date>" query.
The only lever we have is "give me the last N matches". So a timeframe
filter here means: fetch a batch of N recent matches (N = STATS_MATCH_FETCH_LIMIT,
default 30) and filter that batch by each match's `timestamp` field.

Consequences of that:
  - If a club has played fewer games than N within a window, results for
    that window and "All Time" (which for lastgame just means "most recent
    match") will look similar.
  - If a club has played MORE than N games recently, older matches outside
    the fetched batch won't be considered even if they're inside the
    requested window (e.g. "3 Months" could under-count for a very active club).
  - For /clubstats and /playerstats, "All Time" intentionally uses EA's own
    aggregate endpoints (get_overall_stats / get_member_stats(career=True))
    rather than the fetched-batch math, since those are accurate season/
    career totals straight from EA. Only the shorter windows are computed
    client-side from the fetched match batch.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from ea_client import EAClient

log = logging.getLogger("madboys-bot.stats")

PLATFORM = os.getenv("EA_PLATFORM", "common-gen5")

CLUBS = {
    "MADBOYS": int(os.getenv("MADBOYS_CLUB_ID", "85077")),
    "GRASBOYS": int(os.getenv("GRASBOYS_CLUB_ID", "4137103")),
}

CLUB_COLOURS = {
    "MADBOYS": 0x1E90FF,   # blue
    "GRASBOYS": 0x2ECC71,  # green
}

club_choice = app_commands.Choice

# How many recent matches to pull from EA when a non-"All" timeframe is
# selected, since there's no native date-range query to lean on instead.
MATCH_FETCH_LIMIT = int(os.getenv("STATS_MATCH_FETCH_LIMIT", "30"))

TIMEFRAME_DAYS = {
    "1d": 1,
    "1w": 7,
    "1m": 30,
    "3m": 90,
    "all": None,
}

TIMEFRAME_LABELS = {
    "1d": "Last 24 Hours",
    "1w": "Last Week",
    "1m": "Last Month",
    "3m": "Last 3 Months",
    "all": "All Time",
}


def club_choices():
    return [
        app_commands.Choice(name="MADBOYS", value="MADBOYS"),
        app_commands.Choice(name="GRASBOYS", value="GRASBOYS"),
    ]


def timeframe_choices():
    return [app_commands.Choice(name=label, value=key) for key, label in TIMEFRAME_LABELS.items()]


def format_result(club_score: int, opp_score: int) -> str:
    if club_score > opp_score:
        return "✅ WIN"
    elif club_score < opp_score:
        return "❌ LOSS"
    return "🟡 DRAW"


def _match_epoch(match: dict) -> Optional[int]:
    """EA's 'timestamp' field is Unix epoch seconds."""
    ts = match.get("timestamp")
    try:
        return int(ts)
    except (TypeError, ValueError):
        return None


def filter_by_timeframe(matches: list, tf_key: str) -> list:
    """Filter a list of matches down to those within the given window.
    tf_key='all' returns the list unchanged."""
    days = TIMEFRAME_DAYS.get(tf_key)
    if days is None:
        return matches
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
    out = []
    for m in matches:
        epoch = _match_epoch(m)
        if epoch is not None and epoch >= cutoff:
            out.append(m)
    return out


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, ea: EAClient):
        self.bot = bot
        self.ea = ea

    # ------------------------------------------------------------------ #
    #  /lastgame
    # ------------------------------------------------------------------ #
    @app_commands.command(name="lastgame", description="Show the most recent league match result")
    @app_commands.describe(club="Which club to check", timeframe="Only consider matches from this period")
    @app_commands.choices(club=club_choices(), timeframe=timeframe_choices())
    async def lastgame(
        self,
        interaction: discord.Interaction,
        club: app_commands.Choice[str],
        timeframe: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer()

        tf_key = timeframe.value if timeframe else "all"
        club_id = CLUBS[club.value]

        # "All" just wants the single newest match, same as before.
        # Anything narrower needs a bigger batch to filter down from.
        fetch_count = 1 if tf_key == "all" else MATCH_FETCH_LIMIT
        matches = await self.ea.get_recent_matches(club_id, match_type="leagueMatch", count=fetch_count)

        if not matches:
            await interaction.followup.send(
                f"Couldn't fetch recent matches for {club.value} right now — EA's API may be down. Try again in a bit.",
                ephemeral=True,
            )
            return

        matches = filter_by_timeframe(matches, tf_key)
        if not matches:
            await interaction.followup.send(
                f"No {club.value} matches found in **{TIMEFRAME_LABELS[tf_key]}**. "
                f"Try a wider timeframe.",
                ephemeral=True,
            )
            return

        match = matches[0]  # assumed newest-first from EA

        try:
            clubs_data = match.get("clubs", {})
            club_data = clubs_data.get(str(club_id), {})
            opp_id = next((k for k in clubs_data if k != str(club_id)), None)
            opp_data = clubs_data.get(opp_id, {}) if opp_id else {}

            our_score = int(club_data.get("goals", 0))
            opp_score = int(opp_data.get("goals", 0))
            opp_name = opp_data.get("details", {}).get("name", "Unknown")
            result = format_result(our_score, opp_score)

            # NOTE: EA keys this dict by numeric player ID, not name — the
            # actual display name lives in each player's "playername" field.
            players = match.get("players", {}).get(str(club_id), {})
            scorers = []
            for pid, pdata in players.items():
                pname = pdata.get("playername") or f"<{pid}>"
                goals = int(pdata.get("goals", 0))
                assists = int(pdata.get("assists", 0))
                if goals > 0 or assists > 0:
                    scorers.append(f"{pname} — {goals}G {assists}A")

            embed = discord.Embed(
                title=f"{result}  {club.value} {our_score}–{opp_score} {opp_name}",
                colour=CLUB_COLOURS[club.value],
            )
            embed.add_field(
                name="Goals & Assists",
                value="\n".join(scorers) if scorers else "No goal contributions recorded",
                inline=False,
            )

            man_ratings = []
            for pid, pdata in players.items():
                rating = pdata.get("rating")
                if rating:
                    pname = pdata.get("playername") or f"<{pid}>"
                    man_ratings.append((pname, float(rating)))
            if man_ratings:
                man_ratings.sort(key=lambda x: x[1], reverse=True)
                motm = man_ratings[0]
                embed.add_field(name="⭐ MOTM", value=f"{motm[0]} ({motm[1]:.1f})", inline=True)

            embed.set_footer(text=f"{club.value} • EA FC Pro Clubs • {TIMEFRAME_LABELS[tf_key]}")

        except Exception as e:
            log.error(f"Error parsing match data: {e}", exc_info=True)
            await interaction.followup.send("Got data back from EA but couldn't parse it — the API format may have changed.", ephemeral=True)
            return

        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #
    #  /clubstats
    # ------------------------------------------------------------------ #
    @app_commands.command(name="clubstats", description="Show overall stats for a club")
    @app_commands.describe(club="Which club to check", timeframe="Only include matches from this period")
    @app_commands.choices(club=club_choices(), timeframe=timeframe_choices())
    async def clubstats(
        self,
        interaction: discord.Interaction,
        club: app_commands.Choice[str],
        timeframe: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer()

        tf_key = timeframe.value if timeframe else "all"
        club_id = CLUBS[club.value]

        if tf_key == "all":
            # Unchanged: EA's own season aggregate, includes streaks/promotions/etc.
            stats = await self.ea.get_overall_stats(club_id)
            if not stats:
                await interaction.followup.send(
                    f"Couldn't fetch stats for {club.value} right now — EA's API may be down.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title=f"📊 {club.value} — Season Stats",
                colour=CLUB_COLOURS[club.value],
            )

            wins = stats.get("wins", "N/A")
            losses = stats.get("losses", "N/A")
            ties = stats.get("ties", "N/A")
            goals = stats.get("goals", "N/A")
            goals_against = stats.get("goalsAgainst", "N/A")
            games = stats.get("gamesPlayed", "N/A")

            embed.add_field(name="Record", value=f"W{wins} D{ties} L{losses}", inline=True)
            embed.add_field(name="Games Played", value=str(games), inline=True)
            embed.add_field(name="Goals", value=f"{goals} scored / {goals_against} conceded", inline=True)

            embed.add_field(name="Best Division", value=stats.get("bestDivision", "N/A"), inline=True)
            embed.add_field(name="Win Streak", value=stats.get("wstreak", "N/A"), inline=True)
            embed.add_field(name="Unbeaten Streak", value=stats.get("unbeatenstreak", "N/A"), inline=True)

            promotions = stats.get("promotions", "N/A")
            relegations = stats.get("relegations", "N/A")
            embed.add_field(name="Promotions / Relegations", value=f"⬆️ {promotions} / ⬇️ {relegations}", inline=True)

            playoff_games = stats.get("gamesPlayedPlayoff")
            if playoff_games and playoff_games != "0":
                embed.add_field(name="Playoff Games", value=playoff_games, inline=True)

            embed.set_footer(text=f"{club.value} • EA FC Pro Clubs • All Time")

        else:
            # Recompute record/goals from a filtered batch of recent matches.
            # Streaks/promotions/best-division aren't meaningful for a partial
            # window, so we don't fabricate them here.
            matches = await self.ea.get_recent_matches(club_id, match_type="leagueMatch", count=MATCH_FETCH_LIMIT)
            if not matches:
                await interaction.followup.send(
                    f"Couldn't fetch recent matches for {club.value} right now — EA's API may be down.",
                    ephemeral=True,
                )
                return

            matches = filter_by_timeframe(matches, tf_key)
            if not matches:
                await interaction.followup.send(
                    f"No {club.value} matches found in **{TIMEFRAME_LABELS[tf_key]}**. Try a wider timeframe.",
                    ephemeral=True,
                )
                return

            wins = losses = ties = goals = goals_against = 0
            for m in matches:
                clubs_data = m.get("clubs", {})
                cdata = clubs_data.get(str(club_id), {})
                opp_id = next((k for k in clubs_data if k != str(club_id)), None)
                odata = clubs_data.get(opp_id, {}) if opp_id else {}

                gf = int(cdata.get("goals", 0))
                ga = int(odata.get("goals", 0))
                goals += gf
                goals_against += ga
                if gf > ga:
                    wins += 1
                elif gf < ga:
                    losses += 1
                else:
                    ties += 1

            embed = discord.Embed(
                title=f"📊 {club.value} — Stats ({TIMEFRAME_LABELS[tf_key]})",
                colour=CLUB_COLOURS[club.value],
            )
            embed.add_field(name="Record", value=f"W{wins} D{ties} L{losses}", inline=True)
            embed.add_field(name="Games Played", value=str(len(matches)), inline=True)
            embed.add_field(name="Goals", value=f"{goals} scored / {goals_against} conceded", inline=True)
            embed.set_footer(
                text=f"{club.value} • EA FC Pro Clubs • based on {len(matches)} matches "
                     f"(fetched up to {MATCH_FETCH_LIMIT} most recent)"
            )

        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #
    #  /playerstats
    # ------------------------------------------------------------------ #
    @app_commands.command(name="playerstats", description="Show stats for a specific player")
    @app_commands.describe(
        club="Which club the player is in",
        player="Player name (partial match works)",
        timeframe="Only include matches from this period — All Time uses career totals",
    )
    @app_commands.choices(club=club_choices(), timeframe=timeframe_choices())
    async def playerstats(
        self,
        interaction: discord.Interaction,
        club: app_commands.Choice[str],
        player: str,
        timeframe: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer()

        tf_key = timeframe.value if timeframe else "all"
        club_id = CLUBS[club.value]
        search = player.lower()

        if tf_key == "all":
            # Unchanged: EA's own career aggregate.
            members = await self.ea.get_member_stats(club_id, career=True)
            if not members:
                await interaction.followup.send(
                    f"Couldn't fetch player stats for {club.value} right now.",
                    ephemeral=True,
                )
                return

            matches_p = [m for m in members if search in m.get("name", "").lower()]
            if not matches_p:
                await interaction.followup.send(
                    f"No player matching **{player}** found in {club.value}.",
                    ephemeral=True,
                )
                return

            p = matches_p[0]
            name = p.get("name", "Unknown")

            embed = discord.Embed(
                title=f"👤 {name} — {club.value}",
                description="Career (All-Time)",
                colour=CLUB_COLOURS[club.value],
            )
            embed.add_field(name="Games", value=p.get("gamesPlayed", "N/A"), inline=True)
            embed.add_field(name="Goals", value=p.get("goals", "N/A"), inline=True)
            embed.add_field(name="Assists", value=p.get("assists", "N/A"), inline=True)
            embed.add_field(name="Avg Rating", value=p.get("ratingAve", "N/A"), inline=True)
            embed.add_field(name="MOTM", value=p.get("manOfTheMatch", "N/A"), inline=True)
            embed.add_field(name="Favourite Position", value=p.get("favoritePosition", "N/A"), inline=True)

            embed.set_footer(text=f"{club.value} • EA FC Pro Clubs")

        else:
            # Recompute per-player totals from a filtered batch of recent matches.
            matches = await self.ea.get_recent_matches(club_id, match_type="leagueMatch", count=MATCH_FETCH_LIMIT)
            if not matches:
                await interaction.followup.send(
                    f"Couldn't fetch recent matches for {club.value} right now.",
                    ephemeral=True,
                )
                return

            matches = filter_by_timeframe(matches, tf_key)
            if not matches:
                await interaction.followup.send(
                    f"No {club.value} matches found in **{TIMEFRAME_LABELS[tf_key]}**. Try a wider timeframe.",
                    ephemeral=True,
                )
                return

            games = goals = assists = wins = motm_count = 0
            ratings = []
            matched_name = None

            for m in matches:
                players = m.get("players", {}).get(str(club_id), {})

                hit = None
                for pid, pdata in players.items():
                    pname = pdata.get("playername", "")
                    if search in pname.lower():
                        hit = pdata
                        matched_name = pname
                        break
                if not hit:
                    continue

                games += 1
                goals += int(hit.get("goals", 0))
                assists += int(hit.get("assists", 0))
                rating = hit.get("rating")
                if rating:
                    ratings.append(float(rating))

                clubs_data = m.get("clubs", {})
                cdata = clubs_data.get(str(club_id), {})
                opp_id = next((k for k in clubs_data if k != str(club_id)), None)
                odata = clubs_data.get(opp_id, {}) if opp_id else {}
                gf = int(cdata.get("goals", 0))
                ga = int(odata.get("goals", 0))
                if gf > ga:
                    wins += 1

                # MOTM here = highest-rated player in the club for that match
                all_ratings = [
                    (pd.get("playername"), float(pd.get("rating")))
                    for pd in players.values() if pd.get("rating")
                ]
                if all_ratings:
                    all_ratings.sort(key=lambda x: x[1], reverse=True)
                    if all_ratings[0][0] == matched_name:
                        motm_count += 1

            if games == 0:
                await interaction.followup.send(
                    f"No matches found for a player matching **{player}** in {club.value} "
                    f"during {TIMEFRAME_LABELS[tf_key]}.",
                    ephemeral=True,
                )
                return

            avg_rating = sum(ratings) / len(ratings) if ratings else None
            win_rate = round((wins / games) * 100) if games else 0

            embed = discord.Embed(
                title=f"👤 {matched_name} — {club.value}",
                description=TIMEFRAME_LABELS[tf_key],
                colour=CLUB_COLOURS[club.value],
            )
            embed.add_field(name="Games", value=str(games), inline=True)
            embed.add_field(name="Goals", value=str(goals), inline=True)
            embed.add_field(name="Assists", value=str(assists), inline=True)
            embed.add_field(name="Avg Rating", value=f"{avg_rating:.1f}" if avg_rating else "N/A", inline=True)
            embed.add_field(name="MOTM", value=str(motm_count), inline=True)
            embed.add_field(name="Win Rate", value=f"{win_rate}%", inline=True)

            embed.set_footer(
                text=f"{club.value} • based on {games} matches within window "
                     f"(fetched up to {MATCH_FETCH_LIMIT} most recent)"
            )

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    ea = EAClient(platform=PLATFORM)
    await bot.add_cog(StatsCog(bot, ea))
