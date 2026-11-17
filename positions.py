"""Automatic exact-position tracking with separately labelled saved defaults.

Manager lineups/build preferences remain useful planning data, not proof of a
match slot. Exact match values, player confirmations and inferred defaults
retain distinct sources, with no post-match selection workflow.
"""

from clubs import club_id, club_name
import json
import time
from typing import Optional

from cogs.rotation import AREA_NAMES, area_of, broad_role, current_streak, get_recent_positions, is_exact, strip_number
from config import ROTATION_THRESHOLD
from db import connect

# EA bucket -> broad role
EA_BUCKET = {"goalkeeper": "GK", "defender": "DEF", "midfielder": "MID", "forward": "FWD"}

# What players can pick, grouped by EA bucket (so the picker only offers sensible options first)
POSITIONS_BY_ROLE = {
    "GK": ["GK"],
    "DEF": ["RB", "CB", "LB", "RWB", "LWB"],
    "MID": ["CDM", "CM", "CAM", "RM", "LM"],
    "FWD": ["RW", "LW", "ST"],
}
ALL_POSITIONS = [p for ps in POSITIONS_BY_ROLE.values() for p in ps]

# A posted lineup counts for matches played up to this long after it was posted
PLAN_VALID_HOURS = 5


# --------------------------------------------------------------------------- #
#  Posted lineups
# --------------------------------------------------------------------------- #
def save_plan(guild_id: str, formation: str, slots: dict[str, Optional[str]], posted_by: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO lineup_plans (guild_id, club, formation, slots, posted_at, posted_by) VALUES (?,?,?,?,?,?)",
            (guild_id, club_name(), formation, json.dumps({k: v for k, v in slots.items() if v}),
             int(time.time()), posted_by))
        return cur.lastrowid


def plan_for_match(guild_id: str, match_ts: int) -> Optional[dict]:
    """The most recent lineup posted before this match (within PLAN_VALID_HOURS)."""
    when = match_ts or int(time.time())
    with connect() as conn:
        row = conn.execute("""
            SELECT * FROM lineup_plans WHERE guild_id=? AND club=? AND posted_at<=? AND posted_at>=?
            ORDER BY posted_at DESC LIMIT 1""",
            (guild_id, club_name(), when + 60, when - PLAN_VALID_HOURS * 3600)).fetchone()
    if not row:
        return None
    plan = dict(row)
    plan["slots"] = json.loads(plan["slots"])
    return plan


def slot_of(plan: Optional[dict], discord_id: str) -> Optional[str]:
    if not plan:
        return None
    for slot, did in plan["slots"].items():
        if did == discord_id:
            return slot
    return None


# Match slots and preferences are deliberately separate from manager lineups.
from dataclasses import dataclass
from position_types import ea_position, EXACT_ROLES

SOURCE_LABELS={'ea':'EA match','player':'Player confirmed','manual':'Manual',
               'saved':'Saved · inferred','confirmed_default':'Last confirmed · inferred',
               'unknown':'Unknown','lineup':'Legacy lineup','auto':'Legacy inferred'}

def source_label(source):
    return SOURCE_LABELS.get(source,'Legacy record')

@dataclass(frozen=True)
class MatchPosition:
    position: str
    source: str
    ea_role: Optional[str]

def default_event(gid,uid,when=None,cid=None):
    when=int(time.time()) if when is None else when
    with connect() as conn:
        row=conn.execute('SELECT * FROM position_defaults WHERE guild_id=? AND club_id=? AND discord_id=? AND effective_at<=? ORDER BY effective_at DESC,id DESC LIMIT 1',
                         (str(gid),cid if cid is not None else club_id(),str(uid),when)).fetchone()
    return dict(row) if row else None

def save_default(gid,uid,position,cid=None,when=None):
    if position is not None:
        position=position.strip().upper()
        if position not in EXACT_ROLES:
            raise ValueError('Choose a recognised exact position.')
    with connect() as conn:
        conn.execute('INSERT INTO position_defaults (guild_id,club_id,discord_id,position,effective_at) VALUES (?,?,?,?,?)',
                     (str(gid),cid if cid is not None else club_id(),str(uid),position,int(time.time()) if when is None else when))

