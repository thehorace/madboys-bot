"""Additional editorial style templates. Targets are EA heuristics, not player ratings.

These span support roles and quieter statistical spells, not a 'bad footballer'
ranking. Current form is assessed separately using the user's own match data.
"""
PROFILES = {
 'FWD': [
  ('Timo Werner','The quiet finishing spell','An attacking spell with limited end product.',{'goals':.12,'creation':.2,'circulation':.28,'precision':.4},'https://www.chelseafc.com/en/news/article/tuchel--nobody-is-blaming-the-strikers'),
  ('Darwin Núñez','The uneven-output forward','Modest end product with a direct passing pattern.',{'goals':.35,'creation':.15,'circulation':.3,'precision':.5},'https://www.premierleague.com/en/news/4230533'),
  ('Roberto Firmino','The supporting nine','Passing and supplying teammates ahead of goal volume.',{'goals':.3,'creation':.65,'circulation':.8,'precision':.8},'https://www.liverpoolfc.com/news/first-team/298155-roberto-firmino-mohamed-salah-dirk-kuyt'),
  ('Olivier Giroud','The linking centre-forward','Moderate goal output with a supporting passing role.',{'goals':.45,'creation':.4,'circulation':.4,'precision':.65},'https://www.fff.fr/equipe-nationale/joueur/8589-giroud-olivier/fiche.html'),
  ('Jack Grealish','The possession-led forward','More passing involvement than direct goal output.',{'goals':.15,'creation':.4,'circulation':.85,'precision':.9},'https://www.mancity.com/news/mens/wolves-0-3-city-stats-of-the-match-63799093'),
  ('Bukayo Saka','The supplying forward','A balanced mix of goals, assists and passing.',{'goals':.55,'creation':.7,'circulation':.7,'precision':.8},'https://www.englandfootball.com/england/mens-senior-team/squad/bukayo-saka')],
 'MID': [
  ('Jordan Henderson','The supporting midfielder','A steady passing and tackling pattern with modest end product.',{'goals':.05,'creation':.15,'circulation':.65,'precision':.7,'defence':.5},'https://www.englandfootball.com/england/mens-senior-team/squad/Jordan-Henderson'),
  ('James Milner','The utility midfielder','Supporting contributions spread across the midfield stats.',{'goals':.1,'creation':.25,'circulation':.5,'precision':.65,'defence':.6},'https://www.liverpoolfc.com/news/first-team/325353-james-milner-liverpool-trent-alexander-arnold'),
  ('Fred','The recycling midfielder','A lower-output midfield pattern with passing and tackling involvement.',{'goals':.05,'creation':.1,'circulation':.32,'precision':.4,'defence':.3},'https://www.manutd.com/en/news/fred-transfer-complete-as-man-united-issues-press-release-on-his-signing-from-shakhtar-donetsk?t=y'),
  ('Conor Gallagher','The midfield contributor','A mix of tackling and supporting attacking output.',{'goals':.3,'creation':.35,'circulation':.5,'precision':.65,'defence':.8},'https://www.chelseafc.com/en/news/article/in-focus-the-evolution-of-conor-gallagher?lid=k7cjys6ynbf5'),
  ('Luka Modrić','The passing orchestrator','Passing-led midfield play with some creativity.',{'goals':.2,'creation':.5,'circulation':.85,'precision':.85,'defence':.45},'https://www.realmadrid.com/en-US/the-club/history/football-legends/luka-modric'),
  ('Martin Ødegaard','The passing creator','Assist production and high passing involvement.',{'goals':.3,'creation':.85,'circulation':.9,'precision':.85,'defence':.35},'https://www.arsenal.com/men/players/martin-odegaard')],
 'DEF': [
  ('Harry Maguire','The supporting centre-back','A lower-volume defensive and passing pattern.',{'goals':.1,'creation':.05,'circulation':.3,'precision':.45,'defence':.3},'https://www.manutd.com/en/teams/mens-team/harry-maguire'),
  ('John Stones','The build-up defender','High passing involvement from a defensive role.',{'goals':.1,'creation':.25,'circulation':1,'precision':.95,'defence':.45},'https://www.mancity.com/news/mens/john-stones-leaving-manchester-city-63912971'),
  ('Andy Robertson','The supplying full-back','Defensive work paired with attacking supply.',{'goals':.1,'creation':.65,'circulation':.65,'precision':.75,'defence':.65},'https://www.liverpoolfc.com/news/compilation-watch-every-andy-robertson-goal-and-assist-liverpool?amp=1'),
  ('Kyle Walker','The defence-first full-back','Defensive contributions with supporting passing.',{'goals':.05,'creation':.15,'circulation':.6,'precision':.75,'defence':.8},'https://www.englandfootball.com/england/mens-senior-team/squad/Kyle-Walker')],
 'GK': [
  ('David de Gea','The save-focused keeper','More save involvement than passing involvement.',{'saving':.65,'circulation':.15,'precision':.45},'https://www.premierleague.com/en/news/529119/mourinho-de-gea-is-the-best-in-the-world'),
  ('Jordan Pickford','The supporting goalkeeper','A lower-volume blend of saves and distribution.',{'saving':.35,'circulation':.35,'precision':.55},'https://www.englandfootball.com/england/mens-senior-team/squad/Jordan-Pickford')]
}

