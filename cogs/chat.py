"""
AI chat: @mention the bot and it replies using Google Gemini.

Gemini can look up real stats from the bot's own match database (the same data
/playerstats, /leaderboard, /form and /h2h use) via function calling, e.g.
  "@bot what are player1's passing stats?"
  "@bot who's scored the most this week?"
  "@bot how did we do against FC Whatever?"
  "@bot how am I doing this month?"   (uses your /link me name)

Env vars (set in Railway):
  GEMINI_API_KEY  - required. Your key from aistudio.google.com
  GEMINI_MODEL    - optional, defaults to gemini-3.8-flash
  GEMINI_FALLBACK_MODEL - optional, defaults to gemini-3.5-flash-lite (empty disables)
"""

import asyncio
import logging
import math
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands

from clubs import selected_club
from cogs.chat_features import FEATURE_COMMANDS, FeatureReply, command_catalog, show_feature
from cogs.link import get_all_links, get_link
from db import connect
from player_comparison import PlayerComparison, render_comparison

log = logging.getLogger("madboys-bot.chat")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "gemini-3.8-flash").strip()
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite").strip()
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

HISTORY_PAIRS = 6          # remember the last 6 question/answer pairs per channel
MAX_TOOL_ROUNDS = 5        # how many lookups Gemini may chain for one question
DISCORD_LIMIT = 2000


# ---------------------------------------------------------------- stat lookups

def _since(days):
    try:
        days = float(days or 0)
    except (TypeError, ValueError):
        days = 0
    return int(time.time() - days * 86400) if days > 0 else 0


def _pct(made, att):
    return round(100 * made / att, 1) if att else None


def _date(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def _resolve_player(club_id, name):
    """Find the stored EA name that best matches what was typed. Returns (name, candidates)."""
    name = (name or "").strip().lstrip("@")
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT name FROM match_players WHERE club_id=? AND name LIKE ? COLLATE NOCASE",
            (club_id, f"%{name}%"),
        ).fetchall()
    names = [r["name"] for r in rows]
    exact = [n for n in names if n.lower() == name.lower()]
    if exact:
        return exact[0], []
    if len(names) == 1:
        return names[0], []
    return None, names[:15]


def tool_list_players(club_id, days=0):
    with connect() as conn:
        rows = conn.execute(
            """SELECT mp.name, COUNT(*) AS games FROM match_players mp
               JOIN matches m ON m.club_id=mp.club_id AND m.match_id=mp.match_id
               WHERE mp.club_id=? AND m.ts>=? GROUP BY mp.name COLLATE NOCASE ORDER BY games DESC""",
            (club_id, _since(days)),
        ).fetchall()
    return {"players": [dict(r) for r in rows]}


def tool_player_stats(club_id, player, days=0, *, since=None):
    name, candidates = _resolve_player(club_id, player)
    if not name:
        return {"error": f"No single player matches '{player}'.", "did_you_mean": candidates}
    since = _since(days) if since is None else since
    with connect() as conn:
        t = conn.execute(
            """SELECT COUNT(*) AS games, SUM(mp.goals) AS goals, SUM(mp.assists) AS assists,
                      ROUND(AVG(mp.rating), 2) AS avg_rating, SUM(mp.motm) AS motm,
                      SUM(mp.shots) AS shots, SUM(mp.passes_made) AS passes_made,
                      SUM(mp.pass_attempts) AS pass_attempts, SUM(mp.tackles_made) AS tackles_made,
                      SUM(mp.tackle_attempts) AS tackle_attempts, SUM(mp.saves) AS saves,
                      SUM(mp.red_cards) AS red_cards, SUM(mp.clean_sheet) AS clean_sheets,
                      SUM(CASE WHEN m.result='W' THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN m.result='D' THEN 1 ELSE 0 END) AS draws,
                      SUM(CASE WHEN m.result='L' THEN 1 ELSE 0 END) AS losses
               FROM match_players mp JOIN matches m ON m.club_id=mp.club_id AND m.match_id=mp.match_id
               WHERE mp.club_id=? AND mp.name=? COLLATE NOCASE AND m.ts>=?""",
            (club_id, name, since),
        ).fetchone()
        positions = conn.execute(
            """SELECT mp.pos, COUNT(*) AS games FROM match_players mp
               JOIN matches m ON m.club_id=mp.club_id AND m.match_id=mp.match_id
               WHERE mp.club_id=? AND mp.name=? COLLATE NOCASE AND m.ts>=?
               GROUP BY mp.pos ORDER BY games DESC""",
            (club_id, name, since),
        ).fetchall()
    stats = dict(t)
    if not stats["games"]:
        return {"player": name, "games": 0, "note": "No games in that time range."}
    g = stats["games"]
    stats.update(
        player=name,
        period_days=days or "all time",
        pass_accuracy_pct=_pct(stats["passes_made"] or 0, stats["pass_attempts"] or 0),
        tackle_success_pct=_pct(stats["tackles_made"] or 0, stats["tackle_attempts"] or 0),
        goals_per_game=round((stats["goals"] or 0) / g, 2),
        assists_per_game=round((stats["assists"] or 0) / g, 2),
        passes_per_game=round((stats["passes_made"] or 0) / g, 1),
        positions_played={(r["pos"] or "unknown"): r["games"] for r in positions},
    )
    return stats


