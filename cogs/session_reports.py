"""Durable session recaps; private player summaries never use RSVP as attendance."""
import json
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from config import CLUB_ID, CLUB_COLOUR
from db import connect, get_setting, set_setting
from cogs.link import get_all_links
from cogs.operations import report_health
from interaction_tracking import TrackedView

log = logging.getLogger("madboys-bot.session_reports")


def report_settings(gid):
    return {"enabled": get_setting(gid, "reports:enabled") != "0",
            "dms": get_setting(gid, "reports:dms") != "0",
            "gap": int(get_setting(gid, "reports:gap") or 120),
            "channel": get_setting(gid, "reports:channel")}


def history(gid, session_id=None, offset=0):
    with connect() as conn:
        query = "SELECT h.*,s.starts_at,s.note FROM session_history h JOIN sessions s ON s.id=h.session_id WHERE h.guild_id=?"
        args = [gid]
        if session_id is not None:
            query += " AND h.session_id=?"
            args.append(session_id)
        return [dict(r) for r in conn.execute(query + " ORDER BY s.starts_at DESC LIMIT 20 OFFSET ?", [*args, offset])]


def archive_sessions(gid, now):
    """Group timestamped matches by planned session and inactivity; atomic snapshots."""
    gap = report_settings(gid)["gap"] * 60
    baseline = int(get_setting(gid, "reports:since") or now)
    if get_setting(gid, "reports:since") is None:
        set_setting(gid, "reports:since", str(now))
    with connect() as conn:
        # Only sessions not archived yet (this used to re-check every session ever, every poll).
        sessions = conn.execute("SELECT * FROM sessions WHERE guild_id=? AND (starts_at<=? OR cancelled=1) "
                                "AND id NOT IN (SELECT session_id FROM session_history) ORDER BY starts_at,id",
                                (gid, now)).fetchall()
        if not sessions:
            return
        # Matches already given to a session: only recent history can overlap these sessions.
        oldest = min(s["starts_at"] for s in sessions)
        assigned = {mid for r in conn.execute("SELECT match_ids FROM session_history WHERE guild_id=? AND ended_at>=?",
                                              (gid, oldest - 2 * 86400)) for mid in json.loads(r[0])}
        for s in sessions:
            if conn.execute("SELECT 1 FROM session_history WHERE session_id=?", (s["id"],)).fetchone():
                continue
            next_start = conn.execute("SELECT MIN(starts_at) FROM sessions WHERE guild_id=? AND starts_at>? AND cancelled=0", (gid, s["starts_at"])).fetchone()[0]
            boundary = min(next_start or now + 1, now + 1)
            rows = conn.execute("SELECT * FROM matches WHERE club_id=? AND ts>=? AND ts<? ORDER BY ts,match_id", (CLUB_ID, s["starts_at"], boundary)).fetchall()
            matches = []
            for match in rows:
                if match["match_id"] in assigned:
                    continue
                # A distant game belongs to a different playing session.
                previous = matches[-1]["ts"] if matches else s["starts_at"]
                if match["ts"] - previous > gap:
                    break
                matches.append(dict(match))
            cancelled = bool(s["cancelled"])
            if not cancelled and matches and now - matches[-1]["ts"] < gap:
                continue
            if not cancelled and not matches and now - s["starts_at"] < gap:
                continue
            if cancelled:
                matches = []
            ids = [m["match_id"] for m in matches]
            assigned.update(ids)
            players = []
            if ids:
                marks = ",".join("?" for _ in ids)
                players = [dict(p) for p in conn.execute(
                    f"SELECT name,COUNT(*) games,SUM(goals) goals,SUM(assists) assists,AVG(rating) rating,SUM(motm) motm,SUM(saves) saves,SUM(tackles_made) tackles,SUM(passes_made) passes,SUM(pass_attempts) attempts FROM match_players WHERE club_id=? AND match_id IN ({marks}) GROUP BY name COLLATE NOCASE",
                    [CLUB_ID, *ids])]
            links = get_all_links(gid)
            for player in players:
                player["discord_ids"] = [did for did, name in links.items() if name.casefold() == player["name"].casefold()]
            rsvps = [dict(r) for r in conn.execute("SELECT discord_id,status,source FROM session_rsvps WHERE session_id=?", (s["id"],))]
            # Old records remain browsable but never create startup catch-up posts.
            finish = matches[-1]["ts"] + gap if matches else s["starts_at"] + gap
            message = "suppressed" if finish <= baseline else None
            conn.execute("INSERT INTO session_history VALUES (?,?,?,?,?,?,?,?)", (s["id"], gid,
                matches[-1]["ts"] if matches else now, "cancelled" if cancelled else "completed" if matches else "no games",
                json.dumps(ids), json.dumps(rsvps), json.dumps(players), message))
            if message is None and ids and report_settings(gid)["dms"]:
                recipients = {did for p in players for did in p["discord_ids"]}
                conn.executemany("INSERT OR IGNORE INTO session_summary_dms (session_id,discord_id) VALUES (?,?)",
                                 [(s["id"], did) for did in recipients])