# Synthetic EA-stat patterns: these describe the comparison, not measured
# real-player averages. Keep each pattern distinct so every name is reachable.
BARCA_HISTORY = 'https://www.fcbarcelona.com/en/club/history/legendary-players'
MORE_PROFILES = {
 'FWD': [
  ('Wayne Rooney','The involved striker','Goals and assists alongside frequent passing.',{'goals':.6,'creation':.6,'circulation':.8,'precision':.7},'https://www.dcunited.com/players/wayne-rooney/'),
  ('Neymar','The creative attacker','Assist-led attacking involvement with goals and passing.',{'goals':.55,'creation':.95,'circulation':.85,'precision':.8},BARCA_HISTORY),
  ('Luis Suárez','The productive number nine','High scoring output paired with attacking supply.',{'goals':.95,'creation':.75,'circulation':.45,'precision':.65},BARCA_HISTORY),
  ('Ronaldinho','The attacking playmaker','A forward role centred on creating and circulating the ball.',{'goals':.35,'creation':1,'circulation':1,'precision':.85},BARCA_HISTORY),
  ('Marcus Rashford','The direct goal threat','Goal contribution with relatively little passing involvement.',{'goals':.6,'creation':.2,'circulation':.25,'precision':.5},'https://www.englandfootball.com/england/mens-senior-team/squad/marcus-rashford'),
  ('Ollie Watkins','The balanced number nine','Moderate scoring and supply from a forward role.',{'goals':.5,'creation':.5,'circulation':.35,'precision':.6},'https://www.englandfootball.com/england/mens-senior-team/squad/Ollie-Watkins'),
  ("Samuel Eto’o",'The goal-first number nine','High goal output with limited passing volume.',{'goals':.85,'creation':.1,'circulation':.15,'precision':.75},BARCA_HISTORY),
  ('David Villa','The combining finisher','Scoring with accurate supporting passing.',{'goals':.7,'creation':.3,'circulation':.55,'precision':.9},BARCA_HISTORY)],
 'MID': [
  ('Xavi','The circulation specialist','Very high passing involvement with supporting creativity.',{'goals':.05,'creation':.65,'circulation':1,'precision':1,'defence':.2},BARCA_HISTORY),
  ('Andrés Iniesta','The creative connector','Passing and assists ahead of scoring or tackling volume.',{'goals':.15,'creation':.75,'circulation':.8,'precision':.95,'defence':.2},BARCA_HISTORY),
  ('Sergio Busquets','The possession anchor','Accurate midfield circulation with little direct end product.',{'goals':0,'creation':.1,'circulation':1,'precision':1,'defence':.5},BARCA_HISTORY),
  ('Paul Scholes','The passing goal threat','Scoring combined with a high volume of accurate passing.',{'goals':.5,'creation':.35,'circulation':.9,'precision':.9,'defence':.25},'https://www.premierleague.com/en/events/hall-of-fame/inductees/paul-scholes'),
  ('David Beckham','The supply specialist','Assist-focused output and passing from midfield.',{'goals':.2,'creation':1,'circulation':.75,'precision':.75,'defence':.15},'https://www.realmadrid.com/en-US/the-club/history/football-legends/david-robert-joseph-beckham'),
  ('Steven Gerrard','The complete contributor','Goals, assists and tackles alongside midfield passing.',{'goals':.6,'creation':.7,'circulation':.7,'precision':.65,'defence':.75},'https://www.liverpoolfc.com/info/steven-gerrard'),
  ('Casemiro','The defensive anchor','Tackling dominates a lower-volume passing pattern.',{'goals':.15,'creation':.1,'circulation':.4,'precision':.65,'defence':.9},'https://www.realmadrid.com/en-US/the-club/history/football-legends/carlos-henrique-casimiro'),
  ('Scott McTominay','The scoring contributor','Goals and tackles with less emphasis on passing or assists.',{'goals':.6,'creation':.15,'circulation':.35,'precision':.6,'defence':.6},'https://www.scottishfa.co.uk/en/teams/mens-team/scott-mctominay')],
 'DEF': [
  ('Sergio Ramos','The goal-contributing defender','Defensive work combined with an unusual goal contribution.',{'goals':.5,'creation':.1,'circulation':.65,'precision':.8,'defence':.8},'https://www.realmadrid.com/en-US/the-club/history/football-legends/sergio-ramos-garcia'),
  ('Marcelo','The creative full-back','Attacking supply and passing ahead of tackling volume.',{'goals':.25,'creation':.75,'circulation':.9,'precision':.85,'defence':.3},'https://www.fifa.com/en/tournaments/mens/club-world-cup/usa-2025/articles/marcelo-retires-real-madrid-fluminense'),
  ('Gerard Piqué','The circulating centre-back','Accurate build-up passing with supporting defensive actions.',{'goals':.15,'creation':.05,'circulation':1,'precision':.95,'defence':.35},BARCA_HISTORY),
  ('Nemanja Vidić','The tackle-led centre-back','Heavy defensive involvement with modest passing volume.',{'goals':.1,'creation':0,'circulation':.25,'precision':.55,'defence':1},'https://www.thefa.com/bootroom/resources/interviews/nemanja-vidic/i-was-a-fighter-from-an-early-age'),
  ('Carles Puyol','The defensive contributor','Tackles lead the output with limited attacking supply.',{'goals':0,'creation':.05,'circulation':.4,'precision':.85,'defence':.85},BARCA_HISTORY),
  ('Kieran Trippier','The supporting supplier','Assists alongside steady passing and defensive contributions.',{'goals':.05,'creation':.5,'circulation':.55,'precision':.65,'defence':.5},'https://www.englandfootball.com/england/mens-senior-team/squad/kieran-trippier')],
 'GK': [
  ('Gianluigi Buffon','The traditional goalkeeper','Strong save involvement with limited distribution volume.',{'saving':.85,'circulation':.1,'precision':.7},'https://www.juventus.com/en/news/articles/on-this-day-gigi-buffon-s-first-day-as-a-bianconero?appview=true'),
  ('Petr Čech','The measured shot-stopper','Moderate save volume and a restrained passing role.',{'saving':.55,'circulation':.2,'precision':.8},'https://www.chelseafc.com/en/news/article/stories-behind-the-saatchi-shirts-part-two')]
}
for _role, _profiles in MORE_PROFILES.items():
    PROFILES[_role].extend(_profiles)
