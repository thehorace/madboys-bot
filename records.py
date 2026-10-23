"""
Club records from every game the bot has saved (per club).

Ties go to whoever set the record first, so a record only changes hands when it's beaten.
`broken_by()` says which records a batch of new games just broke, for the "New club record!" post.
"""

import json
from typing import Optional

import discord

from db import connect

MIN_GAMES_TO_ANNOUNCE = 20   # don't celebrate "records" while the history is still tiny

# single-game player records: key -> (column, label, unit, extra SQL filter)
PLAYER_GAME = {
    "goals":   ("mp.goals", "Most goals in a game", "goals", ""),
    "assists": ("mp.assists", "Most assists in a game", "assists", ""),
    "rating":  ("mp.rating", "Highest match rating", "rating", ""),
    "saves":   ("mp.saves", "Most saves in a game", "saves", "AND LOWER(mp.pos)='goalkeeper'"),
    "tackles": ("mp.tackles_made", "Most tackles in a game", "tackles", ""),
}
# all-time player totals: key -> (SQL expression, label, unit)
PLAYER_TOTAL = {
    "apps":    ("COUNT(*)", "Most appearances", "games"),
    "goals":   ("SUM(mp.goals)", "Most goals", "goals"),
    "assists": ("SUM(mp.assists)", "Most assists", "assists"),
    "motm":    ("SUM(mp.motm)", "Most EA man of the match", "MOTM"),
}


def _game(row) -> dict:
    return {"ts": row["ts"], "match_id": row["match_id"], "opp": row["opp_name"] or "Opponent",
            "our": row["our_goals"], "their": row["opp_goals"]}


def _streaks(results: list) -> dict:
    """Longest win run and unbeaten run, plus the current run, from results oldest first."""
    best = {"win": None, "unbeaten": None}
    run = {"win": [], "unbeaten": []}
    for r in results:
        for kind, ok in (("win", r["result"] == "W"), ("unbeaten", r["result"] != "L")):
            if ok:
                run[kind].append(r)
                if best[kind] is None or len(run[kind]) > best[kind]["length"]:
                    best[kind] = {"length": len(run[kind]), "start": run[kind][0]["ts"], "end": r["ts"],
                                  "end_match": r["match_id"], "start_match": run[kind][0]["match_id"]}
            else:
                run[kind] = []
    current = None
    if results:
        last = results[-1]["result"]
        n = 0
        for r in reversed(results):
            if r["result"] != last:
                break
            n += 1
        current = {"result": last, "length": n}
    return {"win": best["win"], "unbeaten": best["unbeaten"], "current": current}


def compute(cid: int) -> dict:
    out: dict = {"games": 0, "team": {}, "player_game": {}, "player_total": {}}
    with connect() as conn:
        results = [dict(r) for r in conn.execute(
            "SELECT match_id, ts, result, our_goals, opp_goals, opp_name FROM matches WHERE club_id=? ORDER BY ts, match_id",
            (cid,))]
        out["games"] = len(results)
        if not results:
            return out
        team = out["team"]
        big = max(results, key=lambda r: (r["our_goals"] - r["opp_goals"], r["our_goals"], -r["ts"]))
        if big["our_goals"] > big["opp_goals"]:
            team["biggest_win"] = _game(big)
        bad = max(results, key=lambda r: (r["opp_goals"] - r["our_goals"], r["opp_goals"], -r["ts"]))
        if bad["opp_goals"] > bad["our_goals"]:
            team["heaviest_defeat"] = _game(bad)
        most = max(results, key=lambda r: (r["our_goals"], -r["ts"]))
        if most["our_goals"]:
            team["most_goals"] = _game(most)
        team.update(_streaks(results))

        for key, (col, _, _, extra) in PLAYER_GAME.items():
            row = conn.execute(f"""
                SELECT mp.name, {col} AS value, m.match_id, m.ts, m.opp_name, m.our_goals, m.opp_goals
                FROM matches m CROSS JOIN match_players mp ON mp.club_id=m.club_id AND mp.match_id=m.match_id
                WHERE m.club_id=? AND {col} IS NOT NULL AND {col} > 0 {extra}
                ORDER BY {col} DESC, m.ts ASC LIMIT 1""", (cid,)).fetchone()
            if row:
                out["player_game"][key] = {"name": row["name"], "value": row["value"], **_game(row)}
        cols = ", ".join(f"{expr} AS {key}" for key, (expr, _, _) in PLAYER_TOTAL.items())
        totals = conn.execute(f"SELECT MIN(mp.name) AS name, MIN(mp.rowid) AS first, {cols} FROM match_players mp "
                              f"WHERE mp.club_id=? GROUP BY LOWER(mp.name)", (cid,)).fetchall()
        for key in PLAYER_TOTAL:   # one pass for all four; ties go to whoever appeared first
            best = min((r for r in totals if (r[key] or 0) > 0), key=lambda r: (-r[key], r["first"]), default=None)
            if best:
                out["player_total"][key] = {"name": best["name"], "value": best[key]}
    return out


