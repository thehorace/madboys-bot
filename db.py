"""
One place for the SQLite connection, the schema, and schema migrations.

Every cog used to carry its own get_db()/init_db(). They now all call
db.connect(), and bot.py runs db.init_all() once at startup.

Note on `with sqlite3.connect(...) as conn`: that form only commits/rolls
back — it does NOT close the connection. connect() below does both.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from config import DB_PATH, migrate_legacy_club

log = logging.getLogger("madboys-bot.db")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
-- ---------- existing tables (unchanged, so old databases keep working) ----------
CREATE TABLE IF NOT EXISTS ea_links (
    guild_id   TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    ea_name    TEXT NOT NULL,
    linked_by  TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, discord_id)
);
CREATE INDEX IF NOT EXISTS idx_ea_links_name ON ea_links (guild_id, ea_name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS rotation_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   TEXT NOT NULL,
    club       TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    position   TEXT NOT NULL,
    logged_at  TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS idx_rotation_guild_club ON rotation_log (guild_id, club, discord_id, logged_at);

CREATE TABLE IF NOT EXISTS processed_matches (
    guild_id     TEXT NOT NULL,
    club         TEXT NOT NULL,
    match_id     TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, club, match_id)
);

CREATE TABLE IF NOT EXISTS matchday_poll (
    guild_id      TEXT NOT NULL,
    club          TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    last_match_id TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (guild_id, club)
);

CREATE TABLE IF NOT EXISTS active_formation (
    guild_id   TEXT NOT NULL,
    club       TEXT NOT NULL,
    formation  TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, club)
);

CREATE TABLE IF NOT EXISTS lineup_slots (
    guild_id   TEXT NOT NULL,
    club       TEXT NOT NULL,
    position   TEXT NOT NULL,
    discord_id TEXT,
    PRIMARY KEY (guild_id, club, position)
);

CREATE TABLE IF NOT EXISTS position_prefs (
    guild_id   TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    positions  TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, discord_id)
);

-- ---------- new: permanent match history ----------
CREATE TABLE IF NOT EXISTS matches (
    club_id    INTEGER NOT NULL,
    match_id   TEXT NOT NULL,
    match_type TEXT NOT NULL,
    ts         INTEGER NOT NULL,          -- unix seconds, from EA
    our_goals  INTEGER NOT NULL,
    opp_goals  INTEGER NOT NULL,
    opp_id     TEXT,
    opp_name   TEXT,
    result     TEXT NOT NULL,             -- 'W' / 'D' / 'L'
    raw_json   TEXT,
    stored_at  TEXT NOT NULL,
    PRIMARY KEY (club_id, match_id)
);
CREATE INDEX IF NOT EXISTS idx_matches_ts ON matches (club_id, ts);

CREATE TABLE IF NOT EXISTS match_players (
    club_id         INTEGER NOT NULL,
    match_id        TEXT NOT NULL,
    persona_id      TEXT NOT NULL,
    name            TEXT NOT NULL,
    pos             TEXT,
    goals           INTEGER DEFAULT 0,
    assists         INTEGER DEFAULT 0,
    rating          REAL,
    shots           INTEGER DEFAULT 0,
    passes_made     INTEGER DEFAULT 0,
    pass_attempts   INTEGER DEFAULT 0,
    tackles_made    INTEGER DEFAULT 0,
    tackle_attempts INTEGER DEFAULT 0,
    saves           INTEGER DEFAULT 0,
    red_cards       INTEGER DEFAULT 0,
    motm            INTEGER DEFAULT 0,
    archetype_id    INTEGER,
    seconds_played  INTEGER,
    clean_sheet     INTEGER,
    goals_conceded  INTEGER,
    PRIMARY KEY (club_id, match_id, persona_id)
);
CREATE INDEX IF NOT EXISTS idx_match_players_name ON match_players (club_id, name COLLATE NOCASE);

-- ---------- new: per-server settings (matchday channel, recap bookkeeping...) ----------
CREATE TABLE IF NOT EXISTS settings (
    guild_id TEXT NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT,
    PRIMARY KEY (guild_id, key)
);

-- ---------- new: last-seen career totals, for milestone shout-outs ----------
CREATE TABLE IF NOT EXISTS stat_snapshots (
    club_id     INTEGER NOT NULL,
    player_name TEXT NOT NULL,
    stat        TEXT NOT NULL,
    value       INTEGER NOT NULL,
    PRIMARY KEY (club_id, player_name, stat)
);

-- ---------- squad MOTM votes ----------
CREATE TABLE IF NOT EXISTS motm_polls (
    match_id   TEXT PRIMARY KEY,
    guild_id   TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT,
    title      TEXT NOT NULL,             -- e.g. "3–1 vs Rival FC"
    candidates TEXT NOT NULL,             -- JSON list of EA names
    ea_motm    TEXT,
    closes_at  INTEGER NOT NULL,
    closed     INTEGER NOT NULL DEFAULT 0,
    winners    TEXT                       -- JSON list (ties share the award)
);
CREATE INDEX IF NOT EXISTS idx_motm_polls_msg ON motm_polls (message_id);

CREATE TABLE IF NOT EXISTS motm_votes (
    match_id  TEXT NOT NULL,
    voter_id  TEXT NOT NULL,
    candidate TEXT NOT NULL,
    voted_at  TEXT NOT NULL,
    PRIMARY KEY (match_id, voter_id)
);

-- ---------- lineups the managers posted (source of truth for exact positions) ----------
CREATE TABLE IF NOT EXISTS lineup_plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   TEXT NOT NULL,
    club       TEXT NOT NULL,
    formation  TEXT NOT NULL,
    slots      TEXT NOT NULL,             -- JSON {slot: discord_id}
    posted_at  INTEGER NOT NULL,          -- unix seconds
    posted_by  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lineup_plans ON lineup_plans (guild_id, club, posted_at);

-- ---------- "where did you play?" prompts after a match ----------
CREATE TABLE IF NOT EXISTS position_prompts (
    message_id TEXT PRIMARY KEY,
    guild_id   TEXT NOT NULL,
    match_id   TEXT NOT NULL,
    title      TEXT NOT NULL,
    pending    TEXT NOT NULL,             -- JSON {discord_id: EA bucket e.g. "defender"}
    created_at INTEGER NOT NULL
);

-- ---------- new: play sessions + RSVPs ----------
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT,
    starts_at  INTEGER NOT NULL,          -- unix seconds
    note       TEXT,
    created_by TEXT NOT NULL,
    reminded   INTEGER NOT NULL DEFAULT 0,
    cancelled  INTEGER NOT NULL DEFAULT 0,
    sticky_messages INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_msg ON sessions (message_id);

CREATE TABLE IF NOT EXISTS session_rsvps (
    session_id INTEGER NOT NULL,
    discord_id TEXT NOT NULL,
    status     TEXT NOT NULL,             -- 'yes' / 'maybe' / 'no'
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, discord_id)
);

CREATE TABLE IF NOT EXISTS session_history (
    session_id INTEGER PRIMARY KEY,
    guild_id TEXT NOT NULL,
    ended_at INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    match_ids TEXT NOT NULL,
    rsvps TEXT NOT NULL,
    players TEXT NOT NULL,
    message_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_history_guild ON session_history(guild_id, ended_at);

CREATE TABLE IF NOT EXISTS session_summary_dms (
    session_id INTEGER NOT NULL,
    discord_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    message_id TEXT,
    PRIMARY KEY (session_id, discord_id)
);
"""


