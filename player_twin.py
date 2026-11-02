"""Playful style archetypes, not real-world statistical scouting or ability ratings.

Targets and scales below are hand-tuned EA gameplay heuristics. They are NOT
measurements of the named footballers. Historical style sources are linked in UI.
"""
import io
from collections import Counter

import discord
from PIL import Image, ImageDraw

from awards import _fit
from config import CLUB_COLOUR
from db import connect
from fonts import font

MIN_GAMES, WINDOW = 5, 20
ROLES = {'goalkeeper':'GK','defender':'DEF','midfielder':'MID','forward':'FWD',
         'gk':'GK','def':'DEF','mid':'MID','fwd':'FWD'}
LABELS = {'GK':'Goalkeeper','DEF':'Defender','MID':'Midfielder','FWD':'Forward'}
# name, archetype, editorial description, normalized synthetic targets, source
PROFILES = {
 'FWD': [
  ('Erling Haaland','The finisher','A goal-focused attacking style.',{'goals':.95,'creation':.15,'circulation':.25,'precision':.6},'https://beta.mancity.com/news/mens/erling-haaland-200-manchester-city-appearances-63922654'),
  ('Harry Kane','The linking striker','Scoring mixed with setting up teammates.',{'goals':.65,'creation':.9,'circulation':.65,'precision':.75},'https://www.premierleague.com/en/news/2513673')],
 'MID': [
  ('Kevin De Bruyne','The creator','An assist-focused midfield style.',{'goals':.4,'creation':.95,'circulation':.65,'precision':.7,'defence':.3},'https://live.mancity.com/news/mens/kevin-de-bruyne-201920-passing-stats-63731545'),
  ('Rodri','The midfield metronome','Passing involvement and ball-winning from midfield.',{'goals':.15,'creation':.2,'circulation':.95,'precision':.95,'defence':.7},'https://live.mancity.com/citytv/mens/masterclass-rodri-63807147'),
  ('Frank Lampard','The scoring midfielder','A midfield style with a bigger goal threat.',{'goals':.85,'creation':.4,'circulation':.55,'precision':.7,'defence':.3},'https://www.chelseafc.com/en/news/article/chelseas-team-of-120-midfielder-frank-lampard')],
 'DEF': [
  ('Virgil van Dijk','The passing centre-back','A defender involved in moving the ball.',{'goals':.15,'creation':.1,'circulation':.9,'precision':.9,'defence':.5},'https://www.liverpoolfc.com/international-academy/news/how-pass-ball-virgil-van-dijk?amp=1'),
  ('Trent Alexander-Arnold','The creative defender','A defensive role with attacking supply.',{'goals':.2,'creation':.85,'circulation':.8,'precision':.7,'defence':.4},'https://www.liverpoolfc.com/news/first-team/403093-trent-alexander-arnold-breaks-his-own-premier-league-assist-record'),
  ('Rúben Dias','The defensive stopper','A defence-first statistical pattern.',{'goals':.05,'creation':.05,'circulation':.5,'precision':.7,'defence':.95},'https://www.uefa.com/uefachampionsleague/clubs/players/250058220--ruben-dias/')],
 'GK': [
  ('Thibaut Courtois','The shot-stopper','A keeping style driven by saves.',{'saving':.95,'circulation':.25,'precision':.6},'https://www.uefa.com/uefachampionsleague/news/0275-15415a46222f-56deb06b7df5-1000--player-of-the-match-thibaut-courtois/'),
  ('Marc-André ter Stegen','The passing goalkeeper','A goalkeeper involved in distribution.',{'saving':.6,'circulation':.85,'precision':.95},'https://www.fcbarcelona.com/en/news/758997/ter-stegen-demonstrates-his-ability-with-his-feet')]
}
# More named players, with distinct patterns built only from available EA stats.
# Names such as Mbappé/Neuer do not enable pace/sweeping claims: those actions
# aren't recorded, so their comparisons deliberately describe narrower traits.
EXTRA_PROFILES = {
 'FWD': [
  ('Kylian Mbappé','The scoring forward','Strong goal output with some attacking supply.',{'goals':.85,'creation':.4,'circulation':.4,'precision':.65},'https://www.realmadrid.com/en-US/news/football/first-team/latest-news/mbappe-jugador-mahou-cinco-estrellas-de-la-temporada-27-05-2026'),
  ('Lionel Messi','The scorer-creator','Goals, assists and passing involvement together.',{'goals':.8,'creation':.85,'circulation':.9,'precision':.95},'https://www.fcbarcelona.com/en/card/2214377/leo-messi'),
  ('Cristiano Ronaldo','The goal-led attacker','A scoring-heavy attacking pattern.',{'goals':1,'creation':.3,'circulation':.3,'precision':.6},'https://www.realmadrid.com/en-US/the-club/history/football-legends/cristiano-ronaldo-dos-santos-aveiro'),
  ('Mohamed Salah','The goal-involvement forward','Scoring and supplying goals in attack.',{'goals':.8,'creation':.65,'circulation':.55,'precision':.7},'https://www.liverpoolfc.com/news/record-breaker-watch-all-mohamed-salahs-pl-goals-and-assists-liverpool')],
 'MID': [
  ('Pedri','The passing technician','A passing-led midfield pattern with some creativity.',{'goals':.15,'creation':.45,'circulation':.95,'precision':.95,'defence':.25},'https://www.fcbarcelona.fr/fr/football/equipe-premiere/actualites/4345078/five-years-since-pedris-presentation-as-a-bara-player'),
  ('Jude Bellingham','The all-round midfield threat','Goal contribution and tackling alongside midfield passing.',{'goals':.7,'creation':.55,'circulation':.65,'precision':.7,'defence':.6},'https://www.fifa.com/en/tournaments/mens/club-world-cup/usa-2025/articles/jude-bellingham-real-madrid-creator-or-goalscorer'),
  ('Bruno Fernandes','The chance supplier','Assist output with supporting midfield involvement.',{'goals':.35,'creation':.95,'circulation':.5,'precision':.6,'defence':.55},'https://www.manutd.com/en/teams/mens-team/bruno-fernandes'),
  ("N’Golo Kanté",'The ball-winning midfielder','A tackle-led midfield pattern.',{'goals':.05,'creation':.1,'circulation':.65,'precision':.75,'defence':1},'https://www.chelseafc.com/en/news/article/chelseas-team-of-120-midfielder-ngolo-kante')],
 'DEF': [
  ('William Saliba','The balanced centre-back','Passing involvement balanced with defensive work.',{'goals':.05,'creation':.1,'circulation':.75,'precision':.95,'defence':.75},'https://www.premierleague.com/en/players/462424/william-saliba/career'),
  ('Achraf Hakimi','The attacking defender','Goals and assists alongside defensive involvement.',{'goals':.45,'creation':.6,'circulation':.65,'precision':.7,'defence':.6},'https://www.psg.fr/en/content/achraf-hakimi-200-percent-psg-club-portrait-match-ligue-1-ucl-paris-saint-germain-morocco-2025-2026')],
 'GK': [
  ('Manuel Neuer','The keeper-playmaker','Saves mixed with heavy passing involvement.',{'saving':.8,'circulation':.95,'precision':.8},'https://www.uefa.com/uefachampionsleague/clubs/players/97923--manuel-neuer/'),
  ('Alisson Becker','The balanced goalkeeper','A keeping pattern combining saves and distribution.',{'saving':.8,'circulation':.55,'precision':.8},'https://www.liverpoolfc.com/teams/mens-team/alisson-becker')]
}
for _role, _profiles in EXTRA_PROFILES.items():
    PROFILES[_role].extend(_profiles)