def confirmed_before(gid,uid,when,role=None):
    with connect() as conn:
        marks=','.join('?' for _ in EXACT_ROLES)
        rows=conn.execute("SELECT r.position,COALESCE(m.ts,CAST(strftime('%s',r.logged_at) AS INTEGER)) stamp FROM rotation_log r "
            "LEFT JOIN matches m ON m.club_id=? AND m.match_id=r.match_id "
            "WHERE r.guild_id=? AND r.club=? AND r.discord_id=? AND r.source IN ('player','manual','ea') "
            f"AND RTRIM(UPPER(r.position),'0123456789') IN ({marks}) "
            "AND COALESCE(m.ts,CAST(strftime('%s',r.logged_at) AS INTEGER))<? ORDER BY stamp DESC,r.id DESC LIMIT 1",
            (club_id(),str(gid),club_name(),str(uid),*EXACT_ROLES,when)).fetchall()
    # Only the last confirmation is a default. Never reach past a newer role
    # change to find an older convenient position in the requested role.
    for row in rows:
        pos=strip_number(row['position'].upper())
        if pos in EXACT_ROLES:
            return pos if role is None or broad_role(pos)==role else None
    return None

def detect_position(gid,uid,ea_pos,match_ts=None,exact_position=None):
    role,exact=ea_position(ea_pos)
    if exact_position in EXACT_ROLES:
        exact=exact_position
        role=EXACT_ROLES[exact]
    if exact:
        return MatchPosition(exact,'ea',role)
    when=int(time.time()) if match_ts is None else match_ts
    if when<=0:
        return MatchPosition('Unknown','unknown',role)
    saved=default_event(gid,uid,when)
    if saved is not None:
        pos=saved['position']
        if pos and (role is None or broad_role(pos)==role):
            return MatchPosition(pos,'saved',role)
        return MatchPosition('Unknown','unknown',role)
    confirmed=confirmed_before(gid,uid,when,role)
    if confirmed:
        return MatchPosition(confirmed,'confirmed_default',role)
    return MatchPosition('Unknown','unknown',role)

def record_position(gid,uid,match_id,result,when):
    with connect() as conn:
        conn.execute('INSERT INTO rotation_log (guild_id,club,discord_id,position,logged_at,source,match_id,ea_role) '
            'SELECT ?,?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM rotation_log WHERE guild_id=? AND club=? AND discord_id=? AND match_id=?)',
            (str(gid),club_name(),str(uid),result.position,when,result.source,match_id,result.ea_role,
             str(gid),club_name(),str(uid),match_id))

def resolve_position(plan,discord_id,ea_pos,guild_id=None,match_ts=None):
    # Compatibility wrapper: a posted lineup is an intention, not match proof.
    result=detect_position(guild_id,discord_id,ea_pos,match_ts)
    return result.position,result.source=='ea'

def previous_game_position(guild_id,discord_id,before_ts):
    return confirmed_before(guild_id,discord_id,before_ts)

def last_exact_position(guild_id,discord_id,role=None):
    return confirmed_before(guild_id,discord_id,int(time.time())+1,role)

def profile_text(gid,uid,cid=None):
    cid=club_id() if cid is None else cid
    from clubs import club_scope
    with club_scope(cid):
        saved=default_event(gid,uid)
        lines=[f"Saved: **{saved['position'] if saved and saved['position'] else 'Not set'}**"]
        with connect() as conn:
            row=conn.execute('SELECT position,source,ea_role FROM rotation_log WHERE guild_id=? AND club=? AND discord_id=? ORDER BY logged_at DESC,id DESC LIMIT 1',
                             (str(gid),club_name(),str(uid))).fetchone()
        if row:
            label='' if row['source']=='unknown' else f" · {source_label(row['source'])}"
            lines.append(f"Latest: **{row['position']}**{label}")
            if row['position']=='Unknown' and row['ea_role']:
                lines.append(f"EA match role: {row['ea_role']}")
        return '\n'.join(lines)

def last_played_club(gid,uid):
    from cogs.link import get_link
    name=get_link(str(gid),str(uid))
    if not name:
        return None
    with connect() as conn:
        row=conn.execute('SELECT m.club_id FROM matches m JOIN match_players p ON p.club_id=m.club_id AND p.match_id=m.match_id '
            'JOIN monitored_clubs c ON c.club_id=m.club_id WHERE p.name=? COLLATE NOCASE ORDER BY m.ts DESC,m.club_id LIMIT 1',(name,)).fetchone()
    return row['club_id'] if row else None

