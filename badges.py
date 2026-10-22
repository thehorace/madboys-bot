"""
Badges: earned automatically from the games the bot has saved, shown as an emoji on the
player's server nickname (e.g. "Fauz 🎩").

Rules (all from real tracked games, any club):
  🎩 Hat-trick Hero     3+ goals in a game
  🎯 Playmaker          3+ assists in a game
  🌟 Star Man           a 9.0+ match rating
  🔥 On Fire            scored in 5 games in a row (same club)
  🧱 Brick Wall         3 clean sheets in a row as keeper/defender (same club)
  🧤 Safe Hands         8+ saves in a game in goal
  🗳️ Fan Favourite      5 squad MOTM votes won
  💯 Centurion          100 tracked games
  👑 Player of the Month  won a monthly award

Badges that can happen again (hat-tricks, 9.0 games...) keep a count ("🎩 ×3").
Only linked players (EA name ↔ Discord) can earn badges, since they're stored per Discord user.
A player's history is badged quietly the first time they're seen (no flood of announcements
when the feature launches or someone links late); after that, new badges are announced.
"""

import json
import time
from typing import Optional

from db import connect

BADGES = {   # key: (emoji, name, how to earn it)
    "hat_trick":     ("🎩", "Hat-trick Hero", "3+ goals in a game"),
    "playmaker":     ("🎯", "Playmaker", "3+ assists in a game"),
    "star_man":      ("🌟", "Star Man", "a 9.0+ match rating"),
    "on_fire":       ("🔥", "On Fire", "scored in 5 games in a row"),
    "brick_wall":    ("🧱", "Brick Wall", "3 clean sheets in a row as keeper/defender"),
    "safe_hands":    ("🧤", "Safe Hands", "8+ saves in a game in goal"),
    "fan_favourite": ("🗳️", "Fan Favourite", "won 5 squad MOTM votes"),
    "centurion":     ("💯", "Centurion", "100 tracked games"),
    "potm":          ("👑", "Player of the Month", "won a monthly award"),
}
EMOJIS = {e for e, _, _ in BADGES.values()}

SCHEMA = """
CREATE TABLE IF NOT EXISTS player_badges (
    guild_id   TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    badge      TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 1,
    earned_at  INTEGER NOT NULL,      -- unix time of the game that (last) earned it
    detail     TEXT,                  -- e.g. "3 goals vs Rival FC"
    PRIMARY KEY (guild_id, discord_id, badge)
);
CREATE TABLE IF NOT EXISTS badge_users (
    guild_id   TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    seeded     INTEGER NOT NULL DEFAULT 0,   -- history already badged (quietly)
    show       TEXT NOT NULL DEFAULT 'auto', -- 'auto' (newest badge), 'none', or a badge key
    nick_note  TEXT,                         -- why the nickname couldn't be changed, if it couldn't
    ea_name    TEXT,                         -- the EA name these badges belong to (re-link = fresh start)
    applied    TEXT,                         -- the exact emoji the bot put on their nickname, if any
    PRIMARY KEY (guild_id, discord_id)
);
"""
USER_FIELDS = ("seeded", "show", "nick_note", "ea_name", "applied")


def init_badges():
    with connect() as conn:
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(badge_users)")}
        for col in ("ea_name", "applied"):
            if col not in have:
                conn.execute(f"ALTER TABLE badge_users ADD COLUMN {col} TEXT")


# --------------------------------------------------------------------------- #
#  Working out what someone has earned
# --------------------------------------------------------------------------- #
def _games(ea_name: str) -> list[dict]:
    with connect() as conn:
        clubs = [r[0] for r in conn.execute("SELECT DISTINCT club_id FROM matches")]
        if not clubs:
            return []
        marks = ",".join("?" for _ in clubs)
        # club_id IN (...) lets SQLite use the (club_id, name) index instead of scanning every row
        rows = conn.execute(f"""
            SELECT mp.club_id, mp.goals, mp.assists, mp.rating, mp.saves, mp.clean_sheet, LOWER(COALESCE(mp.pos,'')) AS pos,
                   m.ts, m.opp_name
            FROM match_players mp JOIN matches m ON m.club_id = mp.club_id AND m.match_id = mp.match_id
            WHERE mp.club_id IN ({marks}) AND mp.name = ? COLLATE NOCASE ORDER BY m.ts, m.match_id""",
                            (*clubs, ea_name)).fetchall()
    return [dict(r) for r in rows]


