"""Durable, idempotently keyed delivery jobs with bounded exponential backoff."""
import asyncio
import json
import logging
import time
from db import connect

log = logging.getLogger('madboys-bot.delivery')

def enqueue(kind, key, gid, cid, payload, *, lifetime=86400, conn=None):
    args=(kind,str(key),str(gid),int(cid),json.dumps(payload),int(time.time()),int(time.time())+lifetime)
    sql='INSERT OR IGNORE INTO delivery_jobs (kind,job_key,guild_id,club_id,payload,queued_at,expires_at) VALUES (?,?,?,?,?,?,?)'
    if conn is not None:
        conn.execute(sql,args)
    else:
        with connect() as db:
            db.execute(sql,args)

def get(kind,key):
    with connect() as conn:
        row=conn.execute('SELECT * FROM delivery_jobs WHERE kind=? AND job_key=?',(kind,str(key))).fetchone()
    return dict(row) if row else None

def due(job):
    return job and job['next_attempt']<=time.time()<job['expires_at']

def ack(kind,key):
    with connect() as conn:
        conn.execute('DELETE FROM delivery_jobs WHERE kind=? AND job_key=?',(kind,str(key)))

def fail(job,error):
    attempts=job['attempts']+1
    delay=min(1800,30*2**min(attempts-1,6))
    with connect() as conn:
        conn.execute('UPDATE delivery_jobs SET attempts=?,next_attempt=?,last_error=? WHERE kind=? AND job_key=?',
                     (attempts,int(time.time())+delay,f'{type(error).__name__}: {error}'[:300],job['kind'],job['job_key']))

async def dispatch(kinds, handler):
    marks=','.join('?' for _ in kinds)
    def read():
        with connect() as conn:
            conn.execute(f'DELETE FROM delivery_jobs WHERE kind IN ({marks}) AND expires_at<=?',(*kinds,int(time.time())))
            return [dict(r) for r in conn.execute(f'SELECT * FROM delivery_jobs WHERE kind IN ({marks}) AND next_attempt<=? ORDER BY queued_at,job_key LIMIT 25',(*kinds,int(time.time())))]
    for job in await asyncio.to_thread(read):
        try:
            await handler(job,json.loads(job['payload']))
        except Exception as exc:
            await asyncio.to_thread(fail,job,exc)
            log.warning('%s delivery failed; retry scheduled (%s)',job['kind'],type(exc).__name__)
        else:
            await asyncio.to_thread(ack,job['kind'],job['job_key'])

def status(gid):
    now=int(time.time())
    with connect() as conn:
        rows=[dict(r) for r in conn.execute("SELECT kind,COUNT(*) count,MIN(queued_at) oldest,MIN(next_attempt) next_attempt,MAX(last_error) last_error FROM delivery_jobs WHERE guild_id=? AND expires_at>? AND kind NOT IN ('result','recap','dm') GROUP BY kind",(str(gid),now))]
        for kind,sql in (
            ('results','SELECT COUNT(*) count,MIN(queued_at) oldest FROM matchday_outbox WHERE guild_id=? AND queued_at>?'),
            ('recaps',"SELECT COUNT(*) count,MIN(ended_at) oldest FROM session_history WHERE guild_id=? AND message_id IS NULL AND outcome='completed' AND ended_at>?"),
            ('personal DMs',"SELECT COUNT(*) count,MIN(h.ended_at) oldest FROM session_summary_dms d JOIN session_history h ON h.session_id=d.session_id WHERE h.guild_id=? AND d.status='pending' AND h.ended_at>?")):
            row=conn.execute(sql,(str(gid),now-86400)).fetchone()
            if row['count']:
                rows.append({'kind':kind,**dict(row)})
        retry=conn.execute('SELECT MIN(CASE WHEN next_attempt<=? THEN ? ELSE next_attempt END) FROM delivery_jobs WHERE guild_id=? AND expires_at>?',(now,now,str(gid),now)).fetchone()[0]
    return rows,retry
