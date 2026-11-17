"""Fold late EA games into recent saved sessions before creating follow-ups."""
import json
from clubs import club_for, match_key, monitored_clubs
from config import CLUB_ID
from db import connect
import delivery
from performance import timed


def snapshot_players(conn, matches, frozen, links):
    known = {(p.get('club_id', CLUB_ID), p['name'].casefold()): p.get('discord_ids', []) for p in frozen}
    players = []
    for cid in sorted({m['club_id'] for m in matches}):
        club_name = club_for(cid)['name']
        mids = json.dumps([m['match_id'] for m in matches if m['club_id'] == cid])
        group = [dict(r) for r in conn.execute(
            'SELECT name,COUNT(*) games,SUM(goals) goals,SUM(assists) assists,AVG(rating) rating,'
            'SUM(motm) motm,SUM(saves) saves,SUM(tackles_made) tackles,SUM(passes_made) passes,'
            'SUM(pass_attempts) attempts FROM match_players WHERE club_id=? '
            'AND match_id IN (SELECT value FROM json_each(?)) GROUP BY name COLLATE NOCASE', (cid, mids))]
        for p in group:
            key = cid, p['name'].casefold()
            ids = known.get(key, [did for did, name in links.items() if name.casefold() == key[1]])
            p.update(club_id=cid, club_name=club_name, discord_ids=ids)
        players.extend(group)
    return players


@timed('Late session reconciliation', slow=.5)
def reconcile(gid, now, settings):
    from cogs.session_reports import session_activity, START_WINDOW
    from cogs.link import get_all_links
    with connect() as conn:
        revision = str(conn.execute('SELECT revision FROM data_revision WHERE id=1').fetchone()[0])
        previous = conn.execute("SELECT value FROM settings WHERE guild_id=? AND key='reports:reconciled_revision'", (gid,)).fetchone()
        if previous and previous[0] == revision:
            return []
        clubs = [c['club_id'] for c in monitored_clubs()] or [CLUB_ID]
        links = get_all_links(gid)
        records = [dict(r) for r in conn.execute(
            "SELECT h.*,s.starts_at,s.note FROM session_history h JOIN sessions s ON s.id=h.session_id "
            "WHERE h.guild_id=? AND h.outcome='completed' AND h.ended_at>=? ORDER BY s.starts_at,h.session_id", (gid, now-86400))]
        assigned = {mid for r in conn.execute('SELECT match_ids FROM session_history WHERE guild_id=? AND ended_at>=?', (gid, now-3*86400)) for mid in json.loads(r[0])}
        changed = []
        for record in records:
            saved_gap = conn.execute('SELECT value FROM settings WHERE guild_id=? AND key=?', (gid, f"session:gap:{record['session_id']}")).fetchone()
            gap = int(saved_gap[0] if saved_gap else settings['gap']) * 60
            old_ids = set(json.loads(record['match_ids']))
            next_start = conn.execute('SELECT MIN(starts_at) FROM sessions WHERE guild_id=? AND starts_at>? AND cancelled=0', (gid, record['starts_at'])).fetchone()[0]
            matches, _ = session_activity(conn, record, clubs, min(next_start or now+1, now+1), gap,
                                          max(START_WINDOW*60, gap), assigned-old_ids)
            ids = {match_key(m['match_id'], m['club_id']) for m in matches}
            # Never remove historical matches or move matches from another saved session.
            if not old_ids < ids:
                continue
            conn.execute('INSERT INTO session_revision_audit (session_id,data,changed_at) VALUES (?,?,?)',
                         (record['session_id'], json.dumps(record), now))
            players = snapshot_players(conn, matches, json.loads(record['players']), links)
            conn.execute('UPDATE session_history SET match_ids=?,players=?,ended_at=? WHERE session_id=?',
                         (json.dumps(sorted(ids)), json.dumps(players), max(m['ts'] for m in matches), record['session_id']))
            conn.execute('INSERT OR REPLACE INTO settings (guild_id,key,value) VALUES (?,?,?)',
                         (gid, f"session:updated:{record['session_id']}", str(now)))
            if record['message_id'] not in (None, 'suppressed'):
                delivery.enqueue('recap_edit', f"{gid}:{record['session_id']}", gid, CLUB_ID, {'sid': record['session_id']}, conn=conn)
            assigned.update(ids)
            changed.append(record['session_id'])
        conn.execute("INSERT OR REPLACE INTO settings (guild_id,key,value) VALUES (?,'reports:reconciled_revision',?)", (gid, revision))
    return changed
