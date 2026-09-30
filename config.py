"""
Single-club configuration for MADBOYS FC (EA FC 27).

Everything that used to be a MADBOYS/GRASBOYS choice now reads from here.

Env vars (all optional):
  CLUB_NAME         Display name + DB key for the club   (default "MADBOYS FC")
  CLUB_ID           EA Pro Clubs club ID                 (falls back to the old
                    MADBOYS_CLUB_ID var so existing Railway config keeps working)
  EA_PLATFORM       EA platform string                   (default "common-gen5")
"""

import os
import sqlite3

CLUB_NAME = os.getenv("CLUB_NAME", "MADBOYS FC")
CLUB_ID = int(os.getenv("CLUB_ID") or os.getenv("MADBOYS_CLUB_ID") or "85077")
PLATFORM = os.getenv("EA_PLATFORM", "common-gen5")
CLUB_COLOUR = 0x1E90FF

# Names the club was stored under in older databases.
LEGACY_CLUB_NAMES = ("MADBOYS",)


def migrate_legacy_club(conn: sqlite3.Connection, table: str) -> int:
    """
    Re-key rows stored under the old club name ("MADBOYS") to CLUB_NAME so
    existing history (lineups, rotation log, processed matches, poll state)
    carries over to MADBOYS FC. UPDATE OR IGNORE means a row that would
    collide with an existing primary key is left alone rather than crashing.
    Rows for other clubs (e.g. GRASBOYS) are not touched.
    """
    moved = 0
    for old in LEGACY_CLUB_NAMES:
        if old == CLUB_NAME:
            continue
        cur = conn.execute(f"UPDATE OR IGNORE {table} SET club=? WHERE club=?", (CLUB_NAME, old))
        moved += cur.rowcount
    return moved
