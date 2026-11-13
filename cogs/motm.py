"""Historical squad MOTM records and quiet closure of legacy vote messages.

New squad votes and winner announcements are retired. EA MOTM remains on
result cards. Existing messages can finish through their persistent handlers;
failed result edits retry without posting a new channel message.
"""

from clubs import club_id, club_name, club_scoped, match_key
from clubs import club_for
import json
import asyncio
import delivery
import logging
import time
from collections import Counter
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import match_data as md
from cogs.link import get_link
from config import MOTM_VOTE_MINUTES
from db import connect, now_iso
from interaction_tracking import TrackedView, failed
from utils import clip, is_manager, survive

log = logging.getLogger("madboys-bot.motm")

GOLD = 0xF1C40F


# --------------------------------------------------------------------------- #
#  DB
# --------------------------------------------------------------------------- #
def get_poll_by_message(message_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM motm_polls WHERE message_id=?", (str(message_id),)).fetchone()
    return dict(row) if row else None


def get_votes(match_id: str) -> dict[str, str]:
    """{voter_id: candidate}"""
    with connect() as conn:
        return {r["voter_id"]: r["candidate"] for r in
                conn.execute("SELECT voter_id, candidate FROM motm_votes WHERE match_id=?", (match_id,))}


def cast_vote(match_id: str, voter_id: str, candidate: str):
    with connect() as conn:
        conn.execute("""
            INSERT INTO motm_votes (match_id, voter_id, candidate, voted_at) VALUES (?,?,?,?)
            ON CONFLICT(match_id, voter_id) DO UPDATE SET candidate=excluded.candidate, voted_at=excluded.voted_at
        """, (match_id, voter_id, candidate, now_iso()))


def season_table(since_ts: int = 0) -> list[tuple[str, int, int]]:
    """[(name, awards, total votes received)] best first, from closed polls."""
    awards: Counter = Counter()
    votes: Counter = Counter()
    with connect() as conn:
        polls = conn.execute("""
            SELECT p.match_id, p.winners FROM motm_polls p CROSS JOIN matches m ON m.club_id=p.club_id
              AND m.match_id=CASE WHEN instr(p.match_id,':')>0 THEN substr(p.match_id,instr(p.match_id,':')+1) ELSE p.match_id END
            WHERE p.closed=1 AND p.club_id=? AND m.ts>=?""", (club_id(), since_ts)).fetchall()
        for p in polls:
            for w in json.loads(p["winners"] or "[]"):
                awards[w] += 1
        # all the votes in one go instead of one query per poll
        ids = json.dumps([p["match_id"] for p in polls])
        for r in conn.execute("SELECT candidate, COUNT(*) c FROM motm_votes"
                              " WHERE match_id IN (SELECT value FROM json_each(?)) GROUP BY candidate", (ids,)):
            votes[r["candidate"]] += r["c"]
    names = set(awards) | set(votes)
    return sorted(((n, awards[n], votes[n]) for n in names), key=lambda x: (-x[1], -x[2], x[0].lower()))


# --------------------------------------------------------------------------- #
#  Embeds
# --------------------------------------------------------------------------- #
def open_embed(poll: dict, n_votes: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"🗳️ Squad MOTM — {poll['title']}",
        description=f"Who was **your** man of the match? Pick below.\n"
                    f"Voting closes <t:{poll['closes_at']}:R>. Results stay hidden until then.",
        colour=GOLD)
    if poll.get("ea_motm"):
        embed.add_field(name="EA's pick", value=f"⭐ {poll['ea_motm']}", inline=True)
    embed.add_field(name="Votes so far", value=str(n_votes), inline=True)
    embed.set_footer(text="One vote each • you can change it • no voting for yourself 😄")
    return embed


def results_embed(poll: dict, votes: dict[str, str], winners: list[str]) -> discord.Embed:
    tally = Counter(votes.values()).most_common()
    total = sum(c for _, c in tally)
    lines = []
    for name, c in tally:
        bar = "█" * max(1, round(10 * c / total)) if total else ""
        lines.append(f"{'🏆' if name in winners else '▫️'} **{name}** — {c} vote{'s' if c != 1 else ''}  `{bar}`")
    embed = discord.Embed(title=f"🏆 Squad MOTM — {poll['title']}",
                          description=clip("\n".join(lines), 4000) if lines else "No votes this time.",
                          colour=GOLD)
    if poll.get("ea_motm"):
        agree = poll["ea_motm"] in winners
        embed.add_field(name="EA's pick", value=f"⭐ {poll['ea_motm']}" + (" — agrees with the squad ✅" if agree else ""),
                        inline=False)
    embed.set_footer(text=f"Voting closed • {total} vote{'s' if total != 1 else ''}")
    return embed


def build_table_embed() -> discord.Embed:
    rows = season_table()
    if not rows:
        return discord.Embed(title="🏆 Squad MOTM awards", description="No votes closed yet.", colour=GOLD)
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else f'`{i + 1:>2}.`'} **{n}** — {a} award{'s' if a != 1 else ''} "
             f"({v} vote{'s' if v != 1 else ''})" for i, (n, a, v) in enumerate(rows[:15])]
    embed = discord.Embed(title=f"🏆 {club_name()} — Squad MOTM awards", description="\n".join(lines), colour=GOLD)
    embed.set_footer(text="Voted by the squad after each match • ties share the award")
    return embed


