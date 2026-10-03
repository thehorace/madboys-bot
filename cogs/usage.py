"""
Bot usage stats — private, for the bot's admins only.

Every slash command, button tap and dropdown pick on the bot is logged
(who, what, what they picked, when). Nothing else: normal chat messages are
never read or stored. /usage opens a private report:

  📊 Overview   totals, busiest hours/days, top people, top features
  👥 People     who uses the bot most, their favourite feature, last seen
  🧭 Features   which buttons/commands get used, and by how many people
  🔎 Lookups    what people look up: players, leaderboards, opponents...
  ▾ One person  everything one person did: their features, lookups, last 15 actions
  📄 CSV        download the raw log for the chosen period

Who can open it: the Discord usernames or IDs in USAGE_VIEWERS (comma list), or,
if that isn't set, fauz. Everyone else gets "this is private".
Entries older than USAGE_KEEP_DAYS (default 180) are deleted at startup.
"""

import csv
import io
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_TZ, CLUB_COLOUR
from db import connect

log = logging.getLogger("madboys-bot.usage")

USAGE_VIEWERS = {v.strip().lower() for v in (os.getenv("USAGE_VIEWERS") or "fauz").split(",") if v.strip()}
USAGE_KEEP_DAYS = int(os.getenv("USAGE_KEEP_DAYS", "180"))

