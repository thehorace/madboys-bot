"""
Automatic match tracker — always on, no /matchday start needed after restarts.

What happens in the background, forever, while the bot is running:
  1. Every couple of minutes it asks EA for the club's latest league + playoff
     matches (EA only exposes *finished* matches, so a result appears a few
     minutes after the final whistle — there's no live score feed).
  2. Any match it hasn't seen before is saved to the database (permanent
     history for /form, /h2h, recaps...).
  3. Each linked player's role that game is logged for /rotation.
  4. The result is posted to the matchday channel (if one is set).
  5. Career totals are compared to last time to announce milestones
     (e.g. "just hit 100 goals").
  6. Once a week it posts a recap.

Polling speed adapts: every POLL_ACTIVE_MINUTES (default 1) while you're
playing and every POLL_IDLE_MINUTES (default 5) otherwise, so EA/the home relay
isn't hammered at 4am. "Playing" means any of: 2+ squad members in a voice
channel, someone's Discord status showing EA FC (if ENABLE_PRESENCE=1), a match
finished in the last 90 min, or a /session is on. The switch to fast checks
happens within ~15 seconds of people joining voice.

The first time it runs against an empty database it saves what EA currently
has *without* posting it, so the channel doesn't get spammed with old games.

Commands:
  /matchday start [channel]  - Manager: post results in this (or the given) channel. Saved permanently.
  /matchday stop             - Manager: stop posting (matches are still tracked silently)
  /matchday status           - What the tracker is doing
  /matchday check            - Check for new matches right now
  /recap [days]              - Summary of the last N days (default 7)
"""

from clubs import club_id, club_name, club_scope, club_scoped, monitored_clubs
import asyncio
import json
import delivery
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import match_data as md
from announce import announce_channel_id
from cogs.operations import report_health
from cogs.scout import scout_view
from cogs.link import find_discord_id_by_ea_name, get_all_links
from cogs.rotation import is_match_processed, log_positions, mark_match_processed
from config import (ACTIVE_WINDOW_MINUTES, BOT_TZ, CLUB_COLOUR, CLUB_ID, GUILD_ID,
                    MATCHDAY_CHANNEL_ID, POLL_ACTIVE_MINUTES, POLL_IDLE_MINUTES, RECAP_HOUR, RECAP_WEEKDAY,
                    VOICE_ACTIVE_PLAYERS)
from db import connect, get_setting, set_setting
from utils import clip, is_manager, to_int, survive
from interaction_tracking import failed

log = logging.getLogger("madboys-bot.matchday")

# EA's per-player "pos" bucket -> rotation role
POSITION_MAP = {"goalkeeper": "GK", "defender": "DEF", "midfielder": "MID", "forward": "FWD"}

MAX_POSTS_PER_POLL = 3
# A club whose checks have failed this long stops holding up session summaries for the
# other clubs (its alert stays up until it recovers).
STALE_CLUB_SECONDS = 2 * 3600
_FETCH_FAILED = object()

# Discord "Playing ..." names that mean someone's on the game (only used if ENABLE_PRESENCE=1)
GAME_NAME = re.compile(r"EA SPORTS FC|\bFC ?2[5-9]\b|\bFIFA\b", re.IGNORECASE)  # if the bot was offline and finds 8 new games, don't flood the channel

MILESTONES = {
    "goals":         ("career goals", [10, 25, 50, 75, 100, 150, 200, 250, 300, 400, 500, 750, 1000]),
    "assists":       ("career assists", [10, 25, 50, 75, 100, 150, 200, 250, 300, 400, 500, 750, 1000]),
    "gamesPlayed":   ("career games", [50, 100, 150, 200, 250, 300, 400, 500, 750, 1000, 1500, 2000]),
    "manOfTheMatch": ("career MOTMs", [10, 25, 50, 75, 100, 150, 200, 300, 500]),
}

K_CHANNEL = "matchday_channel"
K_ENABLED = "matchday_enabled"
K_LAST_RECAP = "last_recap_week"


def extract_role(pos: Optional[str]) -> Optional[str]:
    return POSITION_MAP.get((pos or "").strip().lower())


def crossed(old: int, new: int, thresholds: list[int]) -> Optional[int]:
    """Highest threshold t with old < t <= new, else None."""
    hit = [t for t in thresholds if old < t <= new]
    return max(hit) if hit else None


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(BOT_TZ)
    except Exception:
        return ZoneInfo("UTC")


