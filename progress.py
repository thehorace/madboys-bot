"""Read-only progress screens based on saved matches and the actual session rules."""
import json
import time

import discord

from clubs import club_for, monitored_clubs
from config import CLUB_ID, CLUB_COLOUR
from db import connect

WEEK = 7 * 86400
MIN_GAMES = 3


def weekly_data(cid, player=None, now=None):
    now = int(now if now is not None else time.time())
    with connect() as conn:
        if player:
            rows = conn.execute('SELECT m.ts,m.result,SUM(p.goals) goals,SUM(p.assists) assists,AVG(p.rating) rating '
                'FROM matches m JOIN match_players p ON p.club_id=m.club_id AND p.match_id=m.match_id '
                'WHERE m.club_id=? AND p.name=? COLLATE NOCASE AND m.ts>=? AND m.ts<? GROUP BY m.match_id',
                (cid, player, now-2*WEEK, now)).fetchall()
        else:
            rows = conn.execute('SELECT ts,result,our_goals goals FROM matches WHERE club_id=? AND ts>=? AND ts<?',
                                (cid, now-2*WEEK, now)).fetchall()
    def summary(group):
        ratings = [r['rating'] for r in group if player and r['rating'] is not None]
        games = len(group)
        return {'games':games, 'wins':sum(r['result']=='W' for r in group),
                'draws':sum(r['result']=='D' for r in group), 'losses':sum(r['result']=='L' for r in group),
                'goals':sum(r['goals'] or 0 for r in group),
                'assists':sum((r['assists'] or 0) for r in group) if player else None,
                'rating':sum(ratings)/len(ratings) if ratings else None, 'rated':len(ratings)}
    return {'now':now, 'current':summary([r for r in rows if r['ts']>=now-WEEK]),
            'previous':summary([r for r in rows if r['ts']<now-WEEK])}


def weekly_embed(cid, player=None, now=None):
    data = weekly_data(cid,player,now)
    current, previous = data['current'],data['previous']
    label = discord.utils.escape_markdown(player[:80]) if player else 'Club overview'
    embed = discord.Embed(title=f'📈 Weekly Progress · {club_for(cid)["name"]}'[:256], colour=CLUB_COLOUR,
        description=f'**{label}**\nLast 7 days compared with the previous 7 days.')
    for name, stats in [('Last 7 days',current),('Previous 7 days',previous)]:
        embed.add_field(name=name,value=f"**{stats['games']} matches**\n{stats['wins']} wins · {stats['draws']} draws · {stats['losses']} losses\n"
                        f"{stats['goals']} goals" + (f" · {stats['assists']} assists" if player else ''),inline=True)
    if min(current['games'],previous['games']) < MIN_GAMES:
        embed.add_field(name='Building your comparison',value=f'Comparisons need at least {MIN_GAMES} recorded matches in each period. Your totals are shown above.',inline=False)
    else:
        metrics = [('Win rate',100*current['wins']/current['games'],100*previous['wins']/previous['games'],'pp')]
        metrics.append(('Goals per match',current['goals']/current['games'],previous['goals']/previous['games'],''))
        if player:
            metrics.append(('Assists per match',current['assists']/current['games'],previous['assists']/previous['games'],''))
            if min(current['rated'],previous['rated']) >= MIN_GAMES:
                metrics.append(('Average rating',current['rating'],previous['rating'],''))
        lines=[]
        for name,new,old,unit in metrics:
            delta=new-old
            arrow='⬆️' if delta>0.005 else '⬇️' if delta< -0.005 else '➖'
            suffix='%' if unit=='pp' else ''
            change_unit=' percentage points' if unit=='pp' else ''
            lines.append(f'{arrow} **{name}**: {old:.2f}{suffix} → {new:.2f}{suffix} ({delta:+.2f}{change_unit})')
        embed.add_field(name='What changed',value='\n'.join(lines),inline=False)
        if player and min(current['rated'],previous['rated']) < MIN_GAMES:
            embed.add_field(name='Rating comparison',value='Needs 3 rated matches in each period.',inline=False)
    embed.set_footer(text='Saved EA matches only • Per-match comparisons • Clubs kept separate')
    return embed


