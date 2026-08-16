"""
Shared helpers used across cogs.
"""

import logging

import discord

log = logging.getLogger("madboys-bot.utils")


async def resolve_name(guild: discord.Guild, discord_id: str) -> str:
    """
    Resolve a discord_id to a display name.

    Tries the local member cache first (fast, no API call). If that misses
    (common cause: bot started before the member sent any recent activity,
    or the member cache just doesn't have them yet), falls back to a direct
    REST fetch. fetch_member() does NOT require the privileged "Server
    Members Intent" — it's a normal API call — so this is safe to use
    without flipping that toggle in the dev portal.

    Falls back to a placeholder only if the member has actually left the
    server or the ID is otherwise unresolvable.
    """
    member = guild.get_member(int(discord_id))
    if member:
        return member.display_name

    try:
        member = await guild.fetch_member(int(discord_id))
        return member.display_name
    except discord.NotFound:
        return f"<left server: {discord_id}>"
    except discord.HTTPException as e:
        log.warning(f"fetch_member failed for {discord_id}: {e}")
        return f"<{discord_id}>"
