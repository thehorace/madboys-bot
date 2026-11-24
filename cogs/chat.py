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
  GEMINI_MODEL    - optional, defaults to gemini-2.5-flash
"""

import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands

from clubs import selected_club
from cogs.link import get_all_links, get_link
from db import connect

log = logging.getLogger("madboys-bot.chat")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
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


def tool_player_stats(club_id, player, days=0):
    name, candidates = _resolve_player(club_id, player)
    if not name:
        return {"error": f"No single player matches '{player}'.", "did_you_mean": candidates}
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
            (club_id, name, _since(days)),
        ).fetchone()
        positions = conn.execute(
            """SELECT mp.pos, COUNT(*) AS games FROM match_players mp
               JOIN matches m ON m.club_id=mp.club_id AND m.match_id=mp.match_id
               WHERE mp.club_id=? AND mp.name=? COLLATE NOCASE AND m.ts>=?
               GROUP BY mp.pos ORDER BY games DESC""",
            (club_id, name, _since(days)),
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
    "get_leaderboard": tool_leaderboard,
    "get_recent_results": tool_recent_results,
    "get_match_details": tool_match_details,
    "get_head_to_head": tool_head_to_head,
}

_days = {"type": "number", "description": "Only count games in the last N days (1 = today, 7 = week, 30 = month). 0 or omitted = all time."}
TOOL_DECLARATIONS = [{"function_declarations": [
    {"name": "list_players", "description": "List the club's players (EA names) and games played. Use to find the right spelling of a name.",
     "parameters": {"type": "object", "properties": {"days": _days}}},
    {"name": "get_player_stats",
     "description": "One player's totals: games, W/D/L, goals, assists, avg rating, MOTM, shots, passes and pass accuracy, tackles and tackle success, saves, clean sheets, red cards, positions played.",
     "parameters": {"type": "object", "properties": {
         "player": {"type": "string", "description": "EA name or part of it"}, "days": _days},
         "required": ["player"]}},
    {"name": "get_leaderboard", "description": "Rank the squad by a stat.",
     "parameters": {"type": "object", "properties": {
         "stat": {"type": "string", "enum": ["goals", "assists", "goal_contributions", "rating", "motm", "games",
                                             "shots", "passes", "pass_accuracy", "tackles", "saves"]},
         "days": _days, "limit": {"type": "integer"}}}},
    {"name": "get_recent_results", "description": "The club's latest results, scores, opponents and form.",
     "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}, "days": _days}}},
    {"name": "get_match_details", "description": "Full player ratings and stats for one match. Omit match_id for the most recent game.",
     "parameters": {"type": "object", "properties": {"match_id": {"type": "string"}}}},
    {"name": "get_head_to_head", "description": "The club's record against a specific opponent club.",
     "parameters": {"type": "object", "properties": {"opponent": {"type": "string"}}, "required": ["opponent"]}},
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


def _system_prompt(club_name, asker, asker_ea):
    me = f"The person asking is {asker}" + (f", whose EA name is '{asker_ea}'." if asker_ea else
                                             " (they haven't linked an EA name with /link me).")
    return (
        f"You are the Discord bot for {club_name}, an EA FC Pro Clubs team. "
        "Be friendly, a bit cheeky, and keep replies short and Discord-friendly (bullets are fine, no tables). "
        "You can chat about football, EA FC tactics, formations and player builds. "
        "For ANY question about our players, results or stats, call the lookup tools and answer only from what "
        "they return - never invent or guess numbers. If a name doesn't match, use the suggestions or list_players. "
        "Stats only cover matches the bot has recorded. "
        f"{me} If they say 'me' or 'I', use their EA name. "
        f"Today is {datetime.now(timezone.utc):%Y-%m-%d}."
    )


class ChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.history = defaultdict(lambda: deque(maxlen=HISTORY_PAIRS * 2))
        self.session: aiohttp.ClientSession | None = None
        if not GEMINI_API_KEY:
            log.warning("GEMINI_API_KEY not set - AI chat is disabled")

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _call_gemini(self, body: dict):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
        async with self.session.post(GEMINI_URL.format(model=GEMINI_MODEL), json=body, headers=headers) as resp:
            if resp.status != 200:
                log.error("Gemini error %s: %s", resp.status, (await resp.text())[:500])
                return resp.status, None
            return 200, await resp.json()

    async def _answer(self, message: discord.Message, prompt: str) -> str:
        guild_id = str(message.guild.id) if message.guild else ""
        club = selected_club(guild_id, message.author.id) if guild_id else {"club_id": None, "name": "the club"}
        club_id = club["club_id"]
        asker_ea = get_link(guild_id, str(message.author.id)) if guild_id else None

        history = self.history[message.channel.id]
        contents = list(history) + [{"role": "user", "parts": [{"text": prompt}]}]
        body = {
            "system_instruction": {"parts": [{"text": _system_prompt(club["name"], message.author.display_name, asker_ea)}]},
            "contents": contents,
            "tools": TOOL_DECLARATIONS,
        }

        for _ in range(MAX_TOOL_ROUNDS + 1):
            status, data = await self._call_gemini(body)
            if status == 429:
                return "😮‍💨 I've hit my free AI limit for now - try again a bit later."
            if data is None:
                return "⚠️ My AI brain isn't responding right now. Try again in a minute."
            try:
                content = data["candidates"][0]["content"]
                parts = content.get("parts", [])
            except (KeyError, IndexError):
                log.warning("Gemini returned no content: %s", str(data)[:500])
                return "🤐 I can't answer that one."

            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                reply = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                reply = reply or "🤔 Not sure what to say to that."
                history.append({"role": "user", "parts": [{"text": prompt}]})
                history.append({"role": "model", "parts": [{"text": reply}]})
                return reply

            # Run each lookup Gemini asked for, then hand the results back.
            contents.append(content)   # send the model turn back unchanged (keeps thought signatures)
            responses = []
            for call in calls:
                name, args = call.get("name"), call.get("args") or {}
                fn = TOOLS.get(name)
                try:
                    result = fn(club_id, **args) if fn and club_id else {"error": "Lookup not available here."}
                except Exception as e:
                    log.exception("Chat tool %s failed", name)
                    result = {"error": f"Lookup failed: {e}"}
                log.info("Chat tool %s(%s) -> %d chars", name, args, len(json.dumps(result, default=str)))
                responses.append({"functionResponse": {"name": name, "response": json.loads(json.dumps(result, default=str))}})
            contents.append({"role": "user", "parts": responses})

        return "🌀 That needed too many lookups - try asking something more specific."

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not GEMINI_API_KEY or self.bot.user not in message.mentions:
            return

        prompt = re.sub(rf"<@!?{self.bot.user.id}>", "", message.content).strip()
        # Turn other @mentions into names (plus their linked EA name) so "@Dave's stats" works.
        links = get_all_links(str(message.guild.id)) if message.guild else {}
        for user in message.mentions:
            if user.id == self.bot.user.id:
                continue
            label = user.display_name + (f" (EA name: {links[str(user.id)]})" if str(user.id) in links else "")
            prompt = re.sub(rf"<@!?{user.id}>", label, prompt)

        if not prompt:
            await message.reply("👋 Ask me something - e.g. `@me what are my passing stats this month?`")
            return

        async with message.channel.typing():
            try:
                reply = await self._answer(message, prompt)
            except Exception:
                log.exception("Chat request failed")
                reply = "⚠️ Something went wrong talking to the AI."

        first = True
        for part in _chunks(reply):
            if first:
                await message.reply(part, mention_author=False)
                first = False
            else:
                await message.channel.send(part)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatCog(bot))
