"""Compact, non-pinging career achievements with per-server category controls."""
import discord

from config import CLUB_COLOUR
from db import connect

CATEGORIES = {'goals': 'Goals', 'assists': 'Assists', 'gamesPlayed': 'Appearances'}


def settings(gid):
    with connect() as conn:
        saved = dict(conn.execute("SELECT key,value FROM settings WHERE guild_id=? AND key LIKE 'milestones:%'", (str(gid),)))
    return {stat: saved.get(f'milestones:{stat}') != '0' for stat in CATEGORIES}


def card(payload, enabled=None):
    enabled = enabled or {stat: True for stat in CATEGORIES}
    people = {}
    for item in payload['items']:
        if enabled.get(item['stat'], False):
            people.setdefault(item['name'], []).append(f"**{item['threshold']} {item['label']}**")
    if not people:
        return None
    lines = [f"**{discord.utils.escape_markdown(name[:60])}** · " + ' · '.join(achievements)
             for name, achievements in people.items()]
    embed = discord.Embed(title=f"🎉 Milestones · {payload['club'][:100]}", colour=CLUB_COLOUR)
    # Whole entries only: never cut a name or Markdown midway.
    shown = []
    for line in lines:
        if len('\n'.join([*shown, line])) > 3600:
            break
        shown.append(line)
    embed.description = '\n'.join(shown)
    if len(shown) < len(lines):
        embed.description += f"\n+{len(lines)-len(shown)} more players"
    embed.set_footer(text=f"After {payload['score']} vs {payload['opponent'][:100]} • Career totals from EA")
    return embed
