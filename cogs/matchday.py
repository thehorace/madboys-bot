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

Polling speed adapts: every POLL_ACTIVE_MINUTES (default 2) while you're
playing — a match finished recently, or a /session is on — and every
POLL_IDLE_MINUTES (default 10) otherwise, so EA/the home relay isn't hammered
at 4am.

The first time it runs against an empty database it saves what EA currently
has *without* posting it, so the channel doesn't get spammed with old games.

Commands:
  /matchday start [channel]  - Manager: post results in this (or the given) channel. Saved permanently.
  /matchday stop             - Manager: stop posting (matches are still tracked silently)
  /matchday status           - What the tracker is doing
  /matchday check            - Check for new matches right now
  /recap [days]              - Summary of the last N days (default 7)
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import match_data as md
from cogs.link import find_discord_id_by_ea_name, get_all_links
from cogs.rotation import is_match_processed, log_positions, mark_match_processed
from config import (ACTIVE_WINDOW_MINUTES, BOT_TZ, CLUB_COLOUR, CLUB_ID, CLUB_NAME, GUILD_ID,
                    MATCHDAY_CHANNEL_ID, POLL_ACTIVE_MINUTES, POLL_IDLE_MINUTES, RECAP_HOUR, RECAP_WEEKDAY)
from db import connect, get_setting, set_setting
from utils import clip, is_manager, to_int

log = logging.getLogger("madboys-bot.matchday")

# EA's per-player "pos" bucket -> rotation role
POSITION_MAP = {"goalkeeper": "GK", "defender": "DEF", "midfielder": "MID", "forward": "FWD"}

MAX_POSTS_PER_POLL = 3  # if the bot was offline and finds 8 new games, don't flood the channel

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