# --------------------------------------------------------------------------- #
#  The vote dropdown
# --------------------------------------------------------------------------- #
class MotmView(TrackedView):
    def __init__(self, options: Optional[list[discord.SelectOption]] = None):
        super().__init__(timeout=None)
        self.select = discord.ui.Select(
            custom_id="madboys:motm:vote", placeholder="🗳️ Vote for your MOTM…",
            options=options or [discord.SelectOption(label="(loading)", value="_")])
        self.select.callback = self.on_vote
        self.add_item(self.select)

    async def on_vote(self, interaction: discord.Interaction):
        poll = get_poll_by_message(interaction.message.id)
        if not poll:
            await interaction.response.send_message("This vote no longer exists.", ephemeral=True)
            return
        if poll["closed"] or time.time() >= poll["closes_at"]:
            await interaction.response.send_message("Voting has closed for this match.", ephemeral=True)
            return
        choice = interaction.data["values"][0]
        if choice not in json.loads(poll["candidates"]):
            await interaction.response.send_message("That player isn't on the list.", ephemeral=True)
            return
        me = get_link(poll["guild_id"], str(interaction.user.id))
        if me and me.lower() == choice.lower():
            await interaction.response.send_message("Nice try — you can't vote for yourself 😄", ephemeral=True)
            return
        previous = get_votes(poll["match_id"]).get(str(interaction.user.id))
        cast_vote(poll["match_id"], str(interaction.user.id), choice)
        msg = (f"Changed your vote from **{previous}** to **{choice}**." if previous and previous != choice
               else f"Voted for **{choice}** ✅")
        await interaction.response.send_message(msg + f" You can change it until <t:{poll['closes_at']}:t>.",
                                                ephemeral=True)
        try:
            # keep the message's options (so the dropdown doesn't reset to "loading") and update the count
            await interaction.message.edit(embed=open_embed(poll, len(get_votes(poll["match_id"]))))
        except discord.HTTPException:
            pass


class MotmCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Retired automatic voting: never replay a queued vote or winner ping.
        with connect() as conn:
            conn.execute("DELETE FROM delivery_jobs WHERE kind IN ('motm_open','motm_announce')")
            conn.execute('UPDATE motm_polls SET closes_at=MIN(closes_at,?) WHERE closed=0',(int(time.time()),))
        self.closer.start()

    def cog_unload(self):
        self.closer.cancel()

    async def _deliver(self,job,payload):
        poll=payload['poll']
        channel_id=int(poll['channel_id'])
        channel=self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        if poll.get('message_id'):
            try:
                msg=await channel.fetch_message(int(poll['message_id']))
                await msg.edit(embed=results_embed(poll,payload['votes'],payload['winners']),view=None)
            except discord.NotFound:
                return

    async def retry_deliveries(self):
        if not hasattr(self,'_delivery_lock'):
            self._delivery_lock=asyncio.Lock()
        async with self._delivery_lock:
            await delivery.dispatch(('motm_edit',),self._deliver)

    async def close_poll(self, poll: dict):
        votes=get_votes(poll['match_id'])
        tally=Counter(votes.values())
        top=max(tally.values()) if tally else 0
        winners=sorted(n for n,c in tally.items() if c==top) if top else []
        with connect() as conn:
            current=conn.execute('SELECT closed FROM motm_polls WHERE match_id=?',(poll['match_id'],)).fetchone()
            if not current or current['closed']:
                return
            conn.execute('UPDATE motm_polls SET closed=1,winners=? WHERE match_id=?',(json.dumps(winners),poll['match_id']))
            payload={'poll':poll,'votes':votes,'winners':winners,'top':top}
            cid=poll.get('club_id',club_id())
            delivery.enqueue('motm_edit',poll['match_id'],poll['guild_id'],cid,payload,conn=conn)
            # Existing votes close by editing their message; no winner post.
        await self.retry_deliveries()

    @tasks.loop(seconds=20)
    @survive
    async def closer(self):
        await self.retry_deliveries()
        with connect() as conn:
            conn.execute("UPDATE motm_polls SET closed=1,winners='[]' WHERE closed=0 AND message_id IS NULL AND closes_at<=? "
                         "AND NOT EXISTS (SELECT 1 FROM delivery_jobs j WHERE j.kind='motm_open' AND j.job_key=motm_polls.match_id AND j.expires_at>?)",(int(time.time()),int(time.time())))
            due=[dict(r) for r in conn.execute('SELECT * FROM motm_polls WHERE closed=0 AND message_id IS NOT NULL AND closes_at<=?',(int(time.time()),))]
        for poll in due:
            await self.close_poll(poll)

    @closer.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    motm_group = app_commands.Group(name="motm", description="Squad Man of the Match votes")

    @motm_group.command(name="table", description="Season table of squad MOTM awards")
    @club_scoped
    async def motm_table(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=build_table_embed())

    @motm_group.command(name="close", description="Manager: close the open MOTM vote now")
    @club_scoped
    async def motm_close(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            failed(interaction)
            await interaction.response.send_message("Managers only.", ephemeral=True)
            return
        with connect() as conn:
            # Any club's open vote: a manager following MADBOYS can still close a GRAYSBOYS vote.
            row = conn.execute("SELECT * FROM motm_polls WHERE closed=0 AND guild_id=? ORDER BY closes_at DESC LIMIT 1",
                               (str(interaction.guild_id),)).fetchone()
        if not row:
            await interaction.response.send_message("No open vote.", ephemeral=True)
            return
        await interaction.response.send_message("Closing the vote…", ephemeral=True)
        await self.close_poll(dict(row))


async def setup(bot: commands.Bot):
    bot.add_view(MotmView())  # re-attach vote handlers after a restart
    await bot.add_cog(MotmCog(bot))