def _value(key: str, v) -> str:
    return f"{v:.1f}" if key == "rating" else str(v)


def _when(g: dict) -> str:
    return f"{g['our']}–{g['their']} vs {g['opp']} · <t:{g['ts']}:d>"


def build_embed(club_name: str, cid: int, potd_leaders: Optional[list] = None,
                colour: int = 0xF1C40F) -> discord.Embed | str:
    rec = compute(cid)
    if not rec["games"]:
        return f"No games saved for {club_name} yet — records appear after the first tracked match."
    t = rec["team"]
    embed = discord.Embed(title=f"🏆 {club_name} — Club records", colour=colour,
                          description=f"From all {rec['games']} games the bot has saved. Ties go to whoever did it first.")
    lines = []
    if t.get("biggest_win"):
        lines.append(f"**Biggest win:** {_when(t['biggest_win'])}")
    if t.get("heaviest_defeat"):
        lines.append(f"**Heaviest defeat:** {_when(t['heaviest_defeat'])}")
    if t.get("most_goals"):
        lines.append(f"**Most goals we've scored in a game:** {t['most_goals']['our']} ({_when(t['most_goals'])})")
    for kind, label in (("win", "Longest win streak"), ("unbeaten", "Longest unbeaten run")):
        s = t.get(kind)
        if s:
            lines.append(f"**{label}:** {s['length']} games (<t:{s['start']}:d> → <t:{s['end']}:d>)")
    cur = t.get("current")
    if cur:
        word = {"W": "won", "D": "drawn", "L": "lost"}[cur["result"]]
        lines.append(f"**Right now:** {word} the last {cur['length']}")
    embed.add_field(name="🛡️ Team", value="\n".join(lines)[:1024] or "—", inline=False)

    lines = []
    for key, (_, label, unit, _) in PLAYER_GAME.items():
        r = rec["player_game"].get(key)
        if r:
            lines.append(f"**{label}:** {discord.utils.escape_markdown(r['name'])} — {_value(key, r['value'])} "
                         f"{'' if key == 'rating' else unit}".rstrip() + f" ({r['our']}–{r['their']} vs {r['opp']}, <t:{r['ts']}:d>)")
    if lines:
        embed.add_field(name="⚡ In a single game", value="\n".join(lines)[:1024], inline=False)

    lines = []
    for key, (_, label, unit) in PLAYER_TOTAL.items():
        r = rec["player_total"].get(key)
        if r:
            lines.append(f"**{label}:** {discord.utils.escape_markdown(r['name'])} — {r['value']} {unit}")
    if potd_leaders:
        name, n = potd_leaders[0]
        lines.append(f"**Most Player of the Day:** {discord.utils.escape_markdown(name)} — {n}")
    if lines:
        embed.add_field(name="📚 All-time (tracked games)", value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text="New records are announced in the results channel when they're broken")
    return embed


def broken_by(cid: int, new_match_ids: set[str], announced_streak: Optional[str] = None) -> tuple[list[str], Optional[str]]:
    """
    Plain lines for records the new games just set, plus the streak key to remember
    (so a win streak is announced once when it becomes the record, not after every extra win).
    """
    rec = compute(cid)
    if rec["games"] - len(new_match_ids) < MIN_GAMES_TO_ANNOUNCE:
        return [], announced_streak
    t, lines = rec["team"], []
    if t.get("biggest_win", {}).get("match_id") in new_match_ids:
        lines.append(f"🏆 **Biggest win ever:** {_when(t['biggest_win'])}")
    elif t.get("most_goals", {}).get("match_id") in new_match_ids:
        lines.append(f"🏆 **Most goals we've ever scored in a game:** {t['most_goals']['our']} ({_when(t['most_goals'])})")
    for key, (_, label, unit, _) in PLAYER_GAME.items():
        r = rec["player_game"].get(key)
        if r and r["match_id"] in new_match_ids:
            lines.append(f"🏆 **{label}:** {discord.utils.escape_markdown(r['name'])} — {_value(key, r['value'])}"
                         f"{'' if key == 'rating' else ' ' + unit} vs {r['opp']}")
    s = t.get("win")
    streak_key = announced_streak
    if s and s["end_match"] in new_match_ids and s["length"] >= 3:
        key = f"{s['start_match']}"
        if key != announced_streak:
            lines.append(f"🏆 **Longest win streak ever:** {s['length']} games and counting")
            streak_key = key
    return lines, streak_key
