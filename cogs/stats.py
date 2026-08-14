"""
/lastgame  - shows the most recent league match result for MADBOYS or GRASBOYS
/clubstats - shows overall season stats for either club
/playerstats <name> - shows stats for a specific player in either club
"""

import os
import logging
from datetime import datetime, timezone

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


def club_choices():
    return [
        app_commands.Choice(name="MADBOYS", value="MADBOYS"),
        app_commands.Choice(name="GRASBOYS", value="GRASBOYS"),
    ]


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
    @app_commands.describe(club="Which club to check")
    @app_commands.choices(club=club_choices())
    async def lastgame(self, interaction: discord.Interaction, club: app_commands.Choice[str]):
        await interaction.response.defer()

        club_id = CLUBS[club.value]
        matches = await self.ea.get_recent_matches(club_id, match_type="leagueMatch", count=1)

        if not matches:
            await interaction.followup.send(
                f"Couldn't fetch recent matches for {club.value} right now — EA's API may be down. Try again in a bit.",
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

            # Top scorers from player stats
            players = match.get("players", {}).get(str(club_id), {})
            scorers = []
            for pname, pdata in players.items():
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
            for pname, pdata in players.items():
                rating = pdata.get("rating")
                if rating:
                    man_ratings.append((pname, float(rating)))
            if man_ratings:
                man_ratings.sort(key=lambda x: x[1], reverse=True)
                motm = man_ratings[0]
                embed.add_field(name="⭐ MOTM", value=f"{motm[0]} ({motm[1]:.1f})", inline=True)

            embed.set_footer(text=f"{club.value} • EA FC Pro Clubs")

        except Exception as e:
            log.error(f"Error parsing match data: {e}", exc_info=True)
            await interaction.followup.send("Got data back from EA but couldn't parse it — the API format may have changed.", ephemeral=True)
            return

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="clubstats", description="Show overall season stats for a club")
    @app_commands.describe(club="Which club to check")
    @app_commands.choices(club=club_choices())
    async def clubstats(self, interaction: discord.Interaction, club: app_commands.Choice[str]):
        await interaction.response.defer()

        club_id = CLUBS[club.value]
        stats = await self.ea.get_overall_stats(club_id)
        info = await self.ea.get_club_info(club_id)

        if not stats and not info:
            await interaction.followup.send(
                f"Couldn't fetch stats for {club.value} right now — EA's API may be down.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"📊 {club.value} — Season Stats",
            colour=CLUB_COLOURS[club.value],
        )

        if info:
            embed.add_field(name="Skill Rating", value=info.get("skillRating", "N/A"), inline=True)
            embed.add_field(name="Members", value=info.get("memberCount", "N/A"), inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)

        if stats:
            wins = stats.get("wins", "N/A")
            losses = stats.get("losses", "N/A")
            ties = stats.get("ties", "N/A")
            goals = stats.get("goals", "N/A")
            goals_against = stats.get("goalsAgainst", "N/A")
            games = stats.get("gamesPlayed", "N/A")

            embed.add_field(name="Record", value=f"W{wins} D{ties} L{losses}", inline=True)
            embed.add_field(name="Games Played", value=str(games), inline=True)
            embed.add_field(name="Goals", value=f"{goals} scored / {goals_against} conceded", inline=True)

        embed.set_footer(text=f"{club.value} • EA FC Pro Clubs")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="playerstats", description="Show stats for a specific player")
    @app_commands.describe(club="Which club the player is in", player="Player name (partial match works)")
    @app_commands.choices(club=club_choices())
    async def playerstats(
        self,
        interaction: discord.Interaction,
        club: app_commands.Choice[str],
        player: str,
    ):
        await interaction.response.defer()

        club_id = CLUBS[club.value]
        members = await self.ea.get_member_stats(club_id)

        if not members:
            await interaction.followup.send(
                f"Couldn't fetch player stats for {club.value} right now.",
                ephemeral=True,
            )
            return

        # Case-insensitive partial match on player name
        search = player.lower()
        matches = [m for m in members if search in m.get("name", "").lower()]

        if not matches:
            await interaction.followup.send(
                f"No player matching **{player}** found in {club.value}.",
                ephemeral=True,
            )
            return

        p = matches[0]  # take best match
        name = p.get("name", "Unknown")

        embed = discord.Embed(
            title=f"👤 {name} — {club.value}",
            colour=CLUB_COLOURS[club.value],
        )
        embed.add_field(name="Games", value=p.get("gamesPlayed", "N/A"), inline=True)
        embed.add_field(name="Goals", value=p.get("goals", "N/A"), inline=True)
        embed.add_field(name="Assists", value=p.get("assists", "N/A"), inline=True)
        embed.add_field(name="Avg Rating", value=p.get("ratingAve", "N/A"), inline=True)
        embed.add_field(name="Clean Sheets", value=p.get("cleanSheetsDef", "N/A"), inline=True)
        embed.add_field(name="MOTM", value=p.get("manOfTheMatch", "N/A"), inline=True)
        embed.set_footer(text=f"{club.value} • EA FC Pro Clubs")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    ea = EAClient(platform=PLATFORM)
    await bot.add_cog(StatsCog(bot, ea))
