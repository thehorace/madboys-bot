"""Explicit owner-requested session repair with original snapshots retained for audit."""
import json
import time

import potd
from clubs import club_for
from config import CLUB_ID
from db import connect


def merge_sessions(gid, keep, source):
    from cogs.session_reports import session_matches
    gid = str(gid)
    if keep == source:
        raise ValueError('Choose two different sessions.')
    with connect() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS session_merge_audit (source_id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL, data TEXT NOT NULL, merged_at INTEGER NOT NULL)')
        audit = conn.execute('SELECT * FROM session_merge_audit WHERE source_id=?',(source,)).fetchone()
        records = [conn.execute('SELECT h.*,s.starts_at,s.note FROM session_history h JOIN sessions s ON s.id=h.session_id WHERE h.guild_id=? AND h.session_id=?',(gid,sid)).fetchone() for sid in (keep,source)]
        if not all(records):
            raise ValueError('Both finished sessions must belong to this server.')
        target, old_source = [dict(r) for r in records]
        if audit:
            if audit['target_id'] != keep:
                raise ValueError('That session has already been merged into a different session.')
            saved=json.loads(audit['data'])
            if target['outcome'] != 'completed':
                raise ValueError('The destination session is no longer available for repair.')
            result=conn.execute('SELECT data,message_id FROM session_potd WHERE session_id=?',(keep,)).fetchone()
            return {'target':target,'source':saved['source'],'old_potd':saved['potd'],'result':json.loads(result[0]) if result else None,'award_message_id':result[1] if result else None}
        if any(r['outcome'] != 'completed' for r in records):
            raise ValueError('Only completed sessions can be merged.')
        if target['starts_at'] > old_source['starts_at'] or old_source['starts_at']-target['starts_at'] > 12*3600:
            raise ValueError('Keep the earlier session; both sessions must be within the same 12-hour evening.')
        old_potd=[dict(r) for r in conn.execute('SELECT * FROM session_potd WHERE session_id IN (?,?)',(keep,source))]
    ids=list(dict.fromkeys(json.loads(target['match_ids'])+json.loads(old_source['match_ids'])))
    if not ids:
        raise ValueError('There are no recorded matches to merge.')
    matches=session_matches({'match_ids':json.dumps(ids)})
    if len(matches) != len(ids):
        raise ValueError('Some saved matches are missing; the sessions have not been changed.')
    linked={}
    for record in (target,old_source):
        for p in json.loads(record['players']):
            linked.setdefault((p.get('club_id',CLUB_ID),p['name'].casefold()),set()).update(p.get('discord_ids',[]))
    players=[]
    with connect() as conn:
        for cid in sorted({r['club_id'] for r in matches}):
            mids=[r['match_id'] for r in matches if r['club_id']==cid]
            group=[dict(r) for r in conn.execute('SELECT name,COUNT(*) games,SUM(goals) goals,SUM(assists) assists,AVG(rating) rating,SUM(motm) motm,SUM(saves) saves,SUM(tackles_made) tackles,SUM(passes_made) passes,SUM(pass_attempts) attempts FROM match_players WHERE club_id=? AND match_id IN (SELECT value FROM json_each(?)) GROUP BY name COLLATE NOCASE',(cid,json.dumps(mids)))]
            for p in group:
                p.update(club_id=cid,club_name=club_for(cid)['name'],discord_ids=sorted(linked.get((cid,p['name'].casefold()),set())))
            players.extend(group)
    result=potd.compute(matches)
    rsvps={r['discord_id']:r for r in json.loads(old_source['rsvps'])}
    rsvps.update({r['discord_id']:r for r in json.loads(target['rsvps'])})
    if result:
        name=result['winner']['name'].casefold()
        did=next((d for p in players if p['name'].casefold()==name for d in p['discord_ids']),None)
    else:
        did=None
    old_target_potd=next((p for p in old_potd if p['session_id']==keep and p['message_id']),None) or next((p for p in old_potd if p['message_id']),None)
    with connect() as conn:
        conn.execute('INSERT INTO session_merge_audit VALUES (?,?,?,?)',(source,keep,json.dumps({'target':target,'source':old_source,'potd':old_potd}),int(time.time())))
        conn.execute('UPDATE session_history SET ended_at=?,match_ids=?,players=?,rsvps=? WHERE session_id=?',
                     (max(target['ended_at'],old_source['ended_at']),json.dumps(ids),json.dumps(players),json.dumps(list(rsvps.values())),keep))
        conn.execute("UPDATE session_history SET outcome='merged',match_ids='[]',players='[]' WHERE session_id=?",(source,))
        conn.execute("UPDATE session_summary_dms SET status='suppressed' WHERE session_id=? AND status='pending'",(source,))
        conn.execute('DELETE FROM settings WHERE guild_id=? AND key IN (?,?)',(gid,f'potd:pending:{keep}',f'potd:pending:{source}'))
        conn.execute('DELETE FROM session_potd WHERE session_id IN (?,?)',(keep,source))
        if result:
            w=result['winner']
            conn.execute('INSERT INTO session_potd (session_id,guild_id,club_id,name,discord_id,score,data,message_id,posted_at) VALUES (?,?,?,?,?,?,?,?,?)',
                (keep,gid,w['club_id'],w['name'],did,w['score'],json.dumps(result),old_target_potd['message_id'] if old_target_potd else None,int(time.time())))
        target=dict(conn.execute('SELECT h.*,s.starts_at,s.note FROM session_history h JOIN sessions s ON s.id=h.session_id WHERE h.session_id=?',(keep,)).fetchone())
    return {'target':target,'source':old_source,'old_potd':old_potd,'result':result,'award_message_id':old_target_potd['message_id'] if old_target_potd else None}