def session_embed(gid, now=None, in_voice=False, tracker_ok=None, tracker_running=True, next_check=None):
    from cogs.session_reports import report_settings, session_activity, START_WINDOW, VOICE_HOLD, result_words
    now = int(now if now is not None else time.time())
    gid = str(gid)
    settings = report_settings(gid)
    clubs = monitored_clubs() or [club_for(CLUB_ID)]
    with connect() as conn:
        session = conn.execute('SELECT * FROM sessions s WHERE guild_id=? AND cancelled=0 AND starts_at<=? '
            'AND NOT EXISTS (SELECT 1 FROM session_history h WHERE h.session_id=s.id) ORDER BY starts_at DESC,id DESC LIMIT 1',(gid,now)).fetchone()
        if session is None:
            upcoming = conn.execute('SELECT starts_at FROM sessions WHERE guild_id=? AND cancelled=0 AND starts_at>? ORDER BY starts_at LIMIT 1',(gid,now)).fetchone()
            last = conn.execute('SELECT h.session_id,h.outcome,h.message_id FROM session_history h WHERE guild_id=? ORDER BY ended_at DESC,session_id DESC LIMIT 1',(gid,)).fetchone()
            desc = f'Next session: <t:{upcoming[0]}:F>.' if upcoming else 'No session is currently open.'
            if last:
                desc += f"\nLast session: **#{last['session_id']} · {last['outcome']}**. Open Session history for its recap."
            return discord.Embed(title='⏱️ Session Progress',description=desc,colour=CLUB_COLOUR)
        next_start = conn.execute('SELECT MIN(starts_at) FROM sessions WHERE guild_id=? AND starts_at>? AND cancelled=0',(gid,session['starts_at'])).fetchone()[0]
        assigned = {mid for r in conn.execute('SELECT match_ids FROM session_history WHERE guild_id=? AND ended_at>=?',(gid,session['starts_at']-2*86400)) for mid in json.loads(r[0])}
        gap = settings['gap']*60
        start_window=max(START_WINDOW*60,gap)
        matches,last_activity = session_activity(conn,session,[c['club_id'] for c in clubs],min(next_start or now+1,now+1),gap,start_window,assigned)
    deadline = last_activity+gap if last_activity is not None else session['starts_at']+start_window
    if not matches:
        deadline=max(deadline,session['starts_at']+start_window)
    held = in_voice and last_activity is not None and now-last_activity < VOICE_HOLD
    if held:
        deadline=max(deadline,last_activity+VOICE_HOLD)
    state = 'Waiting for the first recorded game' if last_activity is None else 'Session activity recorded'
    reason = f'Recap expected after <t:{deadline}:t> (<t:{deadline}:R>), if there is no further activity.'
    if last_activity is None:
        reason=f'Waiting until <t:{deadline}:t> for a first recorded game. Sessions without games close without a match recap.'
    if held:
        state='Held open by voice activity'
        reason=f'The squad is still together in voice. Recap can finish sooner if they leave; current hold ends <t:{deadline}:R>.'
    if not tracker_running:
        state='Waiting for the match tracker'
        reason='The tracker is unavailable or stopped. A successful check is needed before a recap can finish.'
    elif deadline<=now:
        state='Waiting for a successful EA check' if tracker_ok is not True else 'Ready for the next recap check'
        reason='The inactivity window has passed. The next eligible tracker check can finish this session.'
    embed=discord.Embed(title='⏱️ Session Progress',description=f"**{state}**\nSession #{session['id']} · <t:{session['starts_at']}:f>\n\n{reason}",colour=CLUB_COLOUR)
    embed.add_field(name='Recorded results',value=f"{len(matches)} counted matches · {result_words(matches)}",inline=False)
    embed.add_field(name='Latest recorded activity',value=f'<t:{last_activity}:f> (<t:{last_activity}:R>)' if last_activity is not None else 'No completed match or forfeit recorded yet.',inline=False)
    for club in clubs:
        games=[m for m in matches if m['club_id']==club['club_id']]
        if games:
            embed.add_field(name=club['name'],value=f'{len(games)} matches · {result_words(games)}',inline=True)
    if tracker_ok is not True and tracker_running and deadline>now:
        embed.add_field(name='EA check',value='No complete eligible check yet; recap timing may be delayed.',inline=False)
    if not settings['enabled']:
        embed.add_field(name='Recap posting is off',value='Session history still saves, but no automatic club recap will be posted.',inline=False)
    if next_check and tracker_running:
        embed.add_field(name='Next scheduled check',value=f'<t:{int(next_check)}:R>',inline=False)
    embed.set_footer(text='Estimates update on Refresh • EA reports completed games, not live matches • Forfeits extend activity without counting as results')
    return embed
