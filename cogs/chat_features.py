"""Read-only Discord views that mention chat can display without invoking slash commands."""
import asyncio
from dataclasses import dataclass

import discord
from discord import app_commands

from clubs import club_scope


@dataclass(frozen=True)
class FeatureReply:
    command: str
    screen: str | discord.Embed | tuple[discord.Embed, bytes]

    def summary(self):
        if isinstance(self.screen, str):
            return self.screen
        embed = self.screen[0] if isinstance(self.screen, tuple) else self.screen
        return "\n".join(part for part in (embed.title, embed.description) if part) or self.command


FEATURE_COMMANDS = {
    "player_twin": "/playertwin",
    "season_wrapped": "/wrapped",
    "club_records": "/records",
    "partnerships": "/partners",
    "awards": "/awards",
    "passing": "/passing",
    "club_stats": "/clubstats",
    "recap": "/recap",
}


def command_catalog(bot, guild):
    """Use registered slash commands, including servers with guild-only sync."""
    tree = getattr(bot, "tree", None)
    if tree is None:
        return {}
    commands = {}

    def add(command):
        if guild is None and getattr(command, "guild_only", False):
            return
        if isinstance(command, app_commands.Group):
            for child in command.commands:
                add(child)
        elif isinstance(command, app_commands.Command):
            commands[f"/{command.qualified_name}"] = command.description

    for command in tree.get_commands():
        add(command)
    if guild:
        for command in tree.get_commands(guild=guild):
            add(command)
    return commands


async def show_feature(bot, guild_id, club, feature, *, player=None, asker_ea=None, month=None, days=7):
    """Only named, read-only views are supported. Private actions stay with their commands."""
    from cogs import insights

    if player and player.strip().casefold() in {"me", "myself"}:
        player = asker_ea
        if not player:
            return "Link your EA name with `/link me` first, or ask for an EA player by name."
    with club_scope(club):
        if feature == "player_twin":
            from player_twin import resolve_name
            query = player or asker_ea
            if not query:
                return "Link your EA name with `/link me` first, or ask for a player's EA name."
            name, candidates = await asyncio.to_thread(resolve_name, query)
            if candidates:
                choices = ", ".join(discord.utils.escape_markdown(n[:80]) for n in candidates[:8])
                return f"Which player did you mean? {choices}"
            if not name:
                return f"No saved matches for **{discord.utils.escape_markdown(query[:80])}** yet."
            return await insights.build_player_twin(name)
        if feature == "season_wrapped":
            return await insights.build_wrapped(guild_id, player or asker_ea)
        if feature == "club_records":
            return await insights.build_records(guild_id)
        if feature == "partnerships":
            from insights import build_partnerships
            return await asyncio.to_thread(build_partnerships, player)
        if feature == "awards":
            from cogs.awards import build_awards
            return await build_awards(guild_id, month)
        if feature == "passing":
            from cogs.stats import build_passing
            return await build_passing(bot.ea)
        if feature == "club_stats":
            from cogs.stats import build_clubstats
            return await build_clubstats(bot.ea)
        if feature == "recap":
            from cogs.matchday import build_recap_embed
            days = max(1, min(int(days), 90))
            embed = await asyncio.to_thread(build_recap_embed, days)
            return embed or f"No tracked matches in the last {days} days."
        return "That view is not available in mention chat."
