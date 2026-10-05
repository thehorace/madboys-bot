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
   have builds for. **Managers can do it for them:** 🧑‍💼 Manager → 🛠️ Set
   players' builds shows the whole squad's builds, and lets them pick any player
   and tick their positions (or `/builds player:@someone`).
2. **A manager taps 🧑‍💼 Manager** on the panel: pick a formation, ✨ Auto-suggest
   (uses everyone who's actually around — ✅ signed up, 🎧 in voice, 🎮 played in
   the last 2 hours — plus everyone's builds and the rotation policy below),
   swap anyone with two taps, then 📢 Post lineup. That
   lineup is what tells the bot who's LB and who's CB.
   **Late arrivals are automatic:** a squad member who joins voice around session
   time is marked ✅ on the sign-up (shown with 🎧), even if they'd said ❌.
3. **After each game** the bot logs each player's exact spot from the lineup
   when EA's role agrees. Without a lineup, it assumes you stayed in the spot
   you played the previous game this session. Anyone it can't be sure about gets
   a one-tap "📍 Where did you play?" message. `/position` fixes your last game any time.
4. **Rotation policy (set by the manager):** the pitch is split into 3 areas —
   **Defence, Midfield, Front 3**. ST → RW is still the front 3, so it's not a
   rotation; front 3 → mids is. Someone is "due a rotation" after **3 games in a
   row in the same area** (keepers excluded). Auto-suggest keeps everyone else
   settled where they played last — no moving for the sake of it.
5. **Rotation notes** are private: managers tap "📩 DM me rotation notes" in the
   🧑‍💼 Manager menu and get a DM like *"Ali — Defence 3 games in a row (CB, LB, LB)
   — has builds for CM → could try Midfield next"*, once per streak. They can also
   be posted in a managers-only channel. Players aren't nagged.

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
voice, 🎮 played in the last 2 hours), only puts people where they have builds,
keeps players settled, and only moves someone to another area once they've
done 3 games in a row in one area. It finds the best overall assignment
instead of filling slots first-come-first-served.

"Manager" commands need Manage Channels, or a role named Manager, Admin or Coach.

## Daily session sign-ups

Every day at **11am**, the bot posts a sign-up in **#general** for **6:30pm**
that evening, using `BOT_TZ` (default Singapore time). It uses the usual
In / Maybe / Out buttons and the 30-minute reminder. Nobody is signed up
automatically when the post is created.

Both daily and manual sign-ups ping `@everyone` on their first post (the bot
needs Mention Everyone permission in that channel). Until kick-off, the sign-up
moves to the bottom after every 10 new messages from other users or bots,
keeping all RSVPs and removing the old post. These moves do not ping everyone
again. A **5-minute minimum gap** prevents frequent reposts in a busy chat.
Cancellation and kick-off stop sticky moves; both the count and cooldown survive restarts.

Only private allowlisted users (`USAGE_VIEWERS`, default `fauz`) can use
**`/session settings`** to view or change the daily posting
time, kick-off time, channel, weekdays, enabled state, sticky cooldown and waitlist.
For example: `/session settings post_time:11am kickoff_time:6:30pm days:all`.
Times use `BOT_TZ`, and posting must be earlier than kick-off that same day.
Changes apply to future daily posts; already-posted sessions keep their time.
**`/session skip`** skips today's automatic session and cancels it if already posted.
It uses that same private allowlist; manager/admin roles and server ownership
do not grant access to these two commands. Ordinary session cancellation and
lineup management still use the existing manager permissions.

Session posts prominently show **Need N more**, **Full XI**, or **Session started**.
The waitlist is enabled by default: the first 11 In sign-ups take the places;
extra players queue in order and move into the XI when someone drops out.
Clicking In again does not move a player to the back. Sign-up buttons close at
kick-off (late voice arrivals are still detected for lineup suggestions).

If the bot restarts after 11am, it catches up before kick-off. Existing sessions
at that start time, including cancelled ones, prevent another post. Set
`SESSION_CHANNEL_ID` if the channel has a different name or there is more than
one #general, and `GUILD_ID` if the bot is in multiple servers. Set
`DAILY_SESSIONS=0` to set the initial default to disabled (saved Discord settings
take precedence). Keep `DB_PATH` on a persistent volume
so the bot remembers posts across deploys.

## Bot usage stats (private)

**`/admin`** opens an ephemeral admin panel with buttons for Usage, Settings,
Bot status, Backup and Session history. Session controls include schedule
editing, a channel picker, daily-post/waitlist toggles and Skip today. Every
button and schedule submission rechecks the panel owner and private allowlist;
manager roles and server ownership do not bypass this. Panels expire after 14
minutes; reopen with `/admin`. Admin panel activity is excluded from usage logs.

The bot logs every command, button tap and dropdown pick (who, what they picked,
when) — never normal chat messages. **`/usage`** opens a private report: overview,
who uses the bot most, which features get used, what people look up (players,
leaderboards, opponents), a per-person breakdown, and a CSV download. Only the
people listed in `USAGE_VIEWERS` (Discord usernames or IDs, comma-separated) can
open it; if that's not set, only `fauz` can. Logs older than
`USAGE_KEEP_DAYS` (180) are deleted automatically.

The report separates actual stats lookups from menu navigation and other actions,
shows people returning on multiple days, and includes a **Trends** tab comparing
the last seven days with the preceding seven. Successful-use totals exclude
failed and unfinished attempts. CSV includes category and outcome, including
historical entries whose outcome is unknown. Those older entries are preserved
but do not count as confirmed successes. Normal chat content is never stored.

## Official FC 27 patch-note posts

