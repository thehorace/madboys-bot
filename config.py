"""
Single-club configuration for MADBOYS FC (EA FC 27).

Everything tunable lives here and reads from env vars (all optional).

Club / EA
  CLUB_NAME              Display name + DB key for the club          (default "MADBOYS FC")
  CLUB_ID                EA Pro Clubs club ID (falls back to the old MADBOYS_CLUB_ID var)
  EA_PLATFORM            EA platform string                          (default "common-gen5")
  MATCH_TYPES            Comma list of EA match types to track       (default "leagueMatch,playoffMatch")

Server
  GUILD_ID               Your Discord server ID. Recommended: instant slash-command sync,
                         and tells the background tracker which server it belongs to.

Auto match tracker
  MATCHDAY_CHANNEL_ID    Channel to post results in. Optional: /matchday start sets it
                         from Discord and that choice is saved in the DB.
  POLL_ACTIVE_MINUTES    Check interval while you're playing          (default 1)
  POLL_IDLE_MINUTES      Check interval otherwise                     (default 5)
  VOICE_ACTIVE_PLAYERS   Squad members in voice that count as "playing" (default 2)
  ENABLE_PRESENCE        Set to 1 to also detect "Playing EA SPORTS FC" statuses.
                         Needs "Presence Intent" switched on in the Discord
                         Developer Portal (Bot page) FIRST, or the bot won't start.
  ACTIVE_WINDOW_MINUTES  How long after a match / session start we stay "active" (default 90)

Weekly recap + sessions
  BOT_TZ                 Timezone for recaps and /session times      (default "Asia/Singapore")
  RECAP_WEEKDAY          0=Mon ... 6=Sun                              (default 6)
  RECAP_HOUR             Hour of day, in BOT_TZ                       (default 21)

Squad MOTM vote
  MOTM_VOTE_MINUTES      How long the post-match vote stays open     (default 10)

Misc
  ROTATION_THRESHOLD     Games in the same role before /rotation check flags it (default 3)
  BUILDS_URL             Link used by /build (e.g. an FC 27 Clubs Builder site)
  DB_PATH                SQLite file. On Railway, point this into a mounted volume,
                         e.g. /data/madboys.db, or the DB is wiped on every deploy.
"""

import os
import sqlite3

CLUB_NAME = os.getenv("CLUB_NAME", "MADBOYS FC")
CLUB_ID = int(os.getenv("CLUB_ID") or os.getenv("MADBOYS_CLUB_ID") or "85077")
PLATFORM = os.getenv("EA_PLATFORM", "common-gen5")
CLUB_COLOUR = 0x1E90FF

MATCH_TYPES = [t.strip() for t in os.getenv("MATCH_TYPES", "leagueMatch,playoffMatch").split(",") if t.strip()]

GUILD_ID = os.getenv("GUILD_ID")
MATCHDAY_CHANNEL_ID = os.getenv("MATCHDAY_CHANNEL_ID")

POLL_ACTIVE_MINUTES = float(os.getenv("POLL_ACTIVE_MINUTES", "1"))
POLL_IDLE_MINUTES = float(os.getenv("POLL_IDLE_MINUTES", "5"))
VOICE_ACTIVE_PLAYERS = int(os.getenv("VOICE_ACTIVE_PLAYERS", "2"))
ENABLE_PRESENCE = os.getenv("ENABLE_PRESENCE", "").strip().lower() in ("1", "true", "yes")
ACTIVE_WINDOW_MINUTES = int(os.getenv("ACTIVE_WINDOW_MINUTES", "90"))

BOT_TZ = os.getenv("BOT_TZ", "Asia/Singapore")
RECAP_WEEKDAY = int(os.getenv("RECAP_WEEKDAY", "6"))
RECAP_HOUR = int(os.getenv("RECAP_HOUR", "21"))

# (MOTM_VOTE_HOURS still works if someone already set it in Railway)
MOTM_VOTE_MINUTES = float(os.getenv("MOTM_VOTE_MINUTES")
                          or float(os.getenv("MOTM_VOTE_HOURS") or 0) * 60 or 10)

ROTATION_THRESHOLD = int(os.getenv("ROTATION_THRESHOLD", "3"))
BUILDS_URL = os.getenv("BUILDS_URL", "")

DB_PATH = os.getenv("DB_PATH", "madboys.db")

# Names the club was stored under in older databases.
LEGACY_CLUB_NAMES = ("MADBOYS",)


def migrate_legacy_club(conn: sqlite3.Connection, table: str) -> int:
    """
    Re-key rows stored under the old club name ("MADBOYS") to CLUB_NAME so
    existing history carries over to MADBOYS FC. UPDATE OR IGNORE means a row
    that would collide with an existing primary key is left alone.
    """
    moved = 0
    for old in LEGACY_CLUB_NAMES:
        if old == CLUB_NAME:
            continue
        cur = conn.execute(f"UPDATE OR IGNORE {table} SET club=? WHERE club=?", (CLUB_NAME, old))
        moved += cur.rowcount
    return moved