def build_recap_embed(days: int = 7, end: Optional[datetime] = None, *, combined=False) -> Optional[discord.Embed]:
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
    with connect() as conn:
        clubs = [dict(c) for c in conn.execute('SELECT club_id,name FROM monitored_clubs ORDER BY club_id=? DESC,club_id', (CLUB_ID,))] if combined else [{'club_id':club_id(),'name':club_name()}]
        if not clubs:
            return None
        ids = [c['club_id'] for c in clubs]
        marks = ','.join('?' for _ in ids)
        args = (*ids, start_ts, end_ts)
        rows = [dict(r) for r in conn.execute(f'SELECT * FROM matches WHERE club_id IN ({marks}) AND ts>=? AND ts<? ORDER BY ts,club_id,match_id', args)]
        if not rows:
            return None
        totals = [dict(r) for r in conn.execute(f"""
            WITH appearances AS (
                SELECT p.*, ROW_NUMBER() OVER (
                    PARTITION BY p.club_id,p.match_id,LOWER(p.name) ORDER BY p.persona_id
                ) AS persona_row
                FROM matches m JOIN match_players p ON p.club_id=m.club_id AND p.match_id=m.match_id
                WHERE m.club_id IN ({marks}) AND m.ts>=? AND m.ts<?
            )
            SELECT MIN(name) AS name,COUNT(*) AS games,SUM(goals) AS goals,SUM(assists) AS assists,
                   AVG(rating) AS rating,COUNT(rating) AS rated_games,SUM(motm) AS motm
            FROM appearances WHERE persona_row=1 GROUP BY LOWER(name) ORDER BY LOWER(name)
        """, args)]
    label = ' & '.join(c['name'] for c in clubs)
    kind = 'Weekly' if days == 7 else f'{days}-day'
    w,d,l = (sum(r['result']==code for r in rows) for code in ('W','D','L'))
    record = ' · '.join(f"{n} {single if n==1 else plural}" for n,single,plural in ((w,'win','wins'),(d,'draw','draws'),(l,'loss','losses')))
    count_label = 'match' if len(rows)==1 else 'matches'
    embed = discord.Embed(title=f'🗓️ {kind} recap — {label}'[:256],
        description=f'<t:{start_ts}:f> → <t:{end_ts}:f>\n\n'
                    f'**{len(rows)} {count_label} · {record}**\n'
                    f"⚽ {sum(r['our_goals'] for r in rows)} scored · {sum(r['opp_goals'] for r in rows)} conceded",
        colour=CLUB_COLOUR)
    for club in clubs:
        games = [r for r in rows if r['club_id']==club['club_id']]
        if not games:
            continue
        cw,cd,cl = (sum(r['result']==code for r in games) for code in ('W','D','L'))
        form = md.form_string(list(reversed(games[-15:])))
        embed.add_field(name=club['name'],value=f'**{len(games)} matches** · W{cw} D{cd} L{cl}\n'
            f"{sum(r['our_goals'] for r in games)} scored · {sum(r['opp_goals'] for r in games)} conceded\n"
            f"Last {min(15,len(games))} {'result' if len(games)==1 else 'results'} (oldest → newest): {form}",inline=False)
    def top(key, fmt, min_games=1):
        pool = [t for t in totals if (t['rated_games'] if key=='rating' else t['games'])>=min_games and t[key] is not None and t[key]!=0]
        if not pool:
            return '—'
        best = max(pool,key=lambda t:t[key])
        return f"**{discord.utils.escape_markdown(best['name'])}** — {fmt(best)}"
    embed.add_field(name='⚽ Top scorer',value=top('goals',lambda t:f"{t['goals']} goals"))
    embed.add_field(name='🅰️ Most assists',value=top('assists',lambda t:f"{t['assists']} assists"))
    embed.add_field(name='⭐ Most EA MOTMs',value=top('motm',lambda t:str(t['motm'])))
    minimum = max(2,len(rows)//3)
    embed.add_field(name='📈 Best average rating',value=top('rating',lambda t:f"{t['rating']:.2f} over {t['rated_games']} rated games",minimum))
    wins = [r for r in rows if r['result']=='W']
    if wins:
        big = max(wins,key=lambda r:(r['our_goals']-r['opp_goals'],r['our_goals']))
        name = next(c['name'] for c in clubs if c['club_id']==big['club_id'])
        embed.add_field(name='💥 Biggest win',value=f"{name} · {big['our_goals']}–{big['opp_goals']} vs {discord.utils.escape_markdown(big['opp_name'] or 'Opponent')}")
    embed.set_footer(text=f'Saved EA matches only • Rating needs {minimum}+ rated games • Weekly totals are separate from session recaps')
    return embed


class MatchdayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ea = bot.ea
        self.last_poll_at: Optional[float] = None
        self.last_poll_ok: Optional[bool] = None
        self.last_poll_partial = False
        self.last_success_at: Optional[float] = None
        self.last_new_at: Optional[float] = None
        self._polling = False
        self._linked_in: dict[str, list[str]] = {}   # match_id -> linked players' discord ids
        self._unlinked: dict[str, list[str]] = {}    # match_id -> EA names of players nobody has linked
        self._migrated = False
        from cogs.result_details import ResultDetails
        bot.add_dynamic_items(ResultDetails)
        self.ticker.start()
        self.recap_loop.start()

    def cog_unload(self):
        from cogs.result_details import ResultDetails
        self.bot.remove_dynamic_items(ResultDetails)
        self.ticker.cancel()
        self.recap_loop.cancel()

    # ------------------------------------------------------------------ #
    #  guild / channel resolution
    # ------------------------------------------------------------------ #
    def home_guild(self) -> Optional[discord.Guild]:
        if GUILD_ID:
            return self.bot.get_guild(int(GUILD_ID))
        if len(self.bot.guilds) == 1:
            return self.bot.guilds[0]
        return None

    def _migrate_old_poll_channel(self, guild_id: str):
        """Carry over the channel from the old /matchday start table, once."""
        if self._migrated:
            return
        self._migrated = True
        if get_setting(guild_id, K_CHANNEL):
            return
        with connect() as conn:
            row = conn.execute("SELECT channel_id FROM matchday_poll WHERE guild_id=? AND club=?",
                               (guild_id, club_name())).fetchone()
        if row:
            set_setting(guild_id, K_CHANNEL, row["channel_id"])
            log.info(f"Migrated matchday channel {row['channel_id']} from old matchday_poll table")

    def channel_id_for(self, guild_id: str) -> Optional[int]:
        # The shared announcements channel wins; the old per-feature channel is only a fallback.
        cid = announce_channel_id(guild_id, get_setting(guild_id, K_CHANNEL) or MATCHDAY_CHANNEL_ID)
        return int(cid) if cid else None

    def posting_enabled(self, guild_id: str) -> bool:
        return get_setting(guild_id, K_ENABLED) != "0"

    async def _channel(self, channel_id: int) -> Optional[discord.abc.Messageable]:
        ch = self.bot.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                log.warning(f"Could not fetch matchday channel {channel_id}")
                return None
        return ch

    # ------------------------------------------------------------------ #
    #  adaptive schedule
    # ------------------------------------------------------------------ #
    def active_reason(self) -> Optional[str]:
        """
        Why we think the squad is playing right now (-> check every POLL_ACTIVE_MINUTES),
        or None (-> check every POLL_IDLE_MINUTES). Cheapest signals first.
        """
        now = time.time()
        window = ACTIVE_WINDOW_MINUTES * 60
        guild = self.home_guild()

        # 1. squad members sitting in a voice channel together
        if guild:
            linked = set(get_all_links(str(guild.id)))
            for vc in guild.voice_channels:
                humans = [m for m in vc.members if not m.bot]
                squad = [m for m in humans if str(m.id) in linked] if linked else humans
                if len(squad) >= VOICE_ACTIVE_PLAYERS:
                    return f"{len(squad)} squad members in 🔊 {vc.name}"

        # 2. someone's Discord status says they're playing EA FC (needs ENABLE_PRESENCE=1)
        if guild and self.bot.intents.presences:
            for m in guild.members:
                if not m.bot and any(GAME_NAME.search(getattr(a, "name", "") or "") for a in m.activities):
                    return f"{m.display_name} is playing EA FC"

        # 3. a match finished recently (you're probably mid-session)
        last = max((md.latest_match_ts(c['club_id']) or 0 for c in monitored_clubs()), default=0)
        if last and now - last < window:
            return "played a match recently"

        # 4. a /session is on (15 min before kick-off to ~3h after)
        if not guild:
            return None
        with connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sessions s WHERE guild_id=? AND cancelled=0 AND starts_at BETWEEN ? AND ? "
                "AND NOT EXISTS (SELECT 1 FROM session_history h WHERE h.session_id=s.id) LIMIT 1",
                (str(guild.id), int(now - window - 3 * 3600), int(now + 15 * 60)),
            ).fetchone()
        return "a session is on" if row else None

    def is_active(self) -> bool:
        return self.active_reason() is not None

    def current_interval_minutes(self) -> float:
        return POLL_ACTIVE_MINUTES if self.is_active() else POLL_IDLE_MINUTES

    @property
    def next_poll_at(self) -> float:
        # Worked out fresh every time (rather than fixed when the last check ran), so the moment
        # people hop into voice the bot switches to fast checks instead of finishing a long idle wait.
        return (self.last_poll_at or 0) + self.current_interval_minutes() * 60

    @tasks.loop(seconds=15)
    async def ticker(self):
        if self._polling or time.time() < self.next_poll_at:
            return
        try:
            await self.poll_once()
        except Exception:
            log.exception("Match tracker poll failed")

    @ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    #  the actual work
    # ------------------------------------------------------------------ #
    def poll_soon(self, why: str = ""):
        """Run a check right away in the background (no-op if one is already running)."""
        if self._polling:
            return
        log.info(f"Checking for new matches now ({why})")
        task = asyncio.create_task(self.poll_once())
        task.add_done_callback(
            lambda t: (not t.cancelled() and t.exception()) and log.error(f"poll_soon failed: {t.exception()}"))

    async def poll_once(self) -> int:
        """Fetch, store, log rotation, post, milestones. Returns number of new matches."""
        if getattr(self, '_polling', False):
            return 0
        self._polling = True
        try:
            count = await self._poll_once()
            reports = self.bot.get_cog("SessionReportsCog")
            guild = self.home_guild()
            if self.last_poll_ok:
                self.last_success_at = time.time()
                if guild:
                    set_setting(str(guild.id), "matchday:last_success", str(int(self.last_success_at)))
                if reports and guild:
                    try:
                        await reports.after_poll(guild)
                    except Exception:
                        log.exception("Session summary failed")
                        await report_health(self.bot, str(guild.id), "Session summaries", "Couldn't save or post the session summary. Check the recap channel and logs.")
            elif reports and guild and callable(getattr(reports, 'retry_pending', None)):
                try:
                    await reports.retry_pending(guild)
                except Exception:
                    log.exception('Archived session delivery retry failed')
                    await report_health(self.bot, str(guild.id), 'Session summaries', 'Could not retry an archived recap. Check the recap channel and logs.')
            return count
        except Exception:
            self.last_poll_ok = False
            self.last_poll_partial = False
            raise
        finally:
            self._polling = False

    async def _poll_once(self) -> int:
        """Post ready clubs immediately, then process milestones and session eligibility."""
        self.last_poll_at = time.time()
        self.club_status = getattr(self, "club_status", {})
        clubs = monitored_clubs()
        guild = self.home_guild()
        guild_id = str(guild.id) if guild else None

        async def fetch(club):
            try:
                raw = await self.ea.get_recent_matches_multi(
                    club["club_id"], count=10, bypass_cache=True, allow_stale=False
                )
            except Exception:
                log.exception("Club fetch failed: %s", club["name"])
                raw = _FETCH_FAILED
            observed = (time.time(), time.perf_counter())
            return club, raw, observed

        fetches = [asyncio.create_task(fetch(club)) for club in clubs]
        total, states, jobs = 0, [], {}
        try:
            for finished in asyncio.as_completed(fetches):
                club, raw, observed = await finished
                cid = club["club_id"]
                usable = raw is not None and raw is not _FETCH_FAILED
                partial = usable and not getattr(raw, "complete", True)
                ok = usable and not partial
                state = self.club_status.setdefault(cid, {})
                state.update(club_id=cid, name=club["name"], checked_at=observed[0],
                             ok=ok, partial=partial,
                             error=None if ok else "EA/relay match check incomplete")
                states.append(state)
                try:
                    with club_scope(club):
                        if raw is _FETCH_FAILED:
                            # Even when EA is unavailable, retry existing queued posts.
                            if guild_id:
                                await self._deliver_result_queue(guild_id)
                        else:
                            quiet, new = await self._poll_club(raw, guild, guild_id, ok, observed)
                            total += len(new)
                            if new:
                                jobs[cid] = (quiet, new)
                    if ok:
                        state["success_at"] = time.time()
                except Exception as exc:
                    log.exception("Club check failed: %s", club["name"])
                    state.update(ok=False, partial=False, error=type(exc).__name__)
        finally:
            for task in fetches:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*fetches, return_exceptions=True)

        announcements = []
        for club in clubs:
            with club_scope(club):
                try:
                    if club["club_id"] in jobs:
                        quiet, new = jobs[club["club_id"]]
                        if quiet:
                            await self.check_milestones(guild, announce=False)
                        else:
                            announcements.append((club, guild, new))
                            await self.check_milestones(guild, announce=True, matches=new)
                    if guild_id:
                        await self._deliver_milestones(guild_id)
                except Exception:
                    log.exception("Milestone check failed: %s", club["name"])
        await self._award_badges(announcements)
        await self._announce_records(announcements)

        await self._track_failing_clubs(states)
        # Session summaries wait for every club, except one that has been failing for hours:
        # otherwise a single broken club would stop everyone's recaps.
        responsive = [s for s in states if s['ok'] or s.get('partial')]
        self.last_poll_ok = bool(responsive) and all(s['ok'] or s.get('stale') for s in states)
        self.last_poll_degraded = self.last_poll_ok and any(not s['ok'] for s in states)
        self.last_poll_partial = any(s.get('partial') for s in states) or (not self.last_poll_ok and bool(responsive))
        if states and all(s['ok'] for s in states):
            self.last_complete_at=time.time()
            if guild_id:
                set_setting(guild_id, 'matchday:last_complete', str(int(self.last_complete_at)))
        return total

    async def _track_failing_clubs(self, states: list[dict]):
        now = time.time()
        guild = self.home_guild()
        for s in states:
            key = f"matchday:failing_since:{s['club_id']}"
            saved_value = get_setting(str(guild.id), key) if guild else None
            if s['ok']:
                s['failing_since'] = None
                s['stale'] = False
                if guild and saved_value is not None:
                    set_setting(str(guild.id), key, None)
                if s.pop('alerted', False) and guild:
                    await report_health(self.bot, str(guild.id), f"Club tracking: {s['name']}")
                continue
            saved = to_int(saved_value)
            s['failing_since'] = s.get('failing_since') or (saved if 0 < saved <= now else now)
            if guild and saved_value != str(int(s['failing_since'])):
                set_setting(str(guild.id), key, str(int(s['failing_since'])))
            s['stale'] = now - s['failing_since'] >= STALE_CLUB_SECONDS
            if s['stale'] and not s.get('alerted') and guild:
                s['alerted'] = True
                await report_health(self.bot, str(guild.id), f"Club tracking: {s['name']}",
                                    f"{s['name']}'s EA checks have failed for over 2 hours. Session summaries "
                                    "no longer wait for it. Check its club ID in /admin → Settings → Clubs.")

    def _save_polled_matches(self, raw_matches: list[dict], guild_id: Optional[str], complete: bool):
        """Commit new match history and its queued Discord posts together."""
        cid = club_id()
        quiet_key = f"club:quiet:{cid}"
        with connect() as conn:
            state = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM matches WHERE club_id=?) AS has_matches, "
                "(SELECT value FROM settings WHERE guild_id='global' AND key=?) AS quiet, "
                "(SELECT value FROM settings WHERE guild_id=? AND key=?) AS posting",
                (cid, quiet_key, guild_id, K_ENABLED),
            ).fetchone()
            # Keep the initial backfill quiet until all match types have responded.
            quiet = not state["has_matches"] or bool(state["quiet"])
            new, voided = md.save_polled_matches(conn, cid, raw_matches)
            if quiet and complete and state["quiet"] is not None:
                conn.execute("DELETE FROM settings WHERE guild_id='global' AND key=?", (quiet_key,))
            elif quiet and not complete and state["quiet"] != "1":
                conn.execute(
                    "INSERT INTO settings (guild_id,key,value) VALUES ('global',?,'1') "
                    "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
                    (quiet_key,),
                )
            if new and not quiet and guild_id and state["posting"] != "0":
                queued_at = int(time.time())
                conn.executemany(
                    "INSERT OR IGNORE INTO matchday_outbox VALUES (?,?,?,?)",
                    [(guild_id, cid, match.match_id, queued_at) for match in new],
                )
        return quiet, new, voided

    async def _poll_club(self, raw_matches, guild, guild_id, complete, observed):
        if raw_matches and guild_id:
            self._migrate_old_poll_channel(guild_id)
        quiet, new, voided = await asyncio.to_thread(
            self._save_polled_matches, raw_matches or [], guild_id, complete
        )
        pending = set()
        if new:
            self.last_new_at = time.time()
            log.info("[%s] %d new match(es)%s", club_name(), len(new),
                     " (initial backfill, not posting)" if quiet else "")
            self._linked_in.clear()
            self._unlinked.clear()
            if guild_id:
                for match in new:
                    pending.update(self._log_rotation(guild_id, match))

        if guild_id and (not quiet or not new):
            await self._deliver_result_queue(guild_id, fresh=new, observed=observed)

        if voided and not quiet and guild_id and self.posting_enabled(guild_id):
            cid = self.channel_id_for(guild_id)
            channel = await self._channel(cid) if cid else None
            if channel:
                lines = [f"⚪ **{club_name()}** vs {clip(pm.opp_name, 60)} {reason} — not counted in any stats."
                         for pm, reason in voided]
                try:
                    await channel.send("\n".join(lines)[:2000], allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    log.warning("Couldn't post the void-game note")
        pos_cog = self.bot.get_cog("PositionsCog")
        if new and not quiet and pos_cog and guild:
            ids = {did for match in new for did in self._linked_in.get(match.match_id, [])}
            await pos_cog.check_notes(guild, sorted(ids - pending))
        return quiet, new

    async def _announce_records(self, jobs: list):
        """'New club record!' post when this check's games broke one (biggest win, most goals in a game...)."""
        import records
        for club, guild, new in jobs:
            if not guild or not new or not self.posting_enabled(str(guild.id)):
                continue
            try:
                key = f"records:streak:{club['club_id']}"
                lines, streak = await asyncio.to_thread(
                    records.broken_by, club["club_id"], {pm.match_id for pm in new if pm.club_id == club["club_id"]},
                    get_setting("global", key))
                if streak != get_setting("global", key):
                    set_setting("global", key, streak)
                if not lines:
                    continue
                cid = self.channel_id_for(str(guild.id))
                channel = await self._channel(cid) if cid else None
                if channel:
                    embed = discord.Embed(title=f"📜 New {club['name']} record{'s' if len(lines) > 1 else ''}!",
                                          description="\n".join(lines)[:4000], colour=0xF1C40F)
                    embed.set_footer(text="See every record with /records or 🏆 Records on the panel")
                    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                log.exception("Record check failed: %s", club["name"])

    async def _award_badges(self, jobs: list):
        """Badges for everyone in this check's new games (all clubs at once, one announcement)."""
        badges = self.bot.get_cog("BadgesCog")
        guild = next((g for _, g, _ in jobs if g), None)
        if not badges or not guild:
            return
        try:
            await badges.after_matches(guild, [pm for _, _, new in jobs for pm in new])
        except Exception:
            log.exception("Badge check failed")

    def _log_rotation(self, guild_id: str, pm: md.ParsedMatch) -> dict[str, str]:
        """
        Log a separately sourced exact position or Unknown; never ask after a match.
        EA match roles stay in match_players and are not replaced by preferences.
        """
        if is_match_processed(guild_id, club_name(), pm.match_id):
            return {}  # already logged (e.g. by the old rotation poller)
        import positions as POS
        linked,unmatched=[],[]
        links = {name.casefold(): did for did, name in get_all_links(guild_id).items()}
        when=datetime.fromtimestamp(pm.ts,timezone.utc).isoformat() if pm.ts else datetime.now(timezone.utc).isoformat()
        for player in pm.players:
            did=links.get(player.name.casefold())
            if not did:
                unmatched.append(player.name)
                continue
            result=POS.detect_position(guild_id,did,player.pos,pm.ts,player.exact_position)
            POS.record_position(guild_id,did,pm.match_id,result,when)
            linked.append(did)
        mark_match_processed(guild_id, club_name(), pm.match_id)
        if unmatched:
            log.info(f"Match {pm.match_id}: not linked — {', '.join(unmatched)}")
        self._linked_in[pm.match_id] = linked
        self._unlinked[pm.match_id] = unmatched
        return {}

    async def _deliver_result_queue(self, guild_id, *, fresh=(), observed=None):
        """Retry only explicitly queued result posts, never old saved history."""
        key=f'{guild_id}:{club_id()}'
        with connect() as conn:
            if not self.posting_enabled(guild_id):
                conn.execute('DELETE FROM matchday_outbox WHERE guild_id=? AND club_id=?',(guild_id,club_id()))
                conn.execute("DELETE FROM delivery_jobs WHERE kind='result' AND job_key=?",(key,))
                return
            conn.execute('DELETE FROM matchday_outbox WHERE guild_id=? AND club_id=? AND queued_at<?',
                         (guild_id,club_id(),int(time.time())-86400))
            rows=conn.execute('SELECT m.match_id,m.raw_json FROM matchday_outbox q JOIN matches m ON m.club_id=q.club_id AND m.match_id=q.match_id '
                'WHERE q.guild_id=? AND q.club_id=? ORDER BY m.ts,m.match_id LIMIT 100',(guild_id,club_id())).fetchall()
        if not rows:
            delivery.ack('result',key)
            return
        delivery.enqueue('result',key,guild_id,club_id(),{})
        job=delivery.get('result',key)
        if job and job['expires_at']<=time.time():
            delivery.ack('result',key)
            delivery.enqueue('result',key,guild_id,club_id(),{})
            job=delivery.get('result',key)
        if not delivery.due(job):
            return
        cid=self.channel_id_for(guild_id)
        channel=await self._channel(cid) if cid else None
        if channel is None:
            delivery.fail(job,RuntimeError('Result channel unavailable'))
            return
        fresh_by_id = {match.match_id: match for match in fresh}
        games = [
            fresh_by_id.get(row["match_id"]) or md.parse_match(json.loads(row["raw_json"]), club_id())
            for row in rows if row["raw_json"]
        ]
        games = [match for match in games if match is not None]
        if games:
            try:
                success=await self._post_results(
                    channel, games, guild_id, observed=observed, fresh_ids=fresh_by_id
                )
            except Exception as exc:
                delivery.fail(job,exc)
                raise
            if success is False:
                delivery.fail(job,RuntimeError('Discord result delivery failed'))
            else:
                delivery.ack('result',key)

    def _ack_results(self, guild_id, games):
        if guild_id and games:
            with connect() as conn:
                conn.executemany('DELETE FROM matchday_outbox WHERE guild_id=? AND club_id=? AND match_id=?',
                                 [(guild_id,p.club_id,p.match_id) for p in games])

    async def _post_results(self, channel: discord.abc.Messageable, new: list[md.ParsedMatch],
                            guild_id: Optional[str] = None, pending_by_match: Optional[dict] = None,
                            *, observed=None, fresh_ids=()):
        recent = md.recent_results(club_id(), 5)
        footer = f"Form {md.form_string(recent)} • Streak {md.streak([r['result'] for r in recent])}"
        to_post = new[-MAX_POSTS_PER_POLL:]
        skipped = new[:-MAX_POSTS_PER_POLL]
        try:
            if skipped:
                summary = ", ".join(f"{md.RESULT_EMOJI[p.result]} {p.our_goals}–{p.opp_goals} {p.opp_name}"
                                    for p in skipped)
                await channel.send(f"📡 Caught up on {len(skipped)} earlier result(s): {clip(summary, 1800)}")
                self._ack_results(guild_id,skipped)
            for i, pm in enumerate(to_post):
                embed, file = await md.match_post(pm, footer_extra=footer if i == len(to_post) - 1 else "", compact=True)
                rematch = md.rematch_line(club_id(), pm)
                if rematch:
                    embed.add_field(name="🔁 Rematch", value=rematch, inline=False)
                extra = {"file": file} if file else {}
                view = scout_view(pm.opp_id, pm.opp_name, club_id())  # 🔍 Scout opponent
                from cogs.result_details import ResultDetails
                view = view or discord.ui.View(timeout=None)
                view.add_item(ResultDetails(pm.club_id, pm.match_id))
                extra['view'] = view
                sending_at = time.perf_counter()
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(), **extra)
                if observed and pm.match_id in fresh_ids:
                    observed_wall, observed_clock = observed
                    wait = observed_wall - pm.ts if 0 < pm.ts <= observed_wall else None
                    state = self.club_status[pm.club_id]
                    state["last_result_timing"] = {
                        "ea_wait": wait,
                        "processing": sending_at - observed_clock,
                        "discord": time.perf_counter() - sending_at,
                    }
                self._ack_results(guild_id,[pm])
        except discord.Forbidden:
            log.warning("No permission to post in the matchday channel")
            await report_health(self.bot, guild_id, "Match result posting", "The bot can't post results. Check its matchday channel permissions.")
            return False
        except discord.HTTPException:
            log.warning('Result delivery failed; unsent results remain queued')
            await report_health(self.bot,guild_id,'Match result posting','Discord could not accept a result post. Unsent results will retry automatically.')
            return False
        await report_health(self.bot, guild_id, "Match result posting")

        # EA's MOTM stays on the result card; automatic squad voting is retired.
        return True

    async def check_milestones(self, guild: Optional[discord.Guild], announce: bool,
                               matches: Optional[list[md.ParsedMatch]] = None):
        # Bind eligibility to this poll's actual match participants, not the roster.
        # Catch-up polls can contain several games: keep each player's latest one.
        latest = {}
        for pm in sorted(matches or [], key=lambda m: (m.ts, m.match_id)):
            if pm.club_id != club_id():
                continue
            for player in pm.players:
                latest[player.name.casefold()] = pm
        members = await self.ea.get_member_stats(club_id(), career=True, bypass_cache=True)
        if not members:
            return
        announcements = {}
        with connect() as conn:
            for m in members:
                name = m.get("name")
                if not name:
                    continue
                for stat, (label, thresholds) in MILESTONES.items():
                    if m.get(stat) is None:
                        continue  # Missing EA totals must not reset a saved baseline to zero.
                    new_val = to_int(m.get(stat))
                    row = conn.execute(
                        "SELECT value FROM stat_snapshots WHERE club_id=? AND player_name=? AND stat=?",
                        (club_id(), name, stat)).fetchone()
                    played = latest.get(name.casefold())
                    if row is not None and announce and played is not None:
                        hit = crossed(row["value"], new_val, thresholds)
                        # EA career totals are the same whichever club you look them up for, so the
                        # same milestone would be announced once per club: only the first one counts.
                        conn.execute("CREATE TABLE IF NOT EXISTS milestone_posts (player TEXT, stat TEXT, "
                                     "threshold INTEGER, PRIMARY KEY (player, stat, threshold))")
                        if hit and conn.execute("INSERT OR IGNORE INTO milestone_posts VALUES (?,?,?)",
                                                (name.casefold(), stat, hit)).rowcount == 0:
                            hit = None
                        if hit:
                            payload = announcements.setdefault(played.match_id, {
                                'club': club_name(), 'score': f'{played.our_goals}–{played.opp_goals}',
                                'opponent': played.opp_name, 'items': []})
                            payload['items'].append({'name': name, 'stat': stat, 'threshold': hit, 'label': label})
                    # Refresh absent players silently so historical increases don't
                    # become announcements when they next appear in a match.
                    conn.execute(
                        "INSERT INTO stat_snapshots (club_id, player_name, stat, value) VALUES (?,?,?,?) "
                        "ON CONFLICT(club_id, player_name, stat) DO UPDATE SET value=excluded.value",
                        (club_id(), name, stat, new_val))
            if guild and self.posting_enabled(str(guild.id)):
                for mid, payload in announcements.items():
                    delivery.enqueue('milestone', f'{guild.id}:{club_id()}:{mid}', str(guild.id), club_id(), payload, conn=conn)
        if guild:
            await self._deliver_milestones(str(guild.id))

    async def _deliver_milestones(self, gid):
        import milestones
        with connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM delivery_jobs WHERE kind='milestone' AND guild_id=? AND club_id=?", (gid, club_id()))]
        if not rows:
            return
        enabled = milestones.settings(gid)
        channel = None
        for job in rows:
            if not self.posting_enabled(gid) or job['expires_at'] <= time.time():
                delivery.ack('milestone', job['job_key'])
                continue
            if not delivery.due(job):
                continue
            embed = milestones.card(json.loads(job['payload']), enabled)
            if embed is None:
                delivery.ack('milestone', job['job_key'])
                continue
            try:
                cid = self.channel_id_for(gid)
                channel = channel or (await self._channel(cid) if cid else None)
                if channel is None:
                    raise RuntimeError('Milestone channel unavailable')
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                delivery.ack('milestone', job['job_key'])
            except Exception as exc:
                delivery.fail(job, exc)
                log.warning('Milestone card delivery failed; retry scheduled (%s)', type(exc).__name__)

    # ------------------------------------------------------------------ #
    #  weekly recap
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=5)
    @survive
    async def recap_loop(self):
        guild = self.home_guild()
        if not guild:
            return
        gid = str(guild.id)
        local = datetime.now(_tz())
        if local.weekday() != RECAP_WEEKDAY or local.hour < RECAP_HOUR:
            return
        week_key = f"{local.isocalendar().year}-W{local.isocalendar().week}"
        if not self.posting_enabled(gid):
            return
        cid = self.channel_id_for(gid)
        channel = await self._channel(cid) if cid else None
        key = f'{K_LAST_RECAP}:combined'
        if not channel or get_setting(gid, key) == week_key:
            return
        # Deployment during an already-posted week must not send a duplicate.
        legacy_keys = [K_LAST_RECAP] + [f"{K_LAST_RECAP}:{c['club_id']}" for c in monitored_clubs() if c['club_id'] != CLUB_ID]
        if any(get_setting(gid, old) == week_key for old in legacy_keys):
            set_setting(gid, key, week_key)
            return
        try:
            end = local.replace(hour=RECAP_HOUR, minute=0, second=0, microsecond=0)
            embed = await asyncio.to_thread(build_recap_embed, 7, end, combined=True)
            if embed:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            set_setting(gid, key, week_key)
            for old in legacy_keys:
                set_setting(gid, old, week_key)
        except discord.HTTPException:
            log.warning('Could not post combined weekly recap')

    @recap_loop.before_loop
    async def _before_recap(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    #  commands
    # ------------------------------------------------------------------ #
    matchday_group = app_commands.Group(name="matchday", description="Automatic match result posting")

    @matchday_group.command(name="start", description="Post match results in this channel automatically (stays on)")
    @app_commands.describe(channel="Where to post (defaults to this channel)")
    @club_scoped
    async def matchday_start(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not is_manager(interaction.user):
            failed(interaction)
            await interaction.response.send_message("You need the Manager/Admin role for this.", ephemeral=True)
            return
        channel = channel or interaction.channel
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                f"I can't post embeds in {channel.mention} — give me Send Messages + Embed Links there first.",
                ephemeral=True)
            return
        gid = str(interaction.guild_id)
        set_setting(gid, K_CHANNEL, str(channel.id))
        set_setting(gid, K_ENABLED, "1")
        shared = announce_channel_id(gid)
        if shared and shared != str(channel.id):
            await interaction.response.send_message(
                f"📡 Result posting is **on**. An announcements channel is set (<#{shared}>), so results, "
                f"MOTM votes and recaps will post there — {channel.mention} is only a fallback. "
                f"Use `/announce set` to change it.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"📡 Match results for **{club_name()}** will post in {channel.mention} automatically — "
            f"no need to run this again after restarts. Checking every {POLL_ACTIVE_MINUTES:g} min while you're "
            f"playing and every {POLL_IDLE_MINUTES:g} min otherwise. Weekly recap goes here too.")

    @matchday_group.command(name="stop", description="Stop posting results (matches are still tracked)")
    @club_scoped
    async def matchday_stop(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            failed(interaction)
            await interaction.response.send_message("You need the Manager/Admin role for this.", ephemeral=True)
            return
        set_setting(str(interaction.guild_id), K_ENABLED, "0")
        await interaction.response.send_message(
            "🛑 Stopped posting results. The bot still records every match in the background, so stats, "
            "/form and rotation stay up to date. `/matchday start` turns posting back on.")

    @matchday_group.command(name="status", description="Show what the match tracker is doing")
    @club_scoped
    async def matchday_status(self, interaction: discord.Interaction):
        from cogs.usage import can_view
        if not can_view(interaction.user, interaction.guild):
            await interaction.response.send_message("This status report is private.", ephemeral=True)
            return
        gid = str(interaction.guild_id)
        cid = self.channel_id_for(gid)
        enabled = self.posting_enabled(gid)
        reason = self.active_reason()
        active = reason is not None
        lines = [
            f"**Posting:** {'on' if enabled and cid else 'off'}" + (f" in <#{cid}>" if cid else " (no channel — run `/matchday start`)"),
            f"**Mode:** {'🟢 active' if active else '💤 idle'} — checks every "
            f"{POLL_ACTIVE_MINUTES if active else POLL_IDLE_MINUTES:g} min" + (f" ({reason})" if reason else
            " (speeds up when 2+ of the squad are in voice, a /session is on, or you just played)"),
            "**Last check:** " + (f"<t:{int(self.last_poll_at)}:R> ({'partial — some match types unavailable' if getattr(self, 'last_poll_partial', False) else 'ok' if self.last_poll_ok else 'failed — relay/EA unreachable'})"
                                   if self.last_poll_at else "not yet"),
            f"**Next check:** <t:{int(self.next_poll_at)}:R>" if self.next_poll_at else "",
            "**Clubs monitored:** " + ', '.join(c['name'] for c in monitored_clubs()),
            f"**{club_name()} matches stored:** {md.match_count(club_id())}",
        ]
        last_ts = md.latest_match_ts(club_id())
        if last_ts:
            lines.append(f"**Latest match:** <t:{last_ts}:R>")
        await interaction.response.send_message("\n".join(l for l in lines if l), ephemeral=True)

    @matchday_group.command(name="check", description="Check EA for new matches right now")
    @app_commands.checks.cooldown(1, 60, key=lambda i: i.guild_id)
    @club_scoped
    async def matchday_check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if self._polling:
            await interaction.followup.send("A check is already running — give it a few seconds.", ephemeral=True)
            return
        n = await self.poll_once()
        if getattr(self, "last_poll_partial", False):
            await interaction.followup.send(f"Checked part of EA history — {n} new match(es). Available results post normally; session summaries wait until every match type can be checked.", ephemeral=True)
        elif not self.last_poll_ok:
            await interaction.followup.send("Couldn't reach EA (the relay may be offline). Try again later.",
                                            ephemeral=True)
        else:
            await interaction.followup.send(f"✅ Checked — {n} new match(es)." if n else "✅ Checked — nothing new yet.",
                                            ephemeral=True)

    @app_commands.command(name="recap", description="Summary of recent games (default: last 7 days)")
    @app_commands.describe(days="How many days back (1–90)")
    @club_scoped
    async def recap(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 90] = 7):
        embed = build_recap_embed(days)
        if embed is None:
            await interaction.response.send_message(f"No tracked matches in the last {days} days.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchdayCog(bot))