def _migrate_active_formation_pk(conn: sqlite3.Connection):
    """Old deployments keyed active_formation by guild_id alone; we need (guild_id, club)."""
    cols = conn.execute("PRAGMA table_info(active_formation)").fetchall()
    if not cols:
        return
    pk_cols = sorted(c["name"] for c in cols if c["pk"])
    if pk_cols == ["club", "guild_id"]:
        return
    log.info("Migrating active_formation to composite primary key (guild_id, club)")
    conn.executescript("""
        ALTER TABLE active_formation RENAME TO active_formation_old;
        CREATE TABLE active_formation (
            guild_id  TEXT NOT NULL,
            club      TEXT NOT NULL,
            formation TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, club)
        );
        INSERT INTO active_formation (guild_id, club, formation, updated_at)
            SELECT guild_id, club, formation, updated_at FROM active_formation_old;
        DROP TABLE active_formation_old;
    """)


def init_all():
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        # rotation_log.source was added after some DBs were created. Must run
        # before SCHEMA so an old rotation_log gets the column first.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(rotation_log)").fetchall()]
        if cols and "source" not in cols:
            conn.execute("ALTER TABLE rotation_log ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        _migrate_active_formation_pk(conn)
        conn.executescript(SCHEMA)
        # columns added to match_players after it first shipped
        have = {r["name"] for r in conn.execute("PRAGMA table_info(match_players)")}
        for col in ("archetype_id", "seconds_played", "clean_sheet", "goals_conceded"):
            if col not in have:
                conn.execute(f"ALTER TABLE match_players ADD COLUMN {col} INTEGER")
        # rotation entries remember which match they came from, so a player can correct their position
        if "match_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(rotation_log)")}:
            conn.execute("ALTER TABLE rotation_log ADD COLUMN match_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rotation_match ON rotation_log (guild_id, match_id, discord_id)")
        # how an RSVP was made: 'button' (clicked) or 'voice' (auto-added when they joined voice)
        if "source" not in {r["name"] for r in conn.execute("PRAGMA table_info(session_rsvps)")}:
            conn.execute("ALTER TABLE session_rsvps ADD COLUMN source TEXT")
        if "sticky_messages" not in {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}:
            conn.execute("ALTER TABLE sessions ADD COLUMN sticky_messages INTEGER NOT NULL DEFAULT 0")
        have_sessions = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        for column in ("sticky_at", "started_shown"):
            if column not in have_sessions:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        for table in ("rotation_log", "processed_matches", "matchday_poll", "active_formation", "lineup_slots"):
            migrate_legacy_club(conn, table)
    log.info(f"Database ready at {DB_PATH}")


# --------------------------------------------------------------------------- #
#  settings helpers
# --------------------------------------------------------------------------- #
def get_setting(guild_id: str, key: str) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key)).fetchone()
        return row["value"] if row else None


def set_setting(guild_id: str, key: str, value: Optional[str]):
    with connect() as conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE guild_id=? AND key=?", (guild_id, key))
        else:
            conn.execute(
                "INSERT INTO settings (guild_id, key, value) VALUES (?,?,?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
                (guild_id, key, value),
            )