def recap_embed(record, personal_id=None):
    ids = json.loads(record["match_ids"])
    with connect() as conn:
        rows = [conn.execute("SELECT * FROM matches WHERE club_id=? AND match_id=?", (CLUB_ID, mid)).fetchone() for mid in ids]
    rows = [r for r in rows if r]
    results = " · ".join(f"{sum(r['result'] == outcome for r in rows)}{outcome}" for outcome in ("W", "D", "L"))
    embed = discord.Embed(title="My session summary" if personal_id else "Session finished",
        description=f"Session #{record['session_id']} · <t:{record['starts_at']}:f>\n**{record['outcome'].capitalize()}** · Club: {len(rows)} games · {results}", colour=CLUB_COLOUR)
    players = json.loads(record["players"])
    if personal_id:
        players = [p for p in players if personal_id in p.get("discord_ids", [])]
        if not players:
            embed.add_field(name="Your matches", value="No linked player stats for you in this session. Link your EA name using Setup before playing.", inline=False)
    else:
        embed.add_field(name="Club result", value=f"{sum(r['our_goals'] for r in rows)} scored · {sum(r['opp_goals'] for r in rows)} conceded\n{len(players)} players recorded by EA", inline=False)
        if rows:
            embed.add_field(name="Results (latest 10)", value="\n".join(
                f"{r['result']} · {r['our_goals']}–{r['opp_goals']} vs {r['opp_name'] or 'Opponent'}" for r in rows[-10:])[:1024], inline=False)
        if players:
            lines = []
            for p in sorted(players, key=lambda p: (-(p["goals"] or 0), -(p["assists"] or 0))):
                rating = f"{p['rating']:.2f}" if p["rating"] is not None else "—"
                lines.append(f"**{p['name'][:40]}** · {p['games']} games · {p['goals']}G {p['assists']}A · {rating} rating")
            embed.add_field(name="Squad performance", value="\n".join(lines)[:1024], inline=False)
        players = []
    for p in players[:10]:
        rating = f"{p['rating']:.2f}" if p["rating"] is not None else "—"
        passing = f"{100 * p['passes'] / p['attempts']:.0f}%" if p["attempts"] else "—"
        embed.add_field(name=p["name"][:256], value=f"**{p['games']} games** · {p['goals']} goals · {p['assists']} assists\nRating **{rating}** · {p['motm']} MOTM\n{p['tackles']} tackles · {p['saves']} saves · Passing {passing}", inline=False)
    embed.set_footer(text="Finished after inactivity • Stats from EA recorded matches; sign-ups are not attendance")
    return embed


class SummaryView(TrackedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="My session summary", style=discord.ButtonStyle.primary, custom_id="sessions:personal-summary")
    async def summary(self, interaction, _):
        with connect() as conn:
            row = conn.execute("SELECT session_id FROM session_history WHERE guild_id=? AND message_id=?", (str(interaction.guild_id), str(interaction.message.id))).fetchone()
        records = history(str(interaction.guild_id), row[0]) if row else []
        await interaction.response.send_message(embed=recap_embed(records[0], str(interaction.user.id)) if records else None,
            content=None if records else "This session summary is unavailable.", ephemeral=True)


