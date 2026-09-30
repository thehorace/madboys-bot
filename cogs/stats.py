"""
/lastgame   - shows the most recent league match result for MADBOYS FC
/clubstats  - shows overall season stats
/playerstats <name> - shows stats for a specific player
"""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import CLUB_COLOUR, CLUB_ID, CLUB_NAME, PLATFORM
from ea_client import EAClient

log = logging.getLogger("madboys-bot.stats")


def format_result(club_score: int, opp_score: int) -> str:
    if club_score > opp_score:
        return "✅ WIN"
    elif club_score < opp_score:
        return "❌ LOSS"
    return "🟡 DRAW"


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, ea: EAClient):
        self.bot = bot
        self.ea = ea

    @app_commands.command(name="lastgame", description="Show the most recent league match result")
    async def lastgame(self, interaction: discord.Interaction):
        await interaction.response.defer()

        club_id = CLUB_ID
        matches = await self.ea.get_recent_matches(club_id, match_type="leagueMatch", count=1)

        if not matches:
            await interaction.followup.send(
                f"Couldn't fetch recent matches for {CLUB_NAME} right now — EA's API may be down. Try again in a bit.",
                ephemeral=True,
            )
            return

        match = matches[0]

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
                title=f"{result}  {CLUB_NAME} {our_score}–{opp_score} {opp_name}",
                colour=CLUB_COLOUR,
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

            embed.set_footer(text=f"{CLUB_NAME} • EA FC Pro Clubs")

        except Exception as e:
            log.error(f"Error parsing match data: {e}", exc_info=True)
            await interaction.followup.send("Got data back from EA but couldn't parse it — the API format may have changed.", ephemeral=True)
            return

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="clubstats", description="Show overall season stats for the club")
    async def clubstats(self, interaction: discord.Interaction):
        await interaction.response.defer()

        stats = await self.ea.get_overall_stats(CLUB_ID)

        if not stats:
            await interaction.followup.send(
                f"Couldn't fetch stats for {CLUB_NAME} right now — EA's API may be down.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"📊 {CLUB_NAME} — Season Stats",
            colour=CLUB_COLOUR,
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

        embed.set_footer(text=f"{CLUB_NAME} • EA FC Pro Clubs")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="playerstats", description="Show stats for a specific player")
    @app_commands.describe(
        player="Player name (partial match works)",
        scope="This season's stats, or career (all-time) totals — defaults to this season",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="This Season", value="season"),
            app_commands.Choice(name="Career (All-Time)", value="career"),
        ],
    )
    async def playerstats(
        self,
        interaction: discord.Interaction,
        player: str,
        scope: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer()

        is_career = scope is not None and scope.value == "career"
        members = await self.ea.get_member_stats(CLUB_ID, career=is_career)

        if not members:
            await interaction.followup.send(
                f"Couldn't fetch player stats for {CLUB_NAME} right now.",
                ephemeral=True,
            )
            return

        # Case-insensitive partial match on player name
        search = player.lower()
        matches = [m for m in members if search in m.get("name", "").lower()]

        if not matches:
            await interaction.followup.send(
                f"No player matching **{player}** found in {CLUB_NAME}.",
                ephemeral=True,
            )
            return

        p = matches[0]  # take best match
        name = p.get("name", "Unknown")
        scope_label = "Career (All-Time)" if is_career else "This Season"

        embed = discord.Embed(
            title=f"👤 {name} — {CLUB_NAME}",
            description=scope_label,
            colour=CLUB_COLOUR,
        )
        embed.add_field(name="Games", value=p.get("gamesPlayed", "N/A"), inline=True)
        embed.add_field(name="Goals", value=p.get("goals", "N/A"), inline=True)
        embed.add_field(name="Assists", value=p.get("assists", "N/A"), inline=True)
        embed.add_field(name="Avg Rating", value=p.get("ratingAve", "N/A"), inline=True)
        embed.add_field(name="MOTM", value=p.get("manOfTheMatch", "N/A"), inline=True)

        if is_career:
            # EA's career payload doesn't include clean sheets or win rate —
            # it only has games/goals/assists/MOTM/rating + favourite position.
            embed.add_field(name="Favourite Position", value=p.get("favoritePosition", "N/A"), inline=True)
        else:
            embed.add_field(name="Clean Sheets", value=p.get("cleanSheetsDef", "N/A"), inline=True)
            win_rate = p.get("winRate")
            embed.add_field(name="Win Rate", value=f"{win_rate}%" if win_rate else "N/A", inline=True)

        embed.set_footer(text=f"{CLUB_NAME} • EA FC Pro Clubs")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    ea = EAClient(platform=PLATFORM)
    await bot.add_cog(StatsCog(bot, ea))