The bot checks [EA's official FC 27 news page](https://www.ea.com/games/ea-sports-fc/fc-27/news)
hourly for title updates, patch notes and developer/gameplay launch updates,
plus all Pro Clubs and The Grounds articles identified by their title, summary,
URL slug or EA tags. Grounds/Clubs articles must concern new features, developer
updates, improvements or fixes. Beginner guides and general events are excluded.
Featured EA articles are included too. Purchase/edition promotions, launch-reward
ads and unrelated FUT/Career developer news are excluded. Excerpts put Clubs,
Grounds and live-issue changes first, with decorative images and table-of-contents
links removed so those cannot crowd out the details.
New articles post once in **#general**, with an excerpt of EA's change details,
publication time and a link to the full official notes. No mentions are sent.
The first check quietly records a baseline and sends nothing. Existing installs
also migrate quietly. Only articles published after that baseline and within
the last 24 hours are announced, so new filters, featured articles, restarts or
long downtime cannot replay the archive. Private Preview latest can still show
the latest existing article on demand. Posts use short excerpts (about 1,500
characters maximum) and a link rather than dumping a long article.
Failed fetches or sends are retried without marking the
unsent update as posted. The seen list survives restarts with the database.

Private **`/admin` → Settings → EA News** controls offer enable/disable, a channel picker,
Check EA now and Preview latest. The equivalent `/patchnotes settings`,
`/patchnotes check` and `/patchnotes latest` commands use the same private allowlist.
`PATCHNOTES_CHANNEL_ID` sets the initial channel default. If EA blocks the bot or
changes its page format, the bot reports the problem privately and avoids
posting empty notes. Edits to an already-seen article do not create another post.

## Bot status, settings and session history

Open **`/admin` → Bot status** for the match tracker's current state, last
successful EA check (saved across restarts), next check, latest game, session
tasks and next daily post, EA news check, backups, summary settings and recorded
failures. A service awaiting its first check is shown as unchecked. Refresh
updates the panel. `/maintenance status` opens the same report; `/matchday status`
is now restricted to the private `USAGE_VIEWERS` allowlist too.

**`/admin` → Settings** collects Sessions, Match tracker, Session summaries and
EA News in one place. The existing allowlist and panel-owner checks apply to
each change. Schedule, timezone, sticky cooldown, waitlist, channels, posting
toggles and summary finish gap are saved in SQLite.

After a planned session has no recorded game for **two hours** (default), the
next successful EA poll saves its history and posts one club recap in the
#general (or the configured recap channel), with no pings. The club recap includes
the club record, scores and squad performance. A personal summary is also sent by
DM to each linked player recorded by EA as actually playing, regardless of RSVP.
**Settings → Session summaries** lets you choose a one-hour or two-hour gap, a
recap channel, and independently enable/disable club posts and personal DMs
while still saving history. A new game resets the timer; failed EA checks do
not finish sessions. Games are grouped from the scheduled start up to the first
inactivity gap or next session. The first game must occur within the configured
gap after kick-off; later play after a full inactivity gap needs a new session.
Stats cover games actually stored by the tracker, and can be incomplete if EA
did not provide a match while the bot was offline.

The recap's **My session summary** button opens only the clicking player's
games, goals, assists, average rating, MOTMs, tackles, saves and passing accuracy
privately. It requires an EA link at completion. **My session** on the main panel
and `/mysession` show the latest finished club session. Recap buttons survive
restarts. Initial historical records are saved without public catch-up posts;
failed sends retry for up to 24 hours after the last game.
DM delivery is saved per session and recipient so successful DMs are not resent
after restarts or another recipient's failure. Temporary failures retry for up
to 24 hours; blocked DMs or unavailable members are skipped, and those players
can still open their summaries from the panel. Existing archived sessions are
not sent retroactively when this feature is deployed or DMs are enabled.

**Session history** on the main panel or `/sessionhistory` browses saved sessions,
results, cancellations and sessions without games, with a session picker and
older/newer pages. Each player can open their own past summary. **`/admin` →
Session history** additionally shows actual EA players and frozen RSVP lists.
Sign-ups are explicitly separate from actual attendance. History and player
stats survive restarts and remain tied to the EA links at session completion.

## Guided player setup

**`/setup`**, or the panel's **Setup** button, opens a private three-step flow:
pick your EA player (or enter the name), choose your build positions, then read
a short guide to Stats, Me, session RSVPs and Share. The flow resumes from saved
progress. Opening `/stats` before linking and choosing positions opens this flow.

## Backups and private alerts

The bot makes a checked online SQLite backup daily after **3am in `BOT_TZ`**,
catching up on startup, and retains seven dated snapshots by default. Configure
`BACKUP_DIR` and `BACKUP_KEEP_DAYS` if needed; the default folder is `backups`
beside `DB_PATH`. SQLite's backup API includes committed WAL changes.

Only usage viewers can open **`/maintenance status`** or **`/maintenance backup`**.
The latter creates and downloads a checked copy (large files stay on the volume
if they exceed Discord's upload limit). Failure and recovery alerts for session
posting, reminders, tracking and backups are sent by DM to usage viewers, or to
`ALERT_USER_ID` when configured. The bot sends one alert per failure episode and
stays quiet while a notified failure is unchanged. DMs must be enabled.

Keep the database and backups on a persistent Railway Volume. Copies on that
same volume protect against bad data changes, not loss of the whole volume;
download a copy periodically for an independent backup. To restore, stop the
bot, preserve the current database, replace it with the downloaded copy, remove
stale `-wal`/`-shm` files while stopped, then restart. There is no automatic restore.

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