def tool_compare_players(club_id, left, right, days=0):
    days = float(days or 0)
    if not math.isfinite(days) or days < 0 or days > 36500:
        return {"error": "Use a period from 0 to 36500 days (0 for all recorded matches)."}
    since = _since(days)
    players = [tool_player_stats(club_id, name, days, since=since) for name in (left, right)]
    if any(not player.get("games") for player in players):
        return {"error": "Both players need recorded matches in the selected period.", "lookups": players}
    if players[0]["player"].casefold() == players[1]["player"].casefold():
        return {"error": "Choose two different players."}
    return {"left": players[0], "right": players[1], "days": days}


def tool_leaderboard(club_id, stat="goals", days=0, limit=10):
    allowed = {
        "goals": "SUM(mp.goals)", "assists": "SUM(mp.assists)", "rating": "AVG(mp.rating)",
        "motm": "SUM(mp.motm)", "games": "COUNT(*)", "shots": "SUM(mp.shots)",
        "passes": "SUM(mp.passes_made)", "tackles": "SUM(mp.tackles_made)", "saves": "SUM(mp.saves)",
        "pass_accuracy": "1.0*SUM(mp.passes_made)/NULLIF(SUM(mp.pass_attempts),0)",
        "goal_contributions": "SUM(mp.goals)+SUM(mp.assists)",
    }
    expr = allowed.get(stat, allowed["goals"])
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT mp.name AS player, COUNT(*) AS games, ROUND({expr}, 3) AS value
                FROM match_players mp JOIN matches m ON m.club_id=mp.club_id AND m.match_id=mp.match_id
                WHERE mp.club_id=? AND m.ts>=? GROUP BY mp.name COLLATE NOCASE
                ORDER BY value DESC LIMIT ?""",
            (club_id, _since(days), max(1, min(int(limit or 10), 25))),
        ).fetchall()
    return {"stat": stat if stat in allowed else "goals", "period_days": days or "all time",
            "ranking": [dict(r) for r in rows]}


def tool_recent_results(club_id, limit=10, days=0):
    with connect() as conn:
        rows = conn.execute(
            """SELECT match_id, ts, match_type, our_goals, opp_goals, opp_name, result FROM matches
               WHERE club_id=? AND ts>=? ORDER BY ts DESC LIMIT ?""",
            (club_id, _since(days), max(1, min(int(limit or 10), 30))),
        ).fetchall()
    results = [dict(r, date=_date(r["ts"])) for r in rows]
    for r in results:
        r.pop("ts", None)
    form = "".join(r["result"] for r in results)
    return {"matches": results, "form_newest_first": form,
            "wins": form.count("W"), "draws": form.count("D"), "losses": form.count("L")}


def tool_match_details(club_id, match_id=None):
    with connect() as conn:
        if match_id:
            m = conn.execute("SELECT * FROM matches WHERE club_id=? AND match_id=?", (club_id, str(match_id))).fetchone()
        else:
            m = conn.execute("SELECT * FROM matches WHERE club_id=? ORDER BY ts DESC LIMIT 1", (club_id,)).fetchone()
        if not m:
            return {"error": "Match not found."}
        players = conn.execute(
            """SELECT name, pos, goals, assists, rating, shots, passes_made, pass_attempts,
                      tackles_made, tackle_attempts, saves, red_cards, motm
               FROM match_players WHERE club_id=? AND match_id=? ORDER BY rating DESC""",
            (club_id, m["match_id"]),
        ).fetchall()
    return {"match_id": m["match_id"], "date": _date(m["ts"]), "type": m["match_type"],
            "score": f"{m['our_goals']}-{m['opp_goals']}", "opponent": m["opp_name"], "result": m["result"],
            "players": [dict(p) for p in players]}


def tool_head_to_head(club_id, opponent):
    with connect() as conn:
        rows = conn.execute(
            """SELECT ts, our_goals, opp_goals, opp_name, result FROM matches
               WHERE club_id=? AND opp_name LIKE ? COLLATE NOCASE ORDER BY ts DESC""",
            (club_id, f"%{(opponent or '').strip()}%"),
        ).fetchall()
    if not rows:
        return {"error": f"No matches found against '{opponent}'."}
    names = sorted({r["opp_name"] for r in rows})
    res = [r["result"] for r in rows]
    return {"opponents_matched": names, "played": len(rows), "wins": res.count("W"),
            "draws": res.count("D"), "losses": res.count("L"),
            "goals_for": sum(r["our_goals"] for r in rows), "goals_against": sum(r["opp_goals"] for r in rows),
            "recent": [{"date": _date(r["ts"]), "score": f"{r['our_goals']}-{r['opp_goals']}",
                        "opponent": r["opp_name"], "result": r["result"]} for r in rows[:10]]}


TOOLS = {
    "list_players": tool_list_players,
    "get_player_stats": tool_player_stats,
    "compare_players": tool_compare_players,
    "get_leaderboard": tool_leaderboard,
    "get_recent_results": tool_recent_results,
    "get_match_details": tool_match_details,
    "get_head_to_head": tool_head_to_head,
}

_days = {"type": "NUMBER", "description": "Only count games in the last N days (1 = today, 7 = week, 30 = month). 0 or omitted = all time."}
TOOL_DECLARATIONS = [{"functionDeclarations": [
    {"name": "list_players", "description": "List the club's players (EA names) and games played. Use to find the right spelling of a name.",
     "parameters": {"type": "OBJECT", "properties": {"days": _days}}},
    {"name": "get_player_stats",
     "description": "One player's totals: games, W/D/L, goals, assists, avg rating, MOTM, shots, passes and pass accuracy, tackles and tackle success, saves, clean sheets, red cards, positions played.",
     "parameters": {"type": "OBJECT", "properties": {
         "player": {"type": "STRING", "description": "EA name or part of it"}, "days": _days},
         "required": ["player"]}},
    {"name": "compare_players",
     "description": "Display a side-by-side player comparison graphic from saved games. Use this for general comparisons of two players or when requested. A successful call sends the graphic as the entire response.",
     "parameters": {"type": "OBJECT", "properties": {
         "left": {"type": "STRING"}, "right": {"type": "STRING"}, "days": _days},
         "required": ["left", "right"]}},
    {"name": "show_bot_feature",
     "description": "Open an existing bot report in Discord. Use player_twin when asked which real-life footballer an EA player plays most like, including across clubs. Use this only when asked to see the report.",
     "parameters": {"type": "OBJECT", "properties": {
         "feature": {"type": "STRING", "enum": list(FEATURE_COMMANDS)},
         "player": {"type": "STRING", "description": "EA name or partial name. Use me for the asker."},
         "month": {"type": "STRING", "description": "Optional awards month, YYYY-MM."},
         "days": {"type": "INTEGER", "description": "Optional recap window, 1 to 90 days."}},
         "required": ["feature"]}},
    {"name": "get_leaderboard", "description": "Rank the squad by a stat.",
     "parameters": {"type": "OBJECT", "properties": {
         "stat": {"type": "STRING", "enum": ["goals", "assists", "goal_contributions", "rating", "motm", "games",
                                             "shots", "passes", "pass_accuracy", "tackles", "saves"]},
         "days": _days, "limit": {"type": "INTEGER"}}}},
    {"name": "get_recent_results", "description": "The club's latest results, scores, opponents and form.",
     "parameters": {"type": "OBJECT", "properties": {"limit": {"type": "INTEGER"}, "days": _days}}},
    {"name": "get_match_details", "description": "Full player ratings and stats for one match. Omit match_id for the most recent game.",
     "parameters": {"type": "OBJECT", "properties": {"match_id": {"type": "STRING"}}}},
    {"name": "get_head_to_head", "description": "The club's record against a specific opponent club.",
     "parameters": {"type": "OBJECT", "properties": {"opponent": {"type": "STRING"}}, "required": ["opponent"]}},
]}]


# ---------------------------------------------------------------- the cog

def _chunks(text: str, size: int = DISCORD_LIMIT):
    """Split long replies to fit Discord's 2000-character limit, preferring line breaks."""
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    if text:
        yield text


