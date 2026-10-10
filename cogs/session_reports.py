"""Durable session recaps; private player summaries never use RSVP as attendance."""
import json
import logging
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from config import CLUB_ID, CLUB_COLOUR
from db import connect, get_setting, set_setting
from cogs.link import get_all_links
from cogs.operations import report_health
from interaction_tracking import TrackedView
from clubs import monitored_clubs, match_key, match_club, raw_match_key, club_for

log = logging.getLogger("madboys-bot.session_reports")


def report_settings(gid):
    with connect() as conn:
        saved = {r["key"]: r["value"] for r in conn.execute(
            "SELECT key,value FROM settings WHERE guild_id=? AND key LIKE 'reports:%'", (gid,))}
    return {"enabled": saved.get("reports:enabled") != "0",
            "dms": saved.get("reports:dms") != "0",
            "gap": int(saved.get("reports:gap") or 120),
            "channel": saved.get("reports:channel")}


def history(gid, session_id=None, offset=0):
    with connect() as conn:
        query = "SELECT h.*,s.starts_at,s.note FROM session_history h JOIN sessions s ON s.id=h.session_id WHERE h.guild_id=?"
        args = [gid]
        if session_id is not None:
            query += " AND h.session_id=?"
            args.append(session_id)
        return [dict(r) for r in conn.execute(query + " ORDER BY s.starts_at DESC,s.id DESC LIMIT 20 OFFSET ?", [*args, offset])]


def latest_personal(gid, discord_id):
    """Find this player's latest session directly, even beyond the first history page."""
    with connect() as conn:
        row = conn.execute("SELECT h.*,s.starts_at,s.note FROM session_history h JOIN sessions s ON s.id=h.session_id "
            "WHERE h.guild_id=? AND h.outcome='completed' AND EXISTS "
            "(SELECT 1 FROM json_each(h.players) p, json_each(p.value,'$.discord_ids') d WHERE d.value=?) "
            "ORDER BY s.starts_at DESC,s.id DESC LIMIT 1", (gid, discord_id)).fetchone()
    return dict(row) if row else None


def archive_sessions(gid, now):
    """Group timestamped matches by planned session and inactivity; atomic snapshots."""
    settings = report_settings(gid)
    gap = settings["gap"] * 60
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
        links = get_all_links(gid)
        # Matches already given to a session: only recent history can overlap these sessions.
        oldest = min(s["starts_at"] for s in sessions)
        assigned = {mid for r in conn.execute("SELECT match_ids FROM session_history WHERE guild_id=? AND ended_at>=?",
                                              (gid, oldest - 2 * 86400)) for mid in json.loads(r[0])}
        for s in sessions:
            if conn.execute("SELECT 1 FROM session_history WHERE session_id=?", (s["id"],)).fetchone():
                continue
            next_start = conn.execute("SELECT MIN(starts_at) FROM sessions WHERE guild_id=? AND starts_at>? AND cancelled=0", (gid, s["starts_at"])).fetchone()[0]
            boundary = min(next_start or now + 1, now + 1)
            club_ids = [c['club_id'] for c in monitored_clubs()] or [CLUB_ID]
            marks = ','.join('?' for _ in club_ids)
            rows = conn.execute(f"SELECT * FROM matches WHERE club_id IN ({marks}) AND ts>=? AND ts<? ORDER BY ts,club_id,match_id", (*club_ids, s["starts_at"], boundary)).fetchall()
            matches = []
            for match in rows:
                if match_key(match["match_id"], match['club_id']) in assigned:
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
            ids = [match_key(m["match_id"], m['club_id']) for m in matches]
            assigned.update(ids)
            players = []
            for cid in sorted({m['club_id'] for m in matches}):
                raw_ids = [m['match_id'] for m in matches if m['club_id'] == cid]
                marks = ','.join('?' for _ in raw_ids)
                group = [dict(p) for p in conn.execute(
                    f"SELECT name,COUNT(*) games,SUM(goals) goals,SUM(assists) assists,AVG(rating) rating,SUM(motm) motm,SUM(saves) saves,SUM(tackles_made) tackles,SUM(passes_made) passes,SUM(pass_attempts) attempts FROM match_players WHERE club_id=? AND match_id IN ({marks}) GROUP BY name COLLATE NOCASE",
                    [cid, *raw_ids])]
                for p in group:
                    p.update(club_id=cid, club_name=club_for(cid)['name'])
                players.extend(group)
            for player in players:
                player["discord_ids"] = [did for did, name in links.items() if name.casefold() == player["name"].casefold()]
            rsvps = [dict(r) for r in conn.execute("SELECT discord_id,status,source FROM session_rsvps WHERE session_id=?", (s["id"],))]
            # Old records remain browsable but never create startup catch-up posts.
            finish = matches[-1]["ts"] + gap if matches else s["starts_at"] + gap
            message = "suppressed" if finish <= baseline else None
            conn.execute("INSERT INTO session_history VALUES (?,?,?,?,?,?,?,?)", (s["id"], gid,
                matches[-1]["ts"] if matches else now, "cancelled" if cancelled else "completed" if matches else "no games",
                json.dumps(ids), json.dumps(rsvps), json.dumps(players), message))
            if message is None and ids and settings["dms"]:
                recipients = {did for p in players for did in p["discord_ids"]}
                conn.executemany("INSERT OR IGNORE INTO session_summary_dms (session_id,discord_id) VALUES (?,?)",
                                 [(s["id"], did) for did in recipients])