from twin_roster import PROFILES as SUPPORT_PROFILES
for _role, _profiles in SUPPORT_PROFILES.items():
    PROFILES[_role].extend(_profiles)
WEIGHTS = {'goals':2,'creation':2,'circulation':1,'precision':.5,'defence':2,'saving':2}


def roster_names(query=''):
    """Search both clubs' saved EA names, including players outside a live roster."""
    with connect() as conn:
        names=[r[0] for r in conn.execute('SELECT MIN(p.name) FROM match_players p JOIN monitored_clubs c ON c.club_id=p.club_id GROUP BY LOWER(p.name) ORDER BY LOWER(p.name)')]
    return [n for n in names if query.casefold() in n.casefold()]


def resolve_name(query):
    if not query:
        return None,[]
    names=roster_names(query.strip())
    exact=next((n for n in names if n.casefold()==query.strip().casefold()),None)
    return (exact or names[0],[]) if exact or len(names)==1 else (None,names)


def _analyse_sample(name, recent):
    if not recent:
        return f'No saved matches for **{discord.utils.escape_markdown(name[:80])}** yet.'
    counts = Counter(ROLES.get((r['pos'] or '').lower()) for r in recent)
    counts.pop(None,None)
    if not counts:
        return 'Your recent matches have no recognised EA position data yet. I need that to choose a fair comparison.'
    role = counts.most_common(1)[0][0]
    games = [r for r in recent if ROLES.get((r['pos'] or '').lower())==role]
    if len(games)<MIN_GAMES:
        return f'Your most-played recent role is **{LABELS[role]}** ({len(games)} games). Play at least {MIN_GAMES} games in that role for a Player Twin.'
    n = len(games)
    def total(key):
        return sum(max(0,r.get(key) or 0) for r in games)
    stats = {'goals':total('goals')/n,'creation':total('assists')/n,'circulation':total('passes_made')/n,
             'defence':total('tackles_made')/n,'saving':total('saves')/n,
             'precision':min(1,total('passes_made')/total('pass_attempts')) if total('pass_attempts')>=10 else None}
    ratings=[r['rating'] for r in games if r['rating'] is not None]
    rating=sum(ratings)/len(ratings) if ratings else None
    form=[]
    if rating is not None:
        form.append(f"{'Rough patch' if rating<6 else 'Finding your rhythm' if rating<7 else 'Solid spell' if rating<8 else 'Flying form'} · {rating:.2f} average EA rating")
    if role=='FWD' and total('shots')>=10 and total('goals')<=total('shots'):
        conversion=total('goals')/total('shots')
        form.append(f"{'Finishing has not clicked yet' if conversion<.15 else 'Shot return'} · {conversion:.0%} of recorded shots became goals")
    if stats['precision'] is not None and total('pass_attempts')>=50 and stats['precision']<.65:
        form.append(f"Passing has been patchy · {stats['precision']:.0%} accuracy")
    scales = {'goals':1.5,'creation':1,'circulation':35,'defence':5,'saving':6}
    if role == 'DEF':
        scales.update(circulation=25, defence=2)
    weights = dict(WEIGHTS)
    if role == 'DEF':
        weights.update(goals=.5, creation=1, circulation=2, defence=3, precision=1)
    features = {key:min(1,value/scales[key]) if key in scales else max(0,min(1,(value-.6)/.35))
                for key,value in stats.items() if value is not None}
    profiles = PROFILES[role]
    relevant = set(profiles[0][3])
    if not any(stats[k] for k in relevant if k!='precision'):
        return 'I have the appearances, but not enough recorded actions to identify a playing style yet.'
    def distance(profile):
        targets = profile[3]
        keys = sorted(targets.keys() & features.keys())
        return sum(weights[k]*(features[k]-targets[k])**2 for k in keys)/sum(weights[k] for k in keys)
    ranked = sorted(profiles,key=distance)
    evidence = []
    units = {'goals':'goals per game','creation':'assists per game','circulation':'completed passes per game',
             'defence':'successful tackles per game','saving':'saves per game'}
    for key in sorted(relevant-{'precision'},key=lambda k:(-features.get(k,0)*weights[k], k)):
        if stats[key]>0:
            evidence.append(f"{stats[key]:.2f} {units[key]}")
    if stats['precision'] is not None:
        evidence.append(f"{stats['precision']:.0%} passing accuracy")
    return {'name':recent[0]['name'],'role':role,'games':n,'recent_games':len(recent),'stats':stats,
            'matches':ranked,'scores':{p[0]:distance(p) for p in ranked},'evidence':evidence[:3],'form':form[:2],
            'confidence':'Early read' if n<10 else 'Taking shape' if n<15 else 'More settled sample'}