def _system_prompt(club_name, asker, asker_ea, catalogue):
    me = f"The person asking is {asker}" + (f", whose EA name is '{asker_ea}'." if asker_ea else
                                             " (they haven't linked an EA name with /link me).")
    return (
        f"You are the Discord bot for {club_name}, an EA FC Pro Clubs team. "
        "Be friendly, a bit cheeky, and keep replies short and Discord-friendly (bullets are fine, no tables). "
        "You can chat about football, EA FC tactics, formations and player builds. "
        "For ANY question about our players, results or stats, call the lookup tools and answer only from what "
        "they return - never invent or guess numbers. If a name doesn't match, use the suggestions or list_players. "
        "For a general comparison of two players, call compare_players to show one graphic instead of text. "
        "For a question about a specific stat, use the lookup that provides the requested detail. "
        "Use the registered slash command catalogue below as the source of truth for installed features. "
        "Player Twin is /playertwin, which compares a player's EA play style to a real footballer. "
        "Use show_bot_feature for reports it supports. For other installed features, give the exact slash command. "
        "Private screens, admin tools, settings and actions must be opened or used with their original commands. "
        "Never claim to have run a slash command or changed anything. "
        "Stats only cover matches the bot has recorded. "
        f"{me} If they say 'me' or 'I', use their EA name. "
        f"Today is {datetime.now(timezone.utc):%Y-%m-%d}.\n\n"
        "Registered slash commands for this context:\n"
        + ("\n".join(f"{name} — {description}" for name, description in sorted(catalogue.items())) or "None available.")
    )


class ChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.history = defaultdict(lambda: deque(maxlen=HISTORY_PAIRS * 2))
        self.session: aiohttp.ClientSession | None = None
        self.api_status = "Waiting for first request" if GEMINI_API_KEY else "GEMINI_API_KEY is missing"
        if not GEMINI_API_KEY:
            log.warning("GEMINI_API_KEY not set - AI chat is disabled")

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _call_gemini(self, body: dict, model: str):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        headers = {"x-goog-api-key": GEMINI_API_KEY}
        status = 503
        try:
            async with self.session.post(GEMINI_URL.format(model=model), json=body, headers=headers) as resp:
                status = resp.status
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            if status == 200:
                status = 503
            data = None
            log.warning("Gemini response failed for %s (%s)", model, type(exc).__name__)
        if status != 200:
            error = data.get("error") if isinstance(data, dict) else None
            codes = []
            if isinstance(error, dict):
                reasons = [
                    item.get("reason") for item in (error.get("details") or [])
                    if isinstance(item, dict)
                ]
                codes = [code for code in [error.get("status"), *reasons]
                         if isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,60}", code)]
            detail = f" ({', '.join(codes)})" if codes else ""
            self.api_status = f"{model} returned HTTP {status}{detail}"
            log.warning("Gemini %s", self.api_status)
            return status, None
        self.api_status = f"Responding via {model}"
        return status, data

    async def _answer(self, message: discord.Message, prompt: str) -> str | PlayerComparison | FeatureReply:
        guild_id = str(message.guild.id) if message.guild else ""
        club = selected_club(guild_id, message.author.id) if guild_id else {"club_id": None, "name": "the club"}
        asker_ea = get_link(guild_id, str(message.author.id)) if guild_id else None

        catalogue = command_catalog(self.bot, message.guild)
        twin_command = FEATURE_COMMANDS["player_twin"]
        availability = (
            r"(?:i (?:would like|want) to know if )?"
            r"(?:(?:do|does) (?:you|the bot) have |you have |is )"
            r"(?:the )?/?player[ -]?twin(?: installed| available)?[?.! ]*"
        )
        if re.fullmatch(availability, prompt, re.I):
            if twin_command in catalogue:
                return "Yes. Use `/playertwin` or ask me who an EA player plays like."
            return "`/playertwin` isn't registered in this context."

        twin_request = re.search(r"\bwho ([^?!.]{1,80}?) plays? (?:the )?most like[?.! ]*$", prompt, re.I)
        if twin_request and guild_id and twin_command in catalogue:
            player = twin_request.group(1).strip()
            linked = re.search(r"\(EA name: ([^)]+)\)$", player)
            screen = await show_feature(
                self.bot, guild_id, club, "player_twin",
                player=linked.group(1) if linked else player, asker_ea=asker_ea,
            )
            reply = FeatureReply(twin_command, screen)
            self.history[message.channel.id].extend((
                {"role": "user", "parts": [{"text": prompt}]},
                {"role": "model", "parts": [{"text": reply.summary()}]},
            ))
            return reply

        history = self.history[message.channel.id]
        user_turn = {"role": "user", "parts": [{"text": prompt}]}
        body = {
            "systemInstruction": {"parts": [{"text": _system_prompt(club["name"], message.author.display_name, asker_ea, catalogue)}]},
            "contents": [*history, user_turn],
            "tools": TOOL_DECLARATIONS,
        }

        try:
            async with asyncio.timeout(25):
                status, reply = await self._generate_reply(body, club, GEMINI_MODEL, guild_id, asker_ea, catalogue)
                if (status == 404 or status == 429 or 500 <= status < 600) and (
                    GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL != GEMINI_MODEL
                ):
                    status, reply = await self._generate_reply(body, club, GEMINI_FALLBACK_MODEL, guild_id, asker_ea, catalogue)
        except TimeoutError:
            self.api_status = "Chat request timed out"
            status, reply = 503, None
        if reply is not None:
            if status == 200:
                history_text = reply.summary() if isinstance(reply, (PlayerComparison, FeatureReply)) else reply
                history.extend((user_turn, {"role": "model", "parts": [{"text": history_text}]}))
            return reply
        if status == 429:
            return "Gemini is rate-limited. You can still see your saved stats with `/stats`."
        if status in (400, 401, 403, 404):
            return "Gemini rejected the request. An admin can check Bot Status. Saved stats are available in `/stats`."
        return "Gemini is temporarily unavailable. Saved stats are still available in `/stats`."

    async def _generate_reply(self, body, club, model, guild_id, asker_ea, catalogue):
        club_id = club["club_id"]
        # Start the fallback from plain chat history so it never receives another model's signatures.
        contents = list(body["contents"])
        for _ in range(MAX_TOOL_ROUNDS + 1):
            status, data = await self._call_gemini({**body, "contents": contents}, model)
            if data is None:
                return status if status != 200 else 503, None
            try:
                content = data["candidates"][0]["content"]
                parts = content.get("parts", [])
            except (KeyError, IndexError, TypeError, AttributeError):
                self.api_status = f"{model} returned no answer"
                log.warning("Gemini %s", self.api_status)
                return 422, "🤐 I can't answer that one."

            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                reply = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                reply = reply or "🤔 Not sure what to say to that."
                return 200, reply

            # Run each lookup Gemini asked for, then hand the results back.
            contents = [*contents, content]  # Preserve the model turn and its thought signatures.
            responses = []
            for call in calls:
                name, args = call.get("name"), call.get("args") or {}
                fn = TOOLS.get(name)
                try:
                    if name == "show_bot_feature":
                        feature = args.get("feature")
                        command = FEATURE_COMMANDS.get(feature)
                        if guild_id and command and command in catalogue:
                            params = {key: args[key] for key in ("player", "month", "days") if key in args}
                            screen = await show_feature(self.bot, guild_id, club, feature, asker_ea=asker_ea, **params)
                            return 200, FeatureReply(command, screen)
                        result = {"error": "That report is unavailable in this context. Use the registered slash commands."}
                    elif fn and club_id:
                        result = await asyncio.to_thread(fn, club_id, **args)
                    else:
                        result = {"error": "Lookup not available here."}
                except Exception:
                    log.exception("Chat tool %s failed", name)
                    result = {"error": "The stats lookup failed."}
                if name == "compare_players" and "left" in result:
                    return 200, PlayerComparison(club["name"], result["days"], result["left"], result["right"])
                response = {"name": name, "response": result}
                if call.get("id"):
                    response["id"] = call["id"]
                responses.append({"functionResponse": response})
            contents.append({"role": "user", "parts": responses})

        return 422, "🌀 That needed too many lookups - try asking something more specific."

    async def _send_comparison(self, message: discord.Message, card: PlayerComparison):
        guild = message.guild
        permissions = message.channel.permissions_for(guild.me) if guild and guild.me else None
        options = {"mention_author": False, "allowed_mentions": discord.AllowedMentions.none()}
        from discord.utils import escape_markdown

        if permissions is None or permissions.attach_files:
            try:
                png = await asyncio.to_thread(render_comparison, card)
                with png:
                    file = discord.File(png, filename="player-comparison.png", description=card.summary()[:1024])
                    try:
                        await message.reply(f"**{escape_markdown(card.title)}**", file=file, **options)
                    finally:
                        file.close()
                return
            except discord.Forbidden:
                log.info("Comparison attachment unavailable in channel %s", message.channel.id)
            except (OSError, ValueError):
                log.exception("Could not render a player comparison")

        if permissions is None or permissions.embed_links:
            embed = discord.Embed(title=card.title, description=f"{card.club} · {card.period}", colour=0x27AD94)
            for player, column in ((card.left, 1), (card.right, 2)):
                embed.add_field(name=player["player"], value="\n".join(
                    f"**{row[0]}** {row[column]}" for row in card.rows()
                ))
            try:
                await message.reply(embed=embed, **options)
                return
            except discord.Forbidden:
                log.info("Comparison embed unavailable in channel %s", message.channel.id)
        await message.reply(escape_markdown(card.summary()), **options)

    async def _send_feature(self, message, feature: FeatureReply):
        screen = feature.screen
        options = {"mention_author": False, "allowed_mentions": discord.AllowedMentions.none()}
        if isinstance(screen, str):
            await message.reply(screen, **options)
            return
        guild = message.guild
        permissions = message.channel.permissions_for(guild.me) if guild and guild.me else None
        if permissions is None or permissions.embed_links:
            try:
                if isinstance(screen, tuple):
                    embed, png = screen
                    if permissions is None or permissions.attach_files:
                        from cogs.stats import card_file
                        file = card_file(png)
                        try:
                            await message.reply(embed=embed, file=file, **options)
                        finally:
                            file.close()
                        return
                    screen = embed.copy()
                    screen.remove_image()
                await message.reply(embed=screen, **options)
                return
            except discord.Forbidden:
                log.info("Feature preview unavailable in channel %s", message.channel.id)
        await message.reply(f"Open `{feature.command}` to see this report.", **options)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        # "@bot What..." looks like a text command to discord.py (prefix = mention). The bot only
        # uses slash commands, so ignore those; still log anything else.
        if isinstance(error, commands.CommandNotFound):
            return
        log.error("Command error in %s: %s", ctx.command, error, exc_info=error)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not GEMINI_API_KEY or self.bot.user not in message.mentions:
            return

        prompt = re.sub(rf"<@!?{self.bot.user.id}>", "", message.content).strip()
        # Turn other @mentions into names (plus their linked EA name) so "@Dave's stats" works.
        other_mentions = [user for user in message.mentions if user.id != self.bot.user.id]
        links = (
            await asyncio.to_thread(get_all_links, str(message.guild.id))
            if message.guild and other_mentions else {}
        )
        for user in other_mentions:
            label = user.display_name + (f" (EA name: {links[str(user.id)]})" if str(user.id) in links else "")
            prompt = re.sub(rf"<@!?{user.id}>", lambda _: label, prompt)

        if not prompt:
            await message.reply("👋 Ask me something - e.g. `@me what are my passing stats this month?`")
            return

        async with message.channel.typing():
            try:
                reply = await self._answer(message, prompt)
            except Exception:
                log.exception("Chat request failed")
                reply = "⚠️ Something went wrong talking to the AI."

        if isinstance(reply, PlayerComparison):
            await self._send_comparison(message, reply)
            return

        if isinstance(reply, FeatureReply):
            await self._send_feature(message, reply)
            return

        first = True
        for part in _chunks(reply):
            if first:
                await message.reply(part, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                first = False
            else:
                await message.channel.send(part, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatCog(bot))