def _squad_motm_wins(ea_name: str) -> list[int]:
    """Close times of every squad MOTM vote this player won."""
    with connect() as conn:
        rows = conn.execute("""SELECT p.closes_at FROM motm_polls p, json_each(p.winners) w
                               WHERE p.closed=1 AND p.winners IS NOT NULL AND w.value = ? COLLATE NOCASE""",
                            (ea_name,)).fetchall()
    return sorted(r[0] for r in rows)


def _potm_wins(guild_id: str, ea_name: str) -> list[tuple[int, str]]:
    with connect() as conn:
        try:
            rows = conn.execute("SELECT month, data, posted_at FROM monthly_awards WHERE guild_id=?", (guild_id,)).fetchall()
        except Exception:   # awards table not created yet
            return []
    out = []
    for r in rows:
        potm = (json.loads(r["data"]).get("potm") or {}).get("name", "")
        if potm.casefold() == ea_name.casefold():
            out.append((r["posted_at"] or 0, r["month"]))
    return sorted(out)


def evaluate(guild_id: str, ea_name: str) -> dict[str, dict]:
    """{badge: {count, earned_at, detail}} for everything this EA name has earned so far."""
    earned: dict[str, dict] = {}

    def hit(key, ts, detail):
        b = earned.setdefault(key, {"count": 0, "earned_at": ts, "detail": detail})
        b["count"] += 1
        b["earned_at"], b["detail"] = ts, detail

    games = _games(ea_name)
    scoring_run: dict[int, int] = {}
    clean_run: dict[int, int] = {}
    for g in games:
        vs = f"vs {g['opp_name']}" if g["opp_name"] else ""
        if (g["goals"] or 0) >= 3:
            hit("hat_trick", g["ts"], f"{g['goals']} goals {vs}")
        if (g["assists"] or 0) >= 3:
            hit("playmaker", g["ts"], f"{g['assists']} assists {vs}")
        if (g["rating"] or 0) >= 9.0:
            hit("star_man", g["ts"], f"{g['rating']:.1f} rating {vs}")
        if (g["saves"] or 0) >= 8 and g["pos"] == "goalkeeper":
            hit("safe_hands", g["ts"], f"{g['saves']} saves {vs}")
        cid = g["club_id"]
        scoring_run[cid] = scoring_run.get(cid, 0) + 1 if (g["goals"] or 0) > 0 else 0
        if scoring_run[cid] == 5:           # counts once per run, not every game after
            hit("on_fire", g["ts"], "scored in 5 games in a row")
        if g["pos"] in ("goalkeeper", "defender"):
            clean_run[cid] = clean_run.get(cid, 0) + 1 if g["clean_sheet"] else 0
            if clean_run[cid] == 3:
                hit("brick_wall", g["ts"], "3 clean sheets in a row")
    if len(games) >= 100:
        earned["centurion"] = {"count": 1, "earned_at": games[99]["ts"], "detail": "100 tracked games"}
    wins = _squad_motm_wins(ea_name)
    if len(wins) >= 5:
        earned["fan_favourite"] = {"count": len(wins) // 5, "earned_at": wins[(len(wins) // 5) * 5 - 1],
                                   "detail": f"{len(wins)} squad MOTM votes won"}
    potm = _potm_wins(guild_id, ea_name)
    if potm:
        earned["potm"] = {"count": len(potm), "earned_at": potm[-1][0] or int(time.time()),
                          "detail": f"Player of the Month ({potm[-1][1]})"}
    return earned


def stored(guild_id: str, discord_id: str) -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute("SELECT badge, count, earned_at, detail FROM player_badges WHERE guild_id=? AND discord_id=?",
                            (guild_id, discord_id)).fetchall()
    return {r["badge"]: dict(r) for r in rows if r["badge"] in BADGES}