def correct_position(gid,uid,position,match_id='last'):
    """A user correction changes only their chosen club+game, never a default."""
    from cogs.link import get_link
    from cogs.rotation import set_match_position
    from datetime import datetime,timezone
    position=position.upper()
    if position not in EXACT_ROLES:
        raise ValueError('Choose an exact position for a match correction.')
    name=get_link(str(gid),str(uid))
    if not name:
        raise ValueError('Link your EA name through Setup before correcting a match.')
    prefix=f'{club_id()}:'
    if match_id.startswith(prefix):
        match_id=match_id[len(prefix):]
    with connect() as conn:
        args=[club_id(),name]
        extra=''
        if match_id!='last':
            extra=' AND m.match_id=?'
            args.append(match_id)
        row=conn.execute('SELECT m.*,p.pos FROM matches m JOIN match_players p ON p.club_id=m.club_id AND p.match_id=m.match_id '
            'WHERE m.club_id=? AND p.name=? COLLATE NOCASE'+extra+' ORDER BY m.ts DESC,m.match_id DESC LIMIT 1',args).fetchone()
        if not row:
            raise ValueError(f'No matching game for your linked EA player in {club_name()}.')
        prompts=conn.execute('SELECT message_id,pending FROM position_prompts WHERE guild_id=? AND club_id=? AND match_id=?',
                             (str(gid),club_id(),row['match_id'])).fetchall()
        when=datetime.fromtimestamp(row['ts'],timezone.utc).isoformat()
        # Keep the correction, provenance and legacy prompt cleanup atomic.
        cur=conn.execute("UPDATE rotation_log SET position=?,source='player' WHERE guild_id=? AND club=? AND discord_id=? AND match_id=?",
                         (position,str(gid),club_name(),str(uid),row['match_id']))
        role,_=ea_position(row['pos'])
        if cur.rowcount==0:
            conn.execute('INSERT INTO rotation_log (guild_id,club,discord_id,position,logged_at,source,match_id,ea_role) VALUES (?,?,?,?,?,?,?,?)',
                         (str(gid),club_name(),str(uid),position,when,'player',row['match_id'],role))
        for prompt in prompts:
            pending=json.loads(prompt['pending'])
            if pending.pop(str(uid),None) is not None:
                conn.execute('UPDATE position_prompts SET pending=? WHERE message_id=?',(json.dumps(pending),prompt['message_id']))
    return f"Confirmed {position} for {club_name()} · {row['our_goals']}–{row['opp_goals']} vs {row['opp_name']} (match {row['match_id']})."


# --------------------------------------------------------------------------- #
#  Rotation notes for managers
# --------------------------------------------------------------------------- #
def rotation_note(guild_id: str, discord_id: str, builds: list[str]) -> Optional[tuple[str, str]]:
    """
    If this player has just played ROTATION_THRESHOLD+ games in a row in the same
    AREA (Defence / Midfield / Front 3), -> (dedupe key, note). The key changes only
    when a new streak starts, so each streak is mentioned once, not after every game.
    """
    entries = get_recent_positions(guild_id, club_name(), discord_id, limit=15)
    from cogs.rotation import evidence_position
    positions = [evidence_position(e) for e in entries]
    area, run = current_streak(positions)
    if not area or run < ROTATION_THRESHOLD or area == "GK":
        return None   # keepers staying in goal is the plan, not a rotation problem
    key = f"{area}:{entries[run - 1]['logged_at']}"   # oldest game in the streak identifies it
    spots = ", ".join(strip_number(p) if is_exact(p) else "?" for p in positions[:run][::-1])
    # builds in a DIFFERENT area are the real rotation options
    elsewhere = [b for b in builds if area_of(b) not in (area, "GK")]
    elsewhere.sort(key=lambda b: ALL_POSITIONS.index(b) if b in ALL_POSITIONS else 99)
    if elsewhere:
        tip = f"has builds for {', '.join(elsewhere[:3])} → could try **{AREA_NAMES[area_of(elsewhere[0])]}** next"
    else:
        tip = f"only has {AREA_NAMES.get(area, area).lower()} builds set (they can add more under 🛠️ My builds)"
    return key, f"**{AREA_NAMES.get(area, area)}** {run} games in a row ({spots}) — {tip}"
