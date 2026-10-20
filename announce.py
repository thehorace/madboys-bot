"""
One channel for everything the bot posts on its own.

Scheduled / automatic posts (match results, MOTM votes, milestones, weekly recap,
daily session sign-ups, session summaries, EA news) all resolve their channel
through announce_channel_id(). Slash commands never use it: they reply in the
channel they were run in.

Resolution order:
  1. /announce set  (saved in the DB, survives redeploys)
  2. ANNOUNCE_CHANNEL_ID env var
  3. a text channel named ANNOUNCE_CHANNEL_NAME (default "announcements"), found by cogs/announce.py
  4. the feature's own legacy channel setting (so nothing breaks if there's no such channel)
"""
from typing import Optional

from config import ANNOUNCE_CHANNEL_ID, ANNOUNCE_CHANNEL_NAME
from db import get_setting, set_setting

K_ANNOUNCE = "announce:channel"

# guild id -> id of the channel named ANNOUNCE_CHANNEL_NAME (kept fresh by cogs/announce.py)
_named: dict[str, str] = {}


def refresh_named(guild) -> Optional[str]:
    """Look up the announcements channel by name in this guild. Only an unambiguous match counts."""
    found = [c for c in guild.text_channels if c.name.lower() == ANNOUNCE_CHANNEL_NAME]
    if len(found) == 1:
        _named[str(guild.id)] = str(found[0].id)
    else:
        _named.pop(str(guild.id), None)
    return _named.get(str(guild.id))


def announce_channel_id(guild_id, fallback: Optional[str] = None) -> Optional[str]:
    """The channel id (as a string) automatic posts should use, or `fallback` if none is set."""
    cid = get_setting(str(guild_id), K_ANNOUNCE) or ANNOUNCE_CHANNEL_ID or _named.get(str(guild_id)) or fallback
    return str(cid) if cid else None


def set_announce_channel(guild_id, channel_id: Optional[int]):
    set_setting(str(guild_id), K_ANNOUNCE, str(channel_id) if channel_id else None)