def user_row(guild_id: str, discord_id: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM badge_users WHERE guild_id=? AND discord_id=?", (guild_id, discord_id)).fetchone()
    base = {"seeded": 0, "show": "auto", "nick_note": None, "ea_name": None, "applied": None}
    return {**base, **({k: row[k] for k in row.keys() if k in base} if row else {})}


def set_user(guild_id: str, discord_id: str, **fields):
    current = user_row(guild_id, discord_id)
    current.update(fields)
    with connect() as conn:
        conn.execute(f"""INSERT INTO badge_users (guild_id, discord_id, {", ".join(USER_FIELDS)}) VALUES (?,?,?,?,?,?,?)
                         ON CONFLICT(guild_id, discord_id) DO UPDATE SET
                         {", ".join(f"{f}=excluded.{f}" for f in USER_FIELDS)}""",
                     (guild_id, discord_id, int(current["seeded"]), current["show"], current["nick_note"],
                      current["ea_name"], current["applied"]))


def clear_badges(guild_id: str, discord_id: str):
    """Forget someone's badges (unlinked, or linked to a different EA name)."""
    with connect() as conn:
        conn.execute("DELETE FROM player_badges WHERE guild_id=? AND discord_id=?", (guild_id, discord_id))
    set_user(guild_id, discord_id, seeded=0, ea_name=None)


def users_with_badges(guild_id: str) -> set[str]:
    with connect() as conn:
        return {r[0] for r in conn.execute("SELECT DISTINCT discord_id FROM player_badges WHERE guild_id=?", (guild_id,))}


def refresh(guild_id: str, discord_id: str, ea_name: str) -> tuple[list[tuple[str, dict]], bool]:
    """
    Re-check this player's badges and save them.
    -> ([(badge, info)] that are NEW or went up in count, quiet?) — quiet=True the first time a
       player is checked, so their whole history isn't announced at once.
    """
    row = user_row(guild_id, discord_id)
    if row["ea_name"] and row["ea_name"].casefold() != ea_name.casefold():
        clear_badges(guild_id, discord_id)   # re-linked to another EA name: start again, quietly
        row = user_row(guild_id, discord_id)
    now = evaluate(guild_id, ea_name)
    before = stored(guild_id, discord_id)
    quiet = not row["seeded"]
    changed = [(k, v) for k, v in now.items() if k not in before or v["count"] > before[k]["count"]]
    with connect() as conn:
        for k, v in now.items():
            conn.execute("""INSERT INTO player_badges (guild_id, discord_id, badge, count, earned_at, detail)
                            VALUES (?,?,?,?,?,?) ON CONFLICT(guild_id, discord_id, badge) DO UPDATE SET
                            count=excluded.count, earned_at=excluded.earned_at, detail=excluded.detail""",
                         (guild_id, discord_id, k, v["count"], v["earned_at"], v["detail"]))
    if quiet or not row["ea_name"]:
        set_user(guild_id, discord_id, seeded=1, ea_name=ea_name)
    changed.sort(key=lambda kv: kv[1]["earned_at"])
    return changed, quiet


def display_badge(guild_id: str, discord_id: str) -> Optional[str]:
    """The badge key whose emoji goes on their nickname, or None."""
    show = user_row(guild_id, discord_id)["show"]
    mine = stored(guild_id, discord_id)
    if show == "none" or not mine:
        return None
    if show in mine:
        return show
    return max(mine.items(), key=lambda kv: (kv[1]["earned_at"], kv[0]))[0]   # newest


def badge_line(info: dict, key: str) -> str:
    emoji, name, _ = BADGES[key]
    times = f" ×{info['count']}" if info["count"] > 1 else ""
    return f"{emoji} **{name}**{times}"


# --------------------------------------------------------------------------- #
#  Nicknames
# --------------------------------------------------------------------------- #
NICK_MAX = 32


def _units(text: str) -> int:
    """Length the way Discord counts it (UTF-16), so emoji-heavy names never go over the limit."""
    return len(text.encode("utf-16-le")) // 2


def without_applied(nick: Optional[str], applied: Optional[str]) -> Optional[str]:
    """Their nickname minus the emoji WE added (never touches emojis they chose themselves)."""
    if nick and applied:
        if nick.endswith(" " + applied):
            return nick[: -len(applied) - 1].rstrip() or None
        if nick.endswith(applied):
            return nick[: -len(applied)].rstrip() or None
    return nick


def nickname_for(current_nick: Optional[str], account_name: str, emoji: Optional[str],
                 applied: Optional[str] = None) -> Optional[str]:
    """
    The nickname to set. `applied` is the emoji the bot added last time (stored), so only that is
    swapped or removed. With no emoji to show and nothing added before, the nickname is left as is.
    Returns None to clear the nickname (back to their account name).
    """
    base = without_applied(current_nick, applied)
    if not emoji:
        if not applied:
            return current_nick                      # we never touched it: leave it alone
        return None if (base is None or base == account_name) else base
    name = base or account_name
    suffix = f" {emoji}"
    while name and _units(name) + _units(suffix) > NICK_MAX:
        name = name[:-1]
    name = name.rstrip()
    return f"{name}{suffix}" if name else emoji


def cant_rename_reason(member, me) -> Optional[str]:
    """Why the bot can't change this member's nickname, or None if it can."""
    guild = member.guild
    if member.id == guild.owner_id:
        return "Discord never lets bots rename the server owner."
    if not me.guild_permissions.manage_nicknames:
        return "the bot needs the **Manage Nicknames** permission."
    if member.top_role >= me.top_role:
        return "your highest role is above the bot's — move the bot's role higher in Server Settings → Roles."
    return None