def session_matches(record):
    ids = json.loads(record["match_ids"])
    by_club = {}
    for mid in ids:
        by_club.setdefault(match_club(mid), set()).add(raw_match_key(mid))
    rows = []
    with connect() as conn:
        for cid, match_ids in by_club.items():
            match_ids = sorted(match_ids)
            for start in range(0, len(match_ids), 900):
                batch = match_ids[start:start+900]
                marks = ','.join('?' for _ in batch)
                rows.extend(conn.execute(f"SELECT * FROM matches WHERE club_id=? AND match_id IN ({marks})", [cid, *batch]).fetchall())
    return sorted((dict(r) for r in rows), key=lambda r: (r['ts'], r['club_id'], r['match_id']))


def actual_player_count(players):
    return len({p['name'].casefold() for p in players})


def result_words(rows):
    labels = []
    for code, single, plural in [('W','win','wins'),('D','draw','draws'),('L','loss','losses')]:
        count = sum(r['result'] == code for r in rows)
        labels.append(f"{count} {single if count == 1 else plural}")
    return ' · '.join(labels)


def display_name(name):
    return discord.utils.escape_markdown(str(name)[:45])


def standout(players, stat, label, emoji):
    best = max((p.get(stat) or 0 for p in players), default=0)
    if not best:
        return None
    names = sorted({p['name'] for p in players if (p.get(stat) or 0) == best}, key=str.casefold)
    who = ', '.join(display_name(n) for n in names[:2])
    if len(names) > 2:
        who += f" +{len(names)-2} more"
    return f"{emoji} **{who}** — {best} {label[:-1] if best == 1 else label}"


def compact_recap(record, rows):
    players = json.loads(record['players'])
    count = actual_player_count(players)
    completed = record['outcome'] == 'completed'
    embed = discord.Embed(title='🏁 Session recap' if completed else f"Session {record['outcome']}", colour=CLUB_COLOUR,
        description=f"<t:{record['starts_at']}:D>\n\n**{result_words(rows)}**\n"
                    f"{len(rows)} {'match' if len(rows) == 1 else 'matches'} · {count} {'player' if count == 1 else 'players'}\n"
                    f"⚽ **{sum(r['our_goals'] for r in rows)}** goals scored · **{sum(r['opp_goals'] for r in rows)}** conceded")
    for cid in sorted({r['club_id'] for r in rows}):
        games = [r for r in rows if r['club_id'] == cid]
        squad = [p for p in players if p.get('club_id', CLUB_ID) == cid]
        highlights = [standout(squad,'goals','goals','⚽'), standout(squad,'assists','assists','🎯')]
        lines = [f"**{len(games)} {'match' if len(games) == 1 else 'matches'}** · {result_words(games)}",
                 f"{sum(r['our_goals'] for r in games)} scored · {sum(r['opp_goals'] for r in games)} conceded"]
        lines += [''] + [h for h in highlights if h] if any(highlights) else []
        embed.add_field(name=f"🛡️ {club_for(cid)['name']}", value='\n'.join(lines), inline=False)
    embed.set_footer(text=f"Session #{record['session_id']} • Full results and squad stats below • EA recorded matches")
    return embed


