"""Exercise durable message stages, retry deadlines, scope and private diagnostics."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import discord
import db
import delivery as D
import match_data as md
from clubs import club_scope, monitored_clubs, club_id
from cogs import motm, positions, operations
from performance import measure
from tests.test_squad_features import Base

def unavailable():
    return discord.HTTPException(SimpleNamespace(status=503,reason='Unavailable'),'try later')

class DeliveryRecoveryTests(Base):
    async def test_retirement_discards_queued_votes_and_announcements(self):
        D.enqueue('motm_open','m','10',club_id(),{})
        D.enqueue('motm_announce','m','10',club_id(),{})
        cog=motm.MotmCog(SimpleNamespace())
        cog.cog_unload()
        self.assertIsNone(D.get('motm_open','m'))
        self.assertIsNone(D.get('motm_announce','m'))

    async def test_backoff_persists_and_increases_without_repeated_attempts(self):
        calls=AsyncMock(side_effect=unavailable())
        with patch.object(D.time,'time',return_value=1000):
            D.enqueue('test','one','10',club_id(),{})
            await D.dispatch(('test',),calls)
            await D.dispatch(('test',),calls)
        calls.assert_awaited_once()
        self.assertEqual(D.get('test','one')['next_attempt'],1030)
        with patch.object(D.time,'time',return_value=1030):
            await D.dispatch(('test',),calls)
        self.assertEqual(D.get('test','one')['next_attempt'],1090)
        calls.side_effect=None
        with patch.object(D.time,'time',return_value=1090):
            await D.dispatch(('test',),calls)
        self.assertIsNone(D.get('test','one'))

    async def test_expired_jobs_are_not_delivered(self):
        calls=AsyncMock()
        with patch.object(D.time,'time',return_value=1000):
            D.enqueue('test','expired','10',club_id(),{},lifetime=30)
        with patch.object(D.time,'time',return_value=1030):
            await D.dispatch(('test',),calls)
        calls.assert_not_awaited()

    async def test_retired_motm_edits_retry_without_winner_announcements(self):
        with db.connect() as conn:
            conn.execute("INSERT INTO motm_polls (match_id,guild_id,channel_id,message_id,title,candidates,closes_at,club_id) VALUES ('m','10','20','77','Club game','[\"Ali\",\"Bob\"]',2000,?)",(club_id(),))
            conn.execute("INSERT INTO motm_votes VALUES ('m','42','Ali','now')")
            poll=dict(conn.execute('SELECT * FROM motm_polls').fetchone())
        message=SimpleNamespace(edit=AsyncMock(side_effect=unavailable()))
        channel=SimpleNamespace(fetch_message=AsyncMock(return_value=message),send=AsyncMock())
        cog=object.__new__(motm.MotmCog)
        cog.bot=SimpleNamespace(get_channel=lambda _:channel)
        with patch.object(D.time,'time',return_value=1000):
            await cog.close_poll(poll)
        channel.send.assert_not_awaited()
        message.edit.side_effect=None
        with patch.object(D.time,'time',return_value=1031):
            await cog.retry_deliveries()
            await cog.close_poll(poll)
        channel.send.assert_not_awaited()
        self.assertEqual(message.edit.await_count,2)
        self.assertIsNone(D.get('motm_edit','m'))

    async def test_position_retry_preserves_club_and_does_not_consume_failed_nudge(self):
        secondary=monitored_clubs()[1]
        channel=SimpleNamespace(id=20,send=AsyncMock(side_effect=unavailable()))
        cog=object.__new__(positions.PositionsCog)
        cog.bot=SimpleNamespace(get_channel=lambda _:channel)
        with db.connect() as conn:
            conn.executemany('INSERT INTO rotation_log (guild_id,club,discord_id,position,logged_at,source,match_id) VALUES (?,?,?,?,?,?,?)',
                [('10',secondary['name'],'42','CB','now','auto','m'),('10',secondary['name'],'99','CB','now','player','m')])
        with patch.object(D.time,'time',return_value=1000),club_scope(secondary):
            await cog.open_prompt(channel,'10','m','Secondary game',{'42':'defender','99':'defender'},['Unlinked'])
        self.assertIsNone(db.get_setting('10','linknudge:unlinked'))
        restarted=object.__new__(positions.PositionsCog)
        restarted.bot=cog.bot
        channel.send.side_effect=None
        channel.send.return_value=SimpleNamespace(id=77)
        with patch.object(D.time,'time',return_value=1031):
            await restarted.retry_deliveries()
            await restarted.retry_deliveries()
        self.assertEqual(channel.send.await_count,2)
        self.assertIn('<@42>',channel.send.call_args.args[0])
        self.assertNotIn('<@99>',channel.send.call_args.args[0])
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT club_id FROM position_prompts').fetchone()[0],secondary['club_id'])
        self.assertEqual(db.get_setting('10','linknudge:unlinked'),'1031')

    async def test_status_shows_queued_items_complete_check_and_measured_timings(self):
        now=int(time.time())
        D.enqueue('position','m','10',club_id(),{})
        with db.connect() as conn:
            conn.execute('INSERT INTO matchday_outbox VALUES (?,?,?,?)',('10',club_id(),'m',now-60))
        tracker=SimpleNamespace(ticker=SimpleNamespace(is_running=lambda:True),last_poll_partial=True,
            last_poll_ok=False,last_poll_at=now,last_success_at=None,last_complete_at=now-120,next_poll_at=now+30,
            channel_id_for=lambda _:20,posting_enabled=lambda _:True,club_status={})
        bot=SimpleNamespace(get_cog=lambda name:tracker if name=='MatchdayCog' else None)
        with measure('Database work'):
            pass
        embed=operations.status_embed(bot,'10')
        fields={f.name:f.value for f in embed.fields}
        self.assertIn('position: **1**',fields['Pending deliveries'])
        self.assertIn('results: **1**',fields['Pending deliveries'])
        self.assertIn(str(now-120),fields['Last complete EA check'])
        self.assertIn('Recap progress',fields)
        self.assertIn('Database work',fields['Recent performance'])