# Explicit club joke, not an inferred match or a change to recorded EA stats.
CLUB_PICKS = {'fauzan0410': 'Kylian Mbappé'}


def _samples():
    """One snapshot for the entire shared squad, capped per EA identity.

    Read both clubs together so the same person never reserves two twins.
    Duplicate persona rows are removed before applying the appearance limit.
    """
    with connect() as conn:
        rows = conn.execute('''
            SELECT * FROM (
                SELECT p.*, m.ts, ROW_NUMBER() OVER (
                    PARTITION BY LOWER(p.name)
                    ORDER BY m.ts DESC, p.club_id, p.match_id, p.persona_id
                ) AS recent_index
                FROM match_players p
                JOIN monitored_clubs c ON c.club_id=p.club_id
                JOIN matches m ON m.club_id=p.club_id AND m.match_id=p.match_id
                WHERE p.persona_id=(SELECT MIN(q.persona_id) FROM match_players q
                    WHERE q.club_id=p.club_id AND q.match_id=p.match_id
                    AND q.name=p.name COLLATE NOCASE)
            ) WHERE recent_index<=? ORDER BY LOWER(name), recent_index
        ''', (WINDOW,)).fetchall()
    samples = {}
    for row in rows:
        samples.setdefault(row['name'].casefold(), []).append(dict(row))
    return samples


