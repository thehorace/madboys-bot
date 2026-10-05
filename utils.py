"""
Shared helpers used across cogs (previously copy-pasted into several files).
"""

import asyncio
import functools
import logging
from typing import Optional

import discord

log = logging.getLogger("madboys-bot.utils")

MANAGER_ROLE_NAMES = ("manager", "admin", "coach")

RESULT_LABEL = {"W": "✅ WIN", "L": "❌ LOSS", "D": "🟡 DRAW"}
RESULT_EMOJI = {"W": "✅", "L": "❌", "D": "🟡"}
RESULT_COLOUR = {"W": 0x2ECC71, "L": 0xE74C3C, "D": 0xF1C40F}


def result_letter(ours: int, theirs: int) -> str:
    if ours > theirs:
        return "W"
    if ours < theirs:
        return "L"
    return "D"


def format_result(ours: int, theirs: int) -> str:
    return RESULT_LABEL[result_letter(ours, theirs)]


def is_manager(member: discord.abc.User) -> bool:
    """Manage Channels permission, or a role called Manager / Admin / Coach."""
    perms = getattr(member, "guild_permissions", None)
    if perms and perms.manage_channels:
        return True
    return any(r.name.lower() in MANAGER_ROLE_NAMES for r in getattr(member, "roles", []))


_LEFT: dict[int, float] = {}       # discord id -> when Discord said they're not in the server
_LEFT_TTL = 6 * 3600


async def resolve_name(guild: discord.Guild, discord_id: str) -> str:
    """
    discord_id -> display name. Member cache first (free), then a REST fetch,
    then a placeholder if they've left the server.
    """
    member = guild.get_member(int(discord_id))
    if member:
        return member.display_name
    import time
    if time.time() - _LEFT.get(int(discord_id), 0) < _LEFT_TTL:
        return f"<left server: {discord_id}>"   # asked recently; skip the slow API call
    try:
        member = await guild.fetch_member(int(discord_id))
        return member.display_name
    except discord.NotFound:
        import time
        _LEFT[int(discord_id)] = time.time()
        return f"<left server: {discord_id}>"
    except discord.HTTPException as e:
        log.warning(f"fetch_member failed for {discord_id}: {e}")
        return f"<{discord_id}>"


def to_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def to_float(v, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pct(made: int, attempts: int) -> Optional[float]:
    return (100.0 * made / attempts) if attempts else None


def clip(text: str, limit: int = 1024) -> str:
    """Embed field values max out at 1024 chars; trim instead of crashing."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def survive(fn):
    """
    Put under @tasks.loop(...). discord.py stops a loop for good on any error that isn't a
    network error, so one bad row or a "database is locked" would kill reminders / recaps
    until a restart. This logs the error and lets the loop run again on its next tick.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background task %s failed; it will try again next run", fn.__qualname__)
    return wrapper