PERIODS = {"1d": ("Last 24 hours", 86400), "7d": ("Last 7 days", 7 * 86400),
           "30d": ("Last 30 days", 30 * 86400), "all": ("All time", None)}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    guild_id   TEXT,
    user_id    TEXT NOT NULL,
    user_name  TEXT,
    kind       TEXT,      -- command / button / dropdown
    action     TEXT,      -- e.g. "/lastgame", "Last game", "Player stats"
    detail     TEXT,      -- what they picked, e.g. "Killa", "goals"
    channel_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log (guild_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_log (guild_id, user_id, ts);
"""


def init_usage():
    with connect() as conn:
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(usage_log)")}
        for column, declaration in (("interaction_id", "TEXT"), ("outcome", "TEXT NOT NULL DEFAULT 'unknown'"),
                                    ("category", "TEXT NOT NULL DEFAULT 'action'")):
            if column not in have:
                conn.execute(f"ALTER TABLE usage_log ADD COLUMN {column} {declaration}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_interaction ON usage_log (interaction_id)")
        conn.execute("DELETE FROM usage_log WHERE ts < ?", (int(time.time()) - USAGE_KEEP_DAYS * 86400,))


# --------------------------------------------------------------------------- #
#  Turning an interaction into (kind, action, detail)
# --------------------------------------------------------------------------- #
def clean(text: Optional[str]) -> str:
    """'👤 Player stats…' -> 'Player stats'"""
    t = re.sub(r"^[^\w<]+", "", text or "").strip()
    return re.sub(r"(…|\.\.\.|\?)$", "", t).strip()


def _user_label(data: dict, uid: str, guild: Optional[discord.Guild]) -> str:
    m = guild.get_member(int(uid)) if guild else None
    if m:
        return m.display_name
    u = (data.get("resolved") or {}).get("users", {}).get(uid) or {}
    return u.get("global_name") or u.get("username") or uid


def describe_command(data: dict, guild: Optional[discord.Guild]) -> tuple[str, str]:
    name, opts = "/" + data.get("name", "?"), data.get("options") or []
    while opts and opts[0].get("type") in (1, 2):   # sub-command / group
        name += " " + opts[0]["name"]
        opts = opts[0].get("options") or []
    parts = []
    for o in opts:
        v = o.get("value")
        if o.get("type") == 6:   # user option
            v = _user_label(data, str(v), guild)
        parts.append(f"{o['name']}={v}")
    return name, ", ".join(parts)


def _find_component(message: Optional[discord.Message], custom_id: str):
    for row in getattr(message, "components", None) or []:
        for c in getattr(row, "children", None) or [row]:
            if getattr(c, "custom_id", None) == custom_id:
                return c
    return None


# Dropdowns whose placeholder changes with what's on screen -> a stable feature name
DYNAMIC = [
    (re.compile(r"^Compare (.+) with$"), "Compare", "{0} vs {v}"),
    (re.compile(r"^Who plays (\w+)$"), "Lineup builder: pick player", "{0}: {v}"),
    (re.compile(r"^Tick (.+)'s builds$"), "Set someone's builds", "{0}: {v}"),
    (re.compile(r"^Player: .*$"), "Set someone's builds: pick player", "{v}"),
]
RENAME = {"Career": "Season/Career toggle", "This season": "Season/Career toggle"}


def describe_component(data: dict, message: Optional[discord.Message],
                       guild: Optional[discord.Guild]) -> tuple[str, str, str]:
    cid = data.get("custom_id", "")
    comp = _find_component(message, cid)
    values = [str(v) for v in data.get("values") or []]
    if data.get("component_type") == 2:   # button
        action = clean(getattr(comp, "label", None)) or cid
        return "button", RENAME.get(action, action), ""
    # dropdown: show the option labels people picked (or user names for a user picker)
    labels = {o.value: o.label for o in getattr(comp, "options", None) or []}
    picked = ", ".join(labels.get(v) or (_user_label(data, v, guild) if v.isdigit() else v) for v in values)
    placeholder = clean(getattr(comp, "placeholder", None)) or cid
    for rx, name, fmt in DYNAMIC:
        m = rx.match(placeholder)
        if m:
            return "dropdown", name, fmt.format(*m.groups(), v=picked)
    return "dropdown", placeholder, picked


def category_for(action: str, detail: str) -> str:
    command = action.split(" ")[0].lower()
    lookup_commands = {"/lastgame", "/clubstats", "/playerstats", "/me", "/leaderboard", "/passing", "/compare", "/form", "/h2h", "/recap"}
    lookup_components = {"Player stats", "Leaderboards", "Compare", "Head-to-head vs", "Last game", "Club", "Form", "Recap", "Me", "My stats", "Leaderboard"}
    if command in lookup_commands or action in lookup_components:
        return "lookup"
    if action in {"Home", "Stats", "Career", "Season/Career toggle", "This season"} or command == "/stats":
        return "navigation"
    return "action"


def log_interaction(interaction: discord.Interaction, outcome: str = "pending"):
    if interaction.user.bot or interaction.guild_id is None:
        return
    data = interaction.data or {}
    if interaction.type == discord.InteractionType.application_command:
        kind, (action, detail) = "command", describe_command(data, interaction.guild)
        if action.startswith(("/usage", "/maintenance", "/admin")):
            return
    elif interaction.type == discord.InteractionType.component:
        if str(data.get("custom_id", "")).startswith(("usage:", "admin:")):
            return   # browsing this report isn't "usage"
        kind, action, detail = describe_component(data, interaction.message, interaction.guild)
    else:
        return
    category = interaction.extras.get("usage_category", category_for(action, detail))
    context = interaction.extras.get("usage_lookup")
    if context:
        action, detail = context
        category = "lookup"
    outcome = interaction.extras.get("usage_status", outcome)
    with connect() as conn:
        conn.execute("INSERT INTO usage_log (ts, guild_id, user_id, user_name, kind, action, detail, channel_id, interaction_id, outcome, category) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(interaction_id) DO UPDATE SET "
                     "outcome=CASE WHEN excluded.outcome='pending' THEN usage_log.outcome ELSE excluded.outcome END, "
                     "action=CASE WHEN excluded.outcome='pending' THEN usage_log.action ELSE excluded.action END, "
                     "detail=CASE WHEN excluded.outcome='pending' THEN usage_log.detail ELSE excluded.detail END, "
                     "category=CASE WHEN excluded.outcome='pending' THEN usage_log.category ELSE excluded.category END",
                     (int(time.time()), str(interaction.guild_id), str(interaction.user.id),
                      getattr(interaction.user, "display_name", None) or interaction.user.name,
                      kind, action[:100], (detail or "")[:200], str(interaction.channel_id or ""), str(interaction.id), outcome, category))


# --------------------------------------------------------------------------- #
#  Report queries
# --------------------------------------------------------------------------- #
def rows_since(guild_id: str, since: Optional[int], user_id: Optional[str] = None, raw: bool = False) -> list[dict]:
    q, args = "SELECT * FROM usage_log WHERE guild_id=?", [guild_id]
    if not raw:
        q += " AND outcome='success'"
    if since:
        q += " AND ts>=?"
        args.append(since)
    if user_id:
        q += " AND user_id=?"
        args.append(user_id)
    with connect() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY ts", args)]


def latest_names(rows: list[dict]) -> dict[str, str]:
    names = {}
    for r in rows:   # rows are oldest first, so the newest name wins
        names[r["user_id"]] = r["user_name"] or r["user_id"]
    return names


def _bar(n: int, top: int, width: int = 10) -> str:
    return "▰" * max(1, round(width * n / top)) if top else ""


def _local(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, ZoneInfo(BOT_TZ))


def is_lookup(r: dict) -> bool:
    return r.get("category", category_for(r["action"], r["detail"])) == "lookup"


def build_overview(rows: list[dict], period: str) -> discord.Embed:
    e = discord.Embed(title=f"📊 Bot usage — {PERIODS[period][0]}", colour=CLUB_COLOUR)
    if not rows:
        e.description = "Nothing logged in this period yet."
        return e
    names = latest_names(rows)
    people = Counter(r["user_id"] for r in rows)
    feats = Counter(r["action"] for r in rows)
    days = len({_local(r["ts"]).date() for r in rows})
    e.description = (f"**{len(rows)}** actions by **{len(people)}** people on **{days}** day(s)\n"
                     f"Commands {sum(r['kind'] == 'command' for r in rows)} · "
                     f"buttons {sum(r['kind'] == 'button' for r in rows)} · "
                     f"dropdowns {sum(r['kind'] == 'dropdown' for r in rows)}")
    lookups = sum(is_lookup(r) for r in rows)
    navigation = sum(r.get("category") == "navigation" for r in rows)
    e.description += f"\n**{lookups} actual lookups** · {navigation} menu navigation actions"
    by_user_days = {}
    for r in rows:
        by_user_days.setdefault(r["user_id"], set()).add(_local(r["ts"]).date())
    repeat = sum(len(days) >= 2 for days in by_user_days.values())
    e.add_field(name="🔁 Returning users", value=f"**{repeat}** used the bot on at least two different days", inline=False)
    top = people.most_common(1)[0][1]
    e.add_field(name="👥 Most active", inline=False, value="\n".join(
        f"`{n:>4}` {_bar(n, top)} **{names[u]}**" for u, n in people.most_common(5)))
    topf = feats.most_common(1)[0][1]
    e.add_field(name="🧭 Most used", inline=False, value="\n".join(
        f"`{n:>4}` {_bar(n, topf)} {a}" for a, n in feats.most_common(6)))
    hours = Counter(_local(r["ts"]).hour for r in rows)
    wd = Counter(_local(r["ts"]).weekday() for r in rows)
    h = hours.most_common(1)[0][0]
    e.add_field(name="⏰ Busiest time", inline=True, value=f"{h:02d}:00–{(h + 1) % 24:02d}:00")
    e.add_field(name="📅 Busiest day", inline=True, value=DAYS[wd.most_common(1)[0][0]])
    e.set_footer(text=f"Times in {BOT_TZ} • only bot buttons/commands are logged, never chat messages")
    return e


def build_people(rows: list[dict], period: str) -> discord.Embed:
    e = discord.Embed(title=f"👥 Who uses the bot — {PERIODS[period][0]}", colour=CLUB_COLOUR)
    if not rows:
        e.description = "Nothing logged in this period yet."
        return e
    names = latest_names(rows)
    by_user: dict[str, list[dict]] = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append(r)
    ranked = sorted(by_user.items(), key=lambda kv: -len(kv[1]))
    lines = []
    for i, (uid, rs) in enumerate(ranked[:25], 1):
        fav, fn = Counter(r["action"] for r in rs).most_common(1)[0]
        days = len({_local(r["ts"]).date() for r in rs})
        lines.append(f"**{i}. {names[uid]}** — {len(rs)} action{'s' * (len(rs) != 1)} on {days} day(s) · "
                     f"mostly *{fav}* ({fn}) · last <t:{rs[-1]['ts']}:R>")
    e.description = "\n".join(lines)[:4000]
    e.set_footer(text="Pick someone in the ▾ dropdown for their full breakdown")
    return e


def build_trends(guild_id: str, now: Optional[int] = None) -> discord.Embed:
    now = now or int(time.time())
    week = 7 * 86400
    rows = rows_since(guild_id, now - 2 * week)
    current = [r for r in rows if now - week <= r["ts"] <= now]
    previous = [r for r in rows if r["ts"] < now - week]
    e = discord.Embed(title="📈 Weekly trends", colour=CLUB_COLOUR,
                      description="Last 7 days compared with the preceding 7 days. Successful actions only.")
    for label, metric in (("Actions", lambda rs: len(rs)),
                          ("Lookups", lambda rs: sum(is_lookup(r) for r in rs)),
                          ("Active users", lambda rs: len({r["user_id"] for r in rs}))):
        a, b = metric(current), metric(previous)
        delta = f"{(a - b) / b:+.0%}" if b else "no previous baseline"
        e.add_field(name=label, value=f"**{a}** vs {b} · {delta}", inline=False)
    old_users = {r["user_id"] for r in previous}
    new_users = {r["user_id"] for r in current}
    e.add_field(name="Returning from previous week", value=str(len(new_users & old_users)))
    e.add_field(name="New this fortnight", value=str(len(new_users - old_users)))
    e.set_footer(text="Historical logs without a recorded outcome stay in CSV; they are excluded from successful-use totals.")
    return e


def build_features(rows: list[dict], period: str) -> discord.Embed:
    e = discord.Embed(title=f"🧭 Features — {PERIODS[period][0]}", colour=CLUB_COLOUR)
    if not rows:
        e.description = "Nothing logged in this period yet."
        return e
    feats: dict[str, list[dict]] = {}
    for r in rows:
        feats.setdefault(r["action"], []).append(r)
    ranked = sorted(feats.items(), key=lambda kv: -len(kv[1]))
    top = len(ranked[0][1])
    e.description = "\n".join(
        f"`{len(rs):>4}` {_bar(len(rs), top, 8)} **{a}** · {len({r['user_id'] for r in rs})} people"
        for a, rs in ranked[:30])[:4000]
    return e


def build_lookups(rows: list[dict], period: str) -> discord.Embed:
    e = discord.Embed(title=f"🔎 What people look up — {PERIODS[period][0]}", colour=CLUB_COLOUR)
    looked = [r for r in rows if is_lookup(r)]
    if not looked:
        e.description = "No lookups in this period yet."
        return e
    by_action: dict[str, Counter] = {}
    for r in looked:
        by_action.setdefault(r["action"], Counter())[r["detail"]] += 1
    for action, c in sorted(by_action.items(), key=lambda kv: -sum(kv[1].values()))[:12]:
        e.add_field(name=f"{action} ({sum(c.values())})", inline=False,
                    value="\n".join(f"`{n:>3}` {d}" for d, n in c.most_common(5))[:1024])
    return e


def build_person(rows: list[dict], period: str, name: str) -> discord.Embed:
    e = discord.Embed(title=f"👤 {name} — {PERIODS[period][0]}", colour=CLUB_COLOUR)
    if not rows:
        e.description = "They haven't used the bot in this period."
        return e
    days = len({_local(r["ts"]).date() for r in rows})
    e.description = (f"**{len(rows)}** actions on **{days}** day(s) · first <t:{rows[0]['ts']}:R> · "
                     f"last <t:{rows[-1]['ts']}:R>")
    feats = Counter(r["action"] for r in rows)
    e.add_field(name="🧭 Uses", inline=False,
                value="\n".join(f"`{n:>3}` {a}" for a, n in feats.most_common(8)))
    looks = Counter(f"{r['action']} → {r['detail']}" for r in rows if is_lookup(r))
    if looks:
        e.add_field(name="🔎 Looked up", inline=False,
                    value="\n".join(f"`{n:>3}` {d}" for d, n in looks.most_common(8))[:1024])
    recent = [f"<t:{r['ts']}:f> {r['action']}" + (f" → {r['detail']}" if r["detail"] else "")
              for r in rows[-15:][::-1]]
    e.add_field(name="🕒 Last actions", inline=False, value="\n".join(recent)[:1024])
    return e


def csv_file(rows: list[dict], period: str) -> discord.File:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "user", "user_id", "type", "action", "detail", "category", "outcome"])
    for r in rows:
        w.writerow([_local(r["ts"]).strftime("%Y-%m-%d %H:%M:%S"), r["user_name"], r["user_id"],
                    r["kind"], r["action"], r["detail"], r.get("category", "action"), r.get("outcome", "unknown")])
    return discord.File(io.BytesIO(buf.getvalue().encode("utf-8-sig")), f"madboys-bot-usage-{period}.csv")


# --------------------------------------------------------------------------- #
#  Access + the report menu
# --------------------------------------------------------------------------- #
def can_view(user: discord.abc.User, guild: Optional[discord.Guild]) -> bool:
    return str(user.id) in USAGE_VIEWERS or user.name.lower() in USAGE_VIEWERS


class UsageView(discord.ui.View):
    """custom_ids start with 'usage:' so browsing the report isn't logged as usage."""

    def __init__(self, guild: discord.Guild, user: discord.abc.User):
        super().__init__(timeout=14 * 60)
        self.guild, self.user, self.gid = guild, user, str(guild.id)
        self.page, self.period = "overview", "7d"
        self.person: Optional[str] = None
        self.embed = discord.Embed()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id and can_view(interaction.user, interaction.guild)

    def since(self) -> Optional[int]:
        secs = PERIODS[self.period][1]
        return int(time.time()) - secs if secs else None

    def build(self):
        if self.page == "trends":
            self.embed = build_trends(self.gid)
        elif self.page == "person" and self.person:
            rows = rows_since(self.gid, self.since(), self.person)
            m = self.guild.get_member(int(self.person))
            name = m.display_name if m else (latest_names(rows).get(self.person) or self.person)
            self.embed = build_person(rows, self.period, name)
        else:
            rows = rows_since(self.gid, self.since())
            self.embed = {"overview": build_overview, "people": build_people, "features": build_features,
                          "lookups": build_lookups}[self.page](rows, self.period)
        if self.page == "overview":
            attempts = rows_since(self.gid, self.since(), raw=True)
            outcomes = Counter(r["outcome"] for r in attempts)
            self.embed.add_field(name="Excluded from successful-use totals", inline=False,
                                 value=f"Failed: {outcomes['failed']} · unfinished: {outcomes['pending']} · historical unknown: {outcomes['unknown']}")
        self.render()

    def render(self):
        self.clear_items()
        for key, label, emoji in (("overview", "Overview", "📊"), ("people", "People", "👥"),
                                  ("features", "Features", "🧭"), ("lookups", "Lookups", "🔎"), ("trends", "Trends", "📈")):
            b = discord.ui.Button(label=label, emoji=emoji, row=0, custom_id=f"usage:page:{key}",
                                  style=discord.ButtonStyle.primary if self.page == key
                                  else discord.ButtonStyle.secondary)
            b.callback = self._page(key)
            self.add_item(b)
        ex = discord.ui.Button(label="CSV", emoji="📄", row=3, custom_id="usage:csv",
                               style=discord.ButtonStyle.secondary)
        ex.callback = self._csv
        self.add_item(ex)
        ps = discord.ui.Select(row=1, custom_id="usage:period", options=[
            discord.SelectOption(label=label, value=k, default=k == self.period) for k, (label, _) in PERIODS.items()])
        ps.callback = self._period
        self.add_item(ps)
        us = discord.ui.UserSelect(row=2, custom_id="usage:person", placeholder="👤 Look at one person…")
        us.callback = self._person
        self.add_item(us)

    async def _show(self, interaction: discord.Interaction):
        self.build()
        await interaction.response.edit_message(embed=self.embed, view=self)

    def _page(self, key: str):
        async def cb(interaction: discord.Interaction):
            self.page = key
            await self._show(interaction)
        return cb

    async def _period(self, interaction: discord.Interaction):
        self.period = interaction.data["values"][0]
        await self._show(interaction)

    async def _person(self, interaction: discord.Interaction):
        self.person, self.page = interaction.data["values"][0], "person"
        await self._show(interaction)

    async def _csv(self, interaction: discord.Interaction):
        rows = rows_since(self.gid, self.since(), self.person if self.page == "person" else None, raw=True)
        await interaction.response.send_message(f"📄 {len(rows)} rows ({PERIODS[self.period][0].lower()})",
                                                file=csv_file(rows, self.period), ephemeral=True)


class UsageCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command):
        try:
            log_interaction(interaction, interaction.extras.get("usage_status", "success"))
        except Exception:
            log.exception("Couldn't record command outcome")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        try:
            log_interaction(interaction)
        except Exception:   # logging must never break the bot
            log.exception("Couldn't log usage")

    @app_commands.command(name="usage", description="Private: who uses the bot and for what (bot admins only)")
    async def usage(self, interaction: discord.Interaction):
        if not can_view(interaction.user, interaction.guild):
            await interaction.response.send_message("🔒 This one's private to the bot's admins.", ephemeral=True)
            return
        view = UsageView(interaction.guild, interaction.user)
        view.build()
        await interaction.response.send_message(embed=view.embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    init_usage()
    await bot.add_cog(UsageCog(bot))