def analyse(name):
    if not name:
        return 'Link your EA player through Setup first, or choose a player with `/playertwin`.'
    samples = _samples()
    key = name.casefold()
    result = _analyse_sample(name, samples.get(key, []))
    if isinstance(result, str):
        return result
    results = {n: result if n == key else _analyse_sample(rows[0]['name'], rows) for n, rows in samples.items()}
    results = {n: r for n, r in results.items() if isinstance(r, dict)}
    used, assignments = set(), {}
    for n, twin in CLUB_PICKS.items():
        if n in results:
            profile = next(p for group in PROFILES.values() for p in group if p[0] == twin)
            used.add(twin)
            assignments[n] = profile
    # Give the strongest statistical claims first choice. Ties use sample size
    # and EA name, so lookup order, process restarts and SQL order cannot decide.
    def priority(item):
        n, r = item
        scores = sorted(r['scores'].values())
        return (-round(scores[1]-scores[0], 12), -r['games'], n)
    for n, r in sorted(results.items(), key=priority):
        if n in assignments:
            continue
        available = next((p for p in r['matches'] if p[0] not in used), None)
        if available:
            assignments[n] = available
            used.add(available[0])
    selected = assignments.get(key)
    if selected is None:
        return (f"All {len(PROFILES[result['role']])} {LABELS[result['role']].lower()} twins are already "
                'assigned in the shared squad. More profiles are needed to give you a unique twin.')
    result['matches'] = [selected] + [p for p in result['matches'] if p[0] != selected[0]]
    result['assignment'] = 'Unique squad twin'
    result['reassigned'] = selected[0] != min(result['scores'], key=result['scores'].get)
    return result


def build_embed(result):
    best=result['matches'][0]
    name=discord.utils.escape_markdown(discord.utils.escape_mentions(result['name'][:80]))
    intro = 'your squad style twin is' if result.get('assignment') else 'your closest style inspiration is'
    embed=discord.Embed(title='🪞 Your Player Twin',colour=CLUB_COLOUR,
        description=f"**{name}**, {intro}…\n\n**{best[0]}**\n*{best[1]}* · {best[2]}")
    if result.get('assignment'):
        explanation = 'One primary twin per EA player across both clubs. A fun style inspiration, not an ability rating.'
        embed.add_field(name=result['assignment'], value=explanation, inline=False)
    embed.add_field(name='Your recorded signature',value='\n'.join(f'• {line}' for line in result['evidence']),inline=False)
    if result.get('form'):
        embed.add_field(name='Your current spell',value='\n'.join(result['form'])+'\nForm describes your saved games, not the real player’s ability.',inline=False)
    others=result['matches'][1:3]
    if others and not result.get('assignment'):
        embed.add_field(name='Also gives shades of…',value='\n'.join(f'**{p[0]}** — {p[1].lower()}' for p in others),inline=False)
    embed.add_field(name='The sample',value=f"{result['games']} {LABELS[result['role']].lower()} games from your latest {result['recent_games']} appearances across the clubs. **{result['confidence']}**.",inline=False)
    embed.add_field(name='For fun, not a scouting verdict',value=f"Curated style archetypes, not real-player stat matching or an ability rating. Pace, dribbling and off-ball movement aren't assessed. [Style inspiration]({best[4]})",inline=False)
    embed.set_image(url='attachment://result.png')
    return embed


def render_card(result):
    image=Image.new('RGB',(1200,630),(14,23,43))
    draw=ImageDraw.Draw(image)
    gold=(255,204,95)
    draw.rectangle((0,0,1200,8),fill=gold)
    draw.text((50,35),'YOUR PLAYER TWIN',font=font(28,True),fill=gold)
    label,f=_fit(draw,f"{result['name']}  /  {LABELS[result['role']]}",1100,30)
    draw.text((50,91),label,font=f,fill=(180,192,211))
    best=result['matches'][0]
    title,f=_fit(draw,best[0],1100,72)
    draw.text((50,171),title,font=f,fill=(245,246,250))
    draw.text((50,268),best[1],font=font(34,True),fill=gold)
    for i,line in enumerate(result['evidence']):
        label,f=_fit(draw,line,1080,29)
        draw.text((50,339+i*49),label,font=f,fill=(219,225,235))
    if result.get('form'):
        label,f=_fit(draw,result['form'][0],1100,20)
        draw.text((50,491),label,font=f,fill=gold)
    draw.text((50,526),f"{result['games']} games in role  /  {result['confidence']}",font=font(23),fill=(180,192,211))
    footer = 'Unique squad twin / Play style, not ability' if result.get('assignment') else 'Play style, not ability. A fun EA match-stat comparison.'
    draw.text((50,575),footer,font=font(21),fill=(180,192,211))
    output=io.BytesIO()
    image.save(output,format='PNG')
    return output.getvalue()