def recap_embed(record, personal_id=None):
    rows = session_matches(record)
    if personal_id is None:
        return compact_recap(record, rows)
    players = json.loads(record["players"])
    players = [p for p in players if personal_id in p.get("discord_ids", [])]
    played = sum(p['games'] for p in players)
    embed = discord.Embed(title="My session summary", colour=CLUB_COLOUR,
        description=f"Session #{record['session_id']} · <t:{record['starts_at']}:f>\n"
                    f"**You played {played} {'match' if played == 1 else 'matches'}**\n"
                    f"Club session: {len(rows)} matches · {result_words(rows)}")
    if not players:
        embed.add_field(name="Your matches", value="No linked player stats for you in this session. Link your EA name using Setup before playing.", inline=False)
    for p in players[:10]:
        rating = f"{p['rating']:.2f}" if p["rating"] is not None else "—"
        passing = f"{100 * p['passes'] / p['attempts']:.0f}%" if p["attempts"] else "—"
        embed.add_field(name=f"{p['name']} · {p.get('club_name', club_for(CLUB_ID)['name'])}"[:256], value=f"**{p['games']} games** · {p['goals']} goals · {p['assists']} assists\nRating **{rating}** · {p['motm']} MOTM\n{p['tackles']} tackles · {p['saves']} saves · Passing {passing}", inline=False)
    embed.set_footer(text="Finished after inactivity • Stats from EA recorded matches; sign-ups are not attendance")
    return embed


def detail_pages(record):
    """Paginate whole entries instead of cutting off names/Markdown at 1024 chars."""
    rows = session_matches(record)
    players = json.loads(record['players'])
    pages = []
    club_ids = sorted({r['club_id'] for r in rows} | {p.get('club_id', CLUB_ID) for p in players})
    for cid in club_ids:
        results = [f"{'🟢' if r['result']=='W' else '🟡' if r['result']=='D' else '🔴'} "
                   f"**{r['our_goals']}–{r['opp_goals']}** vs {display_name(r['opp_name'] or 'Opponent')} · <t:{r['ts']}:t>"
                   for r in rows if r['club_id'] == cid]
        squad = sorted((p for p in players if p.get('club_id', CLUB_ID)==cid), key=lambda p: (-(p.get('goals') or 0), -(p.get('assists') or 0), p['name'].casefold()))
        stats = []
        for p in squad:
            rating = f"{p['rating']:.2f}" if p.get('rating') is not None else '—'
            stats.append(f"**{display_name(p['name'])}**\n{p['games']} matches · {p['goals'] or 0} goals · {p['assists'] or 0} assists · {rating} rating")
        for title, entries in [('Match results', results), ('Squad stats', stats)]:
            chunks, chunk = [], []
            for entry in entries:
                if chunk and (len('\n\n'.join([*chunk, entry])) > 950 or len(chunk) >= 6):
                    chunks.append(chunk)
                    chunk = []
                chunk.append(entry)
            if chunk:
                chunks.append(chunk)
            for chunk in chunks:
                embed = discord.Embed(title=f"{title} · {club_for(cid)['name']}", description='\n\n'.join(chunk), colour=CLUB_COLOUR)
                pages.append(embed)
    if not pages:
        pages = [discord.Embed(title='Session details', description='No matches or player stats recorded.', colour=CLUB_COLOUR)]
    for i, embed in enumerate(pages):
        embed.set_footer(text=f"Session #{record['session_id']} • Page {i+1} of {len(pages)} • Stats stay separate for each club")
    return pages


