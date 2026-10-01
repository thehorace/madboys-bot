# MADBOYS FC Pro Clubs Bot

Discord bot for the MADBOYS FC EA FC 27 Pro Clubs squad: automatic match
results, stats, lineups, rotation tracking and session sign-ups.

## What it does

**Automatic match tracking (always on).** While the bot is running it checks
EA for new league and playoff results: **every minute while you're playing**
(2+ of the squad in voice, a session on, or a game just finished), every 3 min
otherwise. Opening **Last game** also triggers an instant check, so a result someone
looks up is posted straight away. Each new match is:

- saved permanently (so `/form`, `/h2h`, `/recap` work beyond EA's short history)
- posted to the matchday channel with a full ratings table
- used to log each linked player's role for `/rotation`
- posted with a **result card image** (score, scorers, colour-coded ratings)
- followed by a **squad MOTM vote**: everyone picks their man of the match from
  a dropdown, results are revealed when it closes (10 min), and `/motm table`
  tracks the season's awards
- checked for career milestones ("🎉 @Fauzan just hit 100 career goals!")

A weekly recap posts on Sunday evening. Set the channel once with
`/matchday start`; it survives restarts and redeploys.

EA only publishes *finished* matches and has no "match ended" notification,
so results appear about 1–3 minutes after full time. There is no live score feed.
Optional: set `ENABLE_PRESENCE=1` (after turning on **Presence Intent** in the
Discord Developer Portal) so "Playing EA SPORTS FC" statuses also count.

## Exact positions, builds and rotation

EA's match data only says goalkeeper / defender / midfielder / forward, so the
bot works out exact spots (LB vs CB vs RB...) itself:

1. **Players tap 🛠️ My builds** on the panel once and tick the positions they
   have builds for.
2. **A manager taps 🧑‍💼 Manager** on the panel: pick a formation, ✨ Auto-suggest
   (uses everyone who's actually around — ✅ signed up, 🎧 in voice, 🎮 played in
   the last 2 hours — plus everyone's builds, and spreads positions around based
   on rotation history), swap anyone with two taps, then 📢 Post lineup. That
   lineup is what tells the bot who's LB and who's CB.
   **Late arrivals are automatic:** a squad member who joins voice around session
   time is marked ✅ on the sign-up (shown with 🎧), even if they'd said ❌.
3. **After each game** the bot logs each player's exact spot from the lineup
   when EA's role agrees. Without a lineup, it assumes you stayed in the spot
   you played the previous game this session. Anyone it can't be sure about gets
   a one-tap "📍 Where did you play?" message. `/position` fixes your last game any time.
4. **Rotation notes** go to a private managers' channel (set it with
   "📝 Send rotation notes here" in the Manager menu), e.g. *"Ali — LB 3 games
   in a row — has builds for CB, CM → try CB next"*. Players aren't nagged.

## Buttons instead of commands

Not everyone likes typing slash commands, so there's a menu too:

- **`/stats`** opens a private menu (only you see it) with buttons and dropdowns
  for everything: last game, club stats, form, recap, any player, leaderboards,
  compare, head-to-head. First-timers link their EA name by picking it from a
  dropdown. **📢 Share** posts whatever you're looking at to the channel.
- **`/panel`** (managers) posts a pinned message with big buttons. Anyone can
  click it anytime, even after restarts, and never has to type anything.
  **`/panel sticky:True`** keeps it at the bottom of a busy channel instead: once
  the chat moves on a few messages, the bot re-posts it and removes the old one.

## Commands

| | |
|---|---|
| **Stats** | `/lastgame` `/clubstats` `/me` `/playerstats` `/leaderboard` `/compare` `/form` `/h2h` `/recap` |
| **Setup** | `/link me` (connect your EA name — do this first) `/builds` (positions you have builds for) `/position` (fix your last game's spot) |
| **Sessions** | `/session create` (who's on tonight? ✅/🤔/❌ buttons + 30-min reminder) `/session list` `/session cancel` |
| **Lineups** | `/lineup builder` (button builder) `/lineup suggest` `/lineup assign` `/lineup clear` `/lineup post` `/formation set` `/formation show` |
| **Rotation** | `/rotation check` `/rotation history` `/rotation stats` |
| **Tracker** | `/matchday start` `/matchday stop` `/matchday status` `/matchday check` |
| **MOTM** | `/motm table` `/motm close` |
| **Menus** | `/stats` (button menu) `/panel` (pinned button panel) |
| **Misc** | `/help` `/status` `/build` `/ping` `/debug` (managers: raw EA JSON) |

`/lineup suggest` uses only the players who are around (✅ signed up, 🎧 in
voice, 🎮 played in the last 2 hours), only puts people where they have builds, and spreads exact
positions around using rotation history. It finds the best overall assignment
instead of filling slots first-come-first-served.

"Manager" commands need Manage Channels, or a role named Manager, Admin or Coach.

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # then fill in your real values
python bot.py
```

All settings are documented in `.env.example` and `config.py`.

## Deployment (Railway)

Push to `main` to deploy. Set the same keys as `.env.example` in Railway's
variables. Two important ones:

- **`DB_PATH` must point into a Railway Volume** (e.g. mount a volume at
  `/data` and set `DB_PATH=/data/madboys.db`). Without a volume the database
  (links, match history, rotation, sessions) is wiped on every deploy.
- **`GUILD_ID`** makes slash commands update instantly and tells the tracker
  which server to post in.

## Files

```
bot.py            startup, cog loading, command sync, global error handler
config.py         all settings (env vars)
db.py             SQLite connection, schema, migrations
ea_client.py      EA relay client: shared cache, stale-data fallback, health
match_data.py     parsing / storing matches, match embeds, history queries
pitch.py          lineup image rendering
match_card.py     result card image
fonts.py          bundled fonts (assets/fonts) so images look the same on Railway
cogs/matchday.py  always-on tracker, milestones, weekly recap, /recap
cogs/stats.py     stats commands
cogs/lineup.py    formations, prefs, lineup suggest (Hungarian assignment)
cogs/rotation.py  rotation history + checks
positions.py      exact-position logic (posted lineups, carry-over, rotation notes)
cogs/positions.py "where did you play?" picker, /position, manager rotation notes
cogs/sessions.py  session sign-ups + reminders
cogs/link.py      Discord <-> EA name links
cogs/motm.py      squad MOTM vote + awards table
cogs/hub.py       /stats button menu + /panel pinned panel
cogs/misc.py      /help /build /ping
```