class HistoryView(TrackedView):
    def __init__(self, user_id, guild_id, records, private=False, offset=0):
        super().__init__(timeout=600)
        self.user_id, self.guild_id, self.private = user_id, guild_id, private
        self.offset = offset
        self.selected = records[0]["session_id"]
        picker = discord.ui.Select(placeholder="Choose a finished session", custom_id="sessions:history-select",
            options=[discord.SelectOption(label=f"Session #{r['session_id']}", value=str(r["session_id"]),
                description=f"{r['outcome'].capitalize()} · {len(json.loads(r['match_ids']))} games") for r in records])
        picker.callback = self.choose
        self.add_item(picker)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id or str(interaction.guild_id) != self.guild_id:
            await interaction.response.send_message("Open your own session history from the panel.", ephemeral=True)
            return False
        if self.private:
            from cogs.usage import can_view
            if not can_view(interaction.user, interaction.guild):
                await interaction.response.send_message("This admin history is private.", ephemeral=True)
                return False
        return True

    async def choose(self, interaction):
        self.selected = int(interaction.data["values"][0])
        record = history(self.guild_id, self.selected)[0]
        embed = recap_embed(record)
        if self.private:
            players = json.loads(record["players"])
            embed.add_field(name="Actual players (EA)", value=", ".join(p["name"] for p in players)[:1024] or "None recorded", inline=False)
            rsvps = json.loads(record["rsvps"])
            embed.add_field(name="Sign-ups (not attendance)", value="\n".join(f"{r['status']}: <@{r['discord_id']}>" for r in rsvps)[:1024] or "No sign-ups", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="My summary for this session", custom_id="sessions:history-personal", row=1)
    async def personal(self, interaction, _):
        record = history(self.guild_id, self.selected)[0]
        await interaction.response.edit_message(embed=recap_embed(record, str(interaction.user.id)), view=self)

    async def paginate(self, interaction, offset):
        records = history(self.guild_id, offset=offset)
        if not records:
            await interaction.response.send_message("No more sessions in that direction.", ephemeral=True)
            return
        view = HistoryView(self.user_id, self.guild_id, records, self.private, offset)
        await interaction.response.edit_message(embed=recap_embed(records[0]), view=view)

    @discord.ui.button(label="Older sessions", custom_id="sessions:history-older", row=1)
    async def older(self, interaction, _):
        await self.paginate(interaction, self.offset + 20)

    @discord.ui.button(label="Newer sessions", custom_id="sessions:history-newer", row=1)
    async def newer(self, interaction, _):
        await self.paginate(interaction, max(0, self.offset - 20))


class SessionReportsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def after_poll(self, guild):
        gid = str(guild.id)
        now = int(time.time())
        archive_sessions(gid, now)
        if not report_settings(gid)["enabled"]:
            with connect() as conn:
                conn.execute("UPDATE session_history SET message_id='suppressed' WHERE guild_id=? AND message_id IS NULL", (gid,))
        with connect() as conn:
            pending = conn.execute("SELECT session_id FROM session_history WHERE guild_id=? AND message_id IS NULL", (gid,)).fetchall()
        errors = []
        for r in pending:
            record = history(gid, r[0])[0]
            if record["outcome"] != "completed" or now - record["ended_at"] > 86400:
                with connect() as conn:
                    conn.execute("UPDATE session_history SET message_id='suppressed' WHERE session_id=?", (r[0],))
                continue
            channel_id = report_settings(gid)["channel"]
            choices = [c for c in guild.text_channels if c.name.lower() == "general"] if not channel_id else []
            channel = guild.get_channel(int(channel_id)) if channel_id else choices[0] if len(choices) == 1 else None
            try:
                if channel is None:
                    raise RuntimeError("Session recap channel is unavailable. Choose General in the summary settings.")
                message = await channel.send(embed=recap_embed(record), view=SummaryView(), allowed_mentions=discord.AllowedMentions.none())
                with connect() as conn:
                    conn.execute("UPDATE session_history SET message_id=? WHERE session_id=?", (str(message.id), r[0]))
            except Exception as exc:
                errors.append(exc)
        errors.extend(await self.send_personal_summaries(guild, now))
        if errors:
            raise errors[0]
        await report_health(self.bot, gid, "Session summaries")

    async def send_personal_summaries(self, guild, now):
        """Retry transient failures per recipient; never replay delivered or old DMs."""
        gid = str(guild.id)
        with connect() as conn:
            pending = conn.execute("SELECT d.session_id,d.discord_id,h.ended_at FROM session_summary_dms d "
                "JOIN session_history h ON h.session_id=d.session_id WHERE h.guild_id=? AND d.status='pending'", (gid,)).fetchall()
        errors = []
        for delivery in pending:
            sid, did = delivery["session_id"], delivery["discord_id"]
            status, message_id = "pending", None
            if not report_settings(gid)["dms"]:
                status = "suppressed"
            elif now - delivery["ended_at"] > 86400:
                status = "expired"
            else:
                try:
                    member = guild.get_member(int(did)) or await guild.fetch_member(int(did))
                    record = history(gid, sid)[0]
                    message = await member.send(embed=recap_embed(record, did), allowed_mentions=discord.AllowedMentions.none())
                    status, message_id = "sent", str(message.id)
                except discord.Forbidden:
                    status = "blocked"
                    log.info("Session %s DM blocked for %s; summary remains in the panel", sid, did)
                except discord.NotFound:
                    status = "unavailable"
                except Exception as exc:
                    errors.append(exc)
                    log.warning("Session %s DM failed for %s; will retry", sid, did)
            with connect() as conn:
                conn.execute("UPDATE session_summary_dms SET status=?,message_id=? WHERE session_id=? AND discord_id=?", (status, message_id, sid, did))
        return errors

    async def show_history(self, interaction, private=False):
        if private:
            from cogs.usage import can_view
            if not can_view(interaction.user, interaction.guild):
                await interaction.response.send_message("This admin history is private.", ephemeral=True)
                return
        records = history(str(interaction.guild_id))
        embed = discord.Embed(title="Session history", colour=CLUB_COLOUR)
        if records:
            embed.description = "Choose a session below for its recap or your personal stats. Browse older sessions with the buttons."
        for record in records[:15]:
            players = json.loads(record["players"])
            text = f"{record['outcome'].capitalize()} · {len(json.loads(record['match_ids']))} games · {len(players)} actual players"
            if private:
                rsvps = json.loads(record["rsvps"])
                text += f"\nSign-ups: {sum(r['status']=='yes' for r in rsvps)} yes · {sum(r['status']=='maybe' for r in rsvps)} maybe"
            embed.add_field(name=f"Session #{record['session_id']}", value=f"<t:{record['starts_at']}:f>\n{text}", inline=False)
        if not records:
            embed.description = "No finished sessions yet. History saves after a successful match check."
        await interaction.response.send_message(embed=embed,
            view=HistoryView(interaction.user.id, str(interaction.guild_id), records, private) if records else None, ephemeral=True)

    async def show_personal(self, interaction):
        records = [r for r in history(str(interaction.guild_id)) if r["outcome"] == "completed"]
        await interaction.response.send_message(embed=recap_embed(records[0], str(interaction.user.id)) if records else None,
            content=None if records else "No completed session yet. Your summary appears after the inactivity gap.", ephemeral=True)

    @app_commands.command(name="sessionhistory", description="Browse finished sessions and club results")
    @app_commands.guild_only()
    async def sessionhistory(self, interaction: discord.Interaction):
        await self.show_history(interaction)

    @app_commands.command(name="mysession", description="Your private summary of the latest finished session")
    @app_commands.guild_only()
    async def mysession(self, interaction: discord.Interaction):
        await self.show_personal(interaction)


async def setup(bot):
    bot.add_view(SummaryView())
    await bot.add_cog(SessionReportsCog(bot))