def build_recap_embed(days: int = 7, end: Optional[datetime] = None) -> Optional[discord.Embed]:
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    rows = md.matches_between(CLUB_ID, int(start.timestamp()), int(end.timestamp()) + 1)
    if not rows:
        return None

    w = sum(r["result"] == "W" for r in rows)
    d = sum(r["result"] == "D" for r in rows)
    l = sum(r["result"] == "L" for r in rows)
    gf = sum(r["our_goals"] for r in rows)
    ga = sum(r["opp_goals"] for r in rows)
    newest_first = list(reversed(rows))

    embed = discord.Embed(
        title=f"🗓️ {CLUB_NAME} — {'Weekly' if days == 7 else f'{days}-day'} Recap",
        description=f"**{len(rows)} games** • W{w} D{d} L{l} • {gf} scored, {ga} conceded\n"
                    f"{md.form_string(newest_first[:15])}",
        colour=CLUB_COLOUR,
    )

    totals = md.player_totals(CLUB_ID, int(start.timestamp()), int(end.timestamp()) + 1)

    def top(key: str, fmt, min_games: int = 1):
        pool = [t for t in totals if (t["games"] or 0) >= min_games and t[key]]
        if not pool:
            return "—"
        best = max(pool, key=lambda t: t[key])
        return f"**{best['name']}** — {fmt(best)}"

    embed.add_field(name="⚽ Top scorer", value=top("goals", lambda t: f"{t['goals']} goals"), inline=True)
    embed.add_field(name="🅰️ Most assists", value=top("assists", lambda t: f"{t['assists']} assists"), inline=True)
    embed.add_field(name="⭐ Most MOTMs", value=top("motm", lambda t: f"{t['motm']}"), inline=True)
    embed.add_field(name="📈 Best avg rating",
                    value=top("rating", lambda t: f"{t['rating']:.2f} over {t['games']} games",
                              min_games=max(2, len(rows) // 3)), inline=True)

    wins = [r for r in rows if r["result"] == "W"]
    if wins:
        big = max(wins, key=lambda r: (r["our_goals"] - r["opp_goals"], r["our_goals"]))
        embed.add_field(name="💥 Biggest win", value=f"{big['our_goals']}–{big['opp_goals']} vs {big['opp_name']}",
                        inline=True)
    embed.set_footer(text=f"{CLUB_NAME} • from matches tracked by the bot")
    return embed


class MatchdayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ea = bot.ea
        self.next_poll_at = 0.0
        self.last_poll_at: Optional[float] = None
        self.last_poll_ok: Optional[bool] = None
        self.last_new_at: Optional[float] = None
        self._polling = False
        self._migrated = False
        self.ticker.start()
        self.recap_loop.start()

    def cog_unload(self):
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
                               (guild_id, CLUB_NAME)).fetchone()
        if row:
            set_setting(guild_id, K_CHANNEL, row["channel_id"])
            log.info(f"Migrated matchday channel {row['channel_id']} from old matchday_poll table")

    def channel_id_for(self, guild_id: str) -> Optional[int]:
        cid = get_setting(guild_id, K_CHANNEL) or MATCHDAY_CHANNEL_ID
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
    def is_active(self) -> bool:
        now = time.time()
        window = ACTIVE_WINDOW_MINUTES * 60
        last = md.latest_match_ts(CLUB_ID)
        if last and now - last < window:
            return True
        with connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE cancelled=0 AND starts_at BETWEEN ? AND ? LIMIT 1",
                (int(now - window - 3 * 3600), int(now + 15 * 60)),
            ).fetchone()
        # a session counts as "on" from 15 min before start to ~3h + window after
        return row is not None

    def current_interval_minutes(self) -> float:
        return POLL_ACTIVE_MINUTES if self.is_active() else POLL_IDLE_MINUTES

    @tasks.loop(seconds=30)
    async def ticker(self):
        if time.time() < self.next_poll_at or self._polling:
            return
        try:
            await self.poll_once()
        except Exception:
            log.exception("Match tracker poll failed")
        finally:
            self.next_poll_at = time.time() + self.current_interval_minutes() * 60

    @ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    #  the actual work
    # ------------------------------------------------------------------ #
    async def poll_once(self) -> int:
        """Fetch, store, log rotation, post, milestones. Returns number of new matches."""
        self._polling = True
        try:
            return await self._poll_once()
        finally:
            self._polling = False

    async def _poll_once(self) -> int:
        self.last_poll_at = time.time()
        raw_matches = await self.ea.get_recent_matches_multi(CLUB_ID, count=10, bypass_cache=True, allow_stale=False)
        self.last_poll_ok = raw_matches is not None
        if not raw_matches:
            return 0

        guild = self.home_guild()
        guild_id = str(guild.id) if guild else None
        if guild_id:
            self._migrate_old_poll_channel(guild_id)

        first_run = md.match_count(CLUB_ID) == 0

        new: list[md.ParsedMatch] = []
        for raw in sorted(raw_matches, key=lambda m: to_int(m.get("timestamp"))):  # oldest first
            pm = md.parse_match(raw, CLUB_ID)
            if pm and md.store_match(CLUB_ID, pm, raw):
                new.append(pm)

        if not new:
            return 0
        self.last_new_at = time.time()
        log.info(f"[{CLUB_NAME}] {len(new)} new match(es){' (initial backfill, not posting)' if first_run else ''}")

        if guild_id:
            for pm in new:
                self._log_rotation(guild_id, pm)

        if first_run:
            await self.check_milestones(guild, announce=False)  # seed the baseline
            return len(new)

        if guild_id and self.posting_enabled(guild_id):
            cid = self.channel_id_for(guild_id)
            channel = await self._channel(cid) if cid else None
            if channel:
                await self._post_results(channel, new)

        await self.check_milestones(guild, announce=True)
        return len(new)

    def _log_rotation(self, guild_id: str, pm: md.ParsedMatch):
        if is_match_processed(guild_id, CLUB_NAME, pm.match_id):
            return  # the old rotation poller already logged this one
        entries, unmatched = [], []
        for p in pm.players:
            did = find_discord_id_by_ea_name(guild_id, p.name)
            role = extract_role(p.pos)
            if did and role:
                entries.append((did, role))
            elif not did:
                unmatched.append(p.name)
        when = datetime.fromtimestamp(pm.ts, timezone.utc).isoformat() if pm.ts else None
        log_positions(guild_id, CLUB_NAME, entries, source="auto", logged_at=when)
        mark_match_processed(guild_id, CLUB_NAME, pm.match_id)
        if unmatched:
            log.info(f"Match {pm.match_id}: not linked — {', '.join(unmatched)}")

    async def _post_results(self, channel: discord.abc.Messageable, new: list[md.ParsedMatch]):
        recent = md.recent_results(CLUB_ID, 5)
        footer = f"Form {md.form_string(recent)} • Streak {md.streak([r['result'] for r in recent])}"
        to_post = new[-MAX_POSTS_PER_POLL:]
        skipped = new[:-MAX_POSTS_PER_POLL]
        try:
            if skipped:
                summary = ", ".join(f"{md.RESULT_EMOJI[p.result]} {p.our_goals}–{p.opp_goals} {p.opp_name}"
                                    for p in skipped)
                await channel.send(f"📡 Caught up on {len(skipped)} earlier result(s): {clip(summary, 1800)}")
            for i, pm in enumerate(to_post):
                embed = md.match_embed(pm, footer_extra=footer if i == len(to_post) - 1 else "")
                await channel.send(content="📡 **Full time!**", embed=embed)
        except discord.Forbidden:
            log.warning("No permission to post in the matchday channel")

    async def check_milestones(self, guild: Optional[discord.Guild], announce: bool):
        members = await self.ea.get_member_stats(CLUB_ID, career=True, bypass_cache=True)
        if not members:
            return
        announcements = []
        links = {v.lower(): k for k, v in get_all_links(str(guild.id)).items()} if guild else {}
        with connect() as conn:
            for m in members:
                name = m.get("name")
                if not name:
                    continue
                for stat, (label, thresholds) in MILESTONES.items():
                    new_val = to_int(m.get(stat))
                    row = conn.execute(
                        "SELECT value FROM stat_snapshots WHERE club_id=? AND player_name=? AND stat=?",
                        (CLUB_ID, name, stat)).fetchone()
                    if row is not None and announce:
                        hit = crossed(row["value"], new_val, thresholds)
                        if hit:
                            who = f"<@{links[name.lower()]}>" if name.lower() in links else f"**{name}**"
                            announcements.append(f"🎉 {who} just hit **{hit} {label}**!")
                    conn.execute(
                        "INSERT INTO stat_snapshots (club_id, player_name, stat, value) VALUES (?,?,?,?) "
                        "ON CONFLICT(club_id, player_name, stat) DO UPDATE SET value=excluded.value",
                        (CLUB_ID, name, stat, new_val))

        if announcements and guild and self.posting_enabled(str(guild.id)):
            cid = self.channel_id_for(str(guild.id))
            channel = await self._channel(cid) if cid else None
            if channel:
                try:
                    await channel.send("\n".join(announcements)[:2000])
                except discord.Forbidden:
                    pass

    # ------------------------------------------------------------------ #
    #  weekly recap
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=5)
    async def recap_loop(self):
        guild = self.home_guild()
        if not guild:
            return
        gid = str(guild.id)
        local = datetime.now(_tz())
        if local.weekday() != RECAP_WEEKDAY or local.hour < RECAP_HOUR:
            return
        week_key = f"{local.isocalendar().year}-W{local.isocalendar().week}"
        if get_setting(gid, K_LAST_RECAP) == week_key:
            return
        set_setting(gid, K_LAST_RECAP, week_key)
        if not self.posting_enabled(gid):
            return
        embed = build_recap_embed(7)
        cid = self.channel_id_for(gid)
        channel = await self._channel(cid) if cid else None
        if embed and channel:
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                pass

    @recap_loop.before_loop
    async def _before_recap(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    #  commands
    # ------------------------------------------------------------------ #
    matchday_group = app_commands.Group(name="matchday", description="Automatic match result posting")

    @matchday_group.command(name="start", description="Post match results in this channel automatically (stays on)")
    @app_commands.describe(channel="Where to post (defaults to this channel)")
    async def matchday_start(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not is_manager(interaction.user):
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
        await interaction.response.send_message(
            f"📡 Match results for **{CLUB_NAME}** will post in {channel.mention} automatically — "
            f"no need to run this again after restarts. Checking every {POLL_ACTIVE_MINUTES:g} min while you're "
            f"playing and every {POLL_IDLE_MINUTES:g} min otherwise. Weekly recap goes here too.")

    @matchday_group.command(name="stop", description="Stop posting results (matches are still tracked)")
    async def matchday_stop(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            await interaction.response.send_message("You need the Manager/Admin role for this.", ephemeral=True)
            return
        set_setting(str(interaction.guild_id), K_ENABLED, "0")
        await interaction.response.send_message(
            "🛑 Stopped posting results. The bot still records every match in the background, so stats, "
            "/form and rotation stay up to date. `/matchday start` turns posting back on.")

    @matchday_group.command(name="status", description="Show what the match tracker is doing")
    async def matchday_status(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        cid = self.channel_id_for(gid)
        enabled = self.posting_enabled(gid)
        active = self.is_active()
        lines = [
            f"**Posting:** {'on' if enabled and cid else 'off'}" + (f" in <#{cid}>" if cid else " (no channel — run `/matchday start`)"),
            f"**Mode:** {'🟢 active' if active else '💤 idle'} — checks every {self.current_interval_minutes():g} min",
            "**Last check:** " + (f"<t:{int(self.last_poll_at)}:R> ({'ok' if self.last_poll_ok else 'failed — relay/EA unreachable'})"
                                   if self.last_poll_at else "not yet"),
            f"**Next check:** <t:{int(self.next_poll_at)}:R>" if self.next_poll_at else "",
            f"**Matches stored:** {md.match_count(CLUB_ID)}",
        ]
        last_ts = md.latest_match_ts(CLUB_ID)
        if last_ts:
            lines.append(f"**Latest match:** <t:{last_ts}:R>")
        await interaction.response.send_message("\n".join(l for l in lines if l), ephemeral=True)

    @matchday_group.command(name="check", description="Check EA for new matches right now")
    @app_commands.checks.cooldown(1, 60, key=lambda i: i.guild_id)
    async def matchday_check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if self._polling:
            await interaction.followup.send("A check is already running — give it a few seconds.", ephemeral=True)
            return
        n = await self.poll_once()
        self.next_poll_at = time.time() + self.current_interval_minutes() * 60
        if not self.last_poll_ok:
            await interaction.followup.send("Couldn't reach EA (the relay may be offline). Try again later.",
                                            ephemeral=True)
        else:
            await interaction.followup.send(f"✅ Checked — {n} new match(es)." if n else "✅ Checked — nothing new yet.",
                                            ephemeral=True)

    @app_commands.command(name="recap", description="Summary of recent games (default: last 7 days)")
    @app_commands.describe(days="How many days back (1–90)")
    async def recap(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 90] = 7):
        embed = build_recap_embed(days)
        if embed is None:
            await interaction.response.send_message(f"No tracked matches in the last {days} days.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchdayCog(bot))