class PrivateSessionView(TrackedView):
    """Keep the latest interaction token for visible timeout feedback."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.anchor = None

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.anchor is not None:
            try:
                await self.anchor.edit_original_response(
                    content='This menu has expired. Reopen it from Session history or Results & squad stats.', view=self)
            except discord.HTTPException:
                pass  # An expired interaction token must not break the timeout task.


class DetailsView(PrivateSessionView):
    def __init__(self, record, user_id):
        super().__init__(timeout=600)
        self.pages = detail_pages(record)
        self.user_id = user_id
        self.index = 0
        sections = {}
        for i, page in enumerate(self.pages):
            sections.setdefault(page.title, i)
        self.section = discord.ui.Select(placeholder='Jump to a club or section', row=1,
            options=[discord.SelectOption(label=title[:100], value=str(index)) for title,index in sections.items()])
        self.section.callback = self.jump
        self.add_item(self.section)
        self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.index == 0
        self.next_page.disabled = self.index == len(self.pages)-1
        for option in self.section.options:
            option.default = self.pages[int(option.value)].title == self.pages[self.index].title

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Open your own session details.', ephemeral=True)
            return False
        return True

    async def move(self, interaction, step):
        self.anchor = interaction
        self.index = max(0, min(len(self.pages)-1, self.index+step))
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def jump(self, interaction):
        value = interaction.data.get('values', [''])[0]
        if value not in {option.value for option in self.section.options}:
            await interaction.response.send_message('That section is unavailable. Reopen the session details.', ephemeral=True)
            return
        self.index = int(value)
        self.anchor = interaction
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label='Previous', emoji='◀️')
    async def previous(self, interaction, _):
        await self.move(interaction, -1)

    @discord.ui.button(label='Next', emoji='▶️')
    async def next_page(self, interaction, _):
        await self.move(interaction, 1)


async def send_details(interaction, record):
    view = DetailsView(record, interaction.user.id)
    view.anchor = interaction
    await interaction.response.send_message(embed=view.pages[0], view=view, ephemeral=True,
                                           allowed_mentions=discord.AllowedMentions.none())


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

    @discord.ui.button(label='Results & squad stats', emoji='📋', style=discord.ButtonStyle.secondary, custom_id='sessions:full-details')
    async def details(self, interaction, _):
        with connect() as conn:
            row = conn.execute('SELECT session_id FROM session_history WHERE guild_id=? AND message_id=?', (str(interaction.guild_id), str(interaction.message.id))).fetchone()
        records = history(str(interaction.guild_id), row[0]) if row else []
        if not records:
            await interaction.response.send_message('This session summary is unavailable.', ephemeral=True)
            return
        await send_details(interaction, records[0])


class HistoryView(PrivateSessionView):
    def __init__(self, user_id, guild_id, records, private=False, offset=0):
        super().__init__(timeout=600)
        self.user_id, self.guild_id, self.private = user_id, guild_id, private
        self.offset = offset
        self.selected = records[0]["session_id"]
        from cogs.sessions import _tz
        zone = _tz(guild_id)
        picker = discord.ui.Select(placeholder="Choose a finished session", custom_id="sessions:history-select",
            options=[discord.SelectOption(label=f"Session #{r['session_id']}", value=str(r["session_id"]),
                description=f"{datetime.fromtimestamp(r['starts_at'], zone):%d %b %Y %H:%M} · {r['outcome'].capitalize()} · {len(json.loads(r['match_ids']))} games") for r in records])
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
        self.anchor = interaction
        self.selected = int(interaction.data["values"][0])
        record = history(self.guild_id, self.selected)[0]
        embed = recap_embed(record)
        if self.private:
            players = json.loads(record["players"])
            names = sorted({p['name'] for p in players}, key=str.casefold)
            embed.add_field(name="Actual players (EA)", value=", ".join(names)[:1024] or "None recorded", inline=False)
            rsvps = json.loads(record["rsvps"])
            embed.add_field(name="Sign-ups (not attendance)", value="\n".join(f"{r['status']}: <@{r['discord_id']}>" for r in rsvps)[:1024] or "No sign-ups", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="My summary for this session", custom_id="sessions:history-personal", row=1)
    async def personal(self, interaction, _):
        self.anchor = interaction
        record = history(self.guild_id, self.selected)[0]
        await interaction.response.edit_message(embed=recap_embed(record, str(interaction.user.id)), view=self)

    @discord.ui.button(label='Results & squad stats', emoji='📋', custom_id='sessions:history-details', row=1)
    async def details(self, interaction, _):
        await send_details(interaction, history(self.guild_id, self.selected)[0])

    async def paginate(self, interaction, offset):
        records = history(self.guild_id, offset=offset)
        if not records:
            await interaction.response.send_message("No more sessions in that direction.", ephemeral=True)
            return
        view = HistoryView(self.user_id, self.guild_id, records, self.private, offset)
        view.anchor = interaction
        await interaction.response.edit_message(embed=recap_embed(records[0]), view=view)
        self.stop()  # An old page's timeout must not overwrite the replacement menu.

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
        enabled = report_settings(gid)["dms"]
        for delivery in pending:
            sid, did = delivery["session_id"], delivery["discord_id"]
            status, message_id = "pending", None
            if not enabled:
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
            text = f"{record['outcome'].capitalize()} · {len(json.loads(record['match_ids']))} games · {actual_player_count(players)} actual players"
            if private:
                rsvps = json.loads(record["rsvps"])
                text += f"\nSign-ups: {sum(r['status']=='yes' for r in rsvps)} yes · {sum(r['status']=='maybe' for r in rsvps)} maybe"
            embed.add_field(name=f"Session #{record['session_id']}", value=f"<t:{record['starts_at']}:f>\n{text}", inline=False)
        if not records:
            embed.description = "No finished sessions yet. History saves after a successful match check."
        view = HistoryView(interaction.user.id, str(interaction.guild_id), records, private) if records else None
        if view:
            view.anchor = interaction
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def show_personal(self, interaction):
        record = latest_personal(str(interaction.guild_id), str(interaction.user.id))
        await interaction.response.send_message(embed=recap_embed(record, str(interaction.user.id)) if record else None,
            content=None if record else "No finished session is linked to you yet. Link your EA account through Setup before playing; summaries appear after the inactivity gap.", ephemeral=True)

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
