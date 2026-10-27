"""Atomic session batches and durable Player of the Day retry regression tests."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import db
import potd
from cogs import session_reports as reports
from tests.test_squad_features import Base

START = 1_800_000_000


class OctoberReviewTests(Base):
    def setUp(self):
        super().setUp()
        db.set_setting('10','reports:since',str(START-100))
        db.set_setting('10','reports:gap_v30','1')
        db.set_setting('10','reports:dms','0')
        db.set_setting('10','reports:channel','20')
        self.bot = SimpleNamespace(get_cog=lambda _:None)
        self.cog = reports.SessionReportsCog(self.bot)
        self.channel = SimpleNamespace(id=20,name='general',send=AsyncMock(return_value=SimpleNamespace(id=100)))
        self.guild = SimpleNamespace(id=10,text_channels=[self.channel],get_channel=lambda _:self.channel,voice_channels=[])

    def session(self, starts):
        with db.connect() as conn:
            return conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by) VALUES ('10','20',?,'daily')",(starts,)).lastrowid

    async def poll(self, now):
        with patch.object(reports.time,'time',return_value=now), patch.object(reports,'report_health',AsyncMock()):
            await self.cog.after_poll(self.guild)

    async def test_snapshot_failure_rolls_back_the_whole_batch(self):
        self.session(START)
        self.session(START+10000)
        first=self.game(START+600,players=[('Ali',{'r':8})])
        second=self.game(START+10600,players=[('Bob',{'r':7})])
        dumps=json.dumps
        def fail_second(value,*args,**kwargs):
            if value == [second]:
                raise RuntimeError('Interrupted while building the second snapshot')
            return dumps(value,*args,**kwargs)
        with patch.object(reports.json,'dumps',side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                reports.archive_sessions('10',START+15000)
        self.assertEqual(reports.history('10'),[])
        reports.archive_sessions('10',START+15000)
        records=reports.history('10')
        self.assertEqual(len(records),2)
        self.assertEqual({m for r in records for m in json.loads(r['match_ids'])},{first,second})

    async def test_failed_card_retries_after_restart_without_reposting_recap(self):
        sid=self.session(START)
        self.game(START+600,players=[('Ali',{'r':8,'g':2})])
        self.channel.send.side_effect=[SimpleNamespace(id=100),RuntimeError('Temporary Discord failure'),SimpleNamespace(id=101)]
        with patch.object(potd,'render_card',return_value=b'png'):
            await self.poll(START+2500)
            self.assertEqual(self.channel.send.await_count,2)
            self.assertEqual(db.get_setting('10',f'potd:pending:{sid}'),'1')
            self.assertEqual(reports.history('10')[0]['message_id'],'100')
            self.cog=reports.SessionReportsCog(self.bot)
            await self.poll(START+2600)
            self.assertEqual(self.channel.send.await_count,3)
            self.assertTrue(potd.already_done(sid))
            self.assertIsNone(db.get_setting('10',f'potd:pending:{sid}'))
            await self.poll(START+2700)
            self.assertEqual(self.channel.send.await_count,3)

    async def test_expired_queue_is_removed_and_old_recaps_are_not_backfilled(self):
        sid=self.session(START)
        self.game(START+600,players=[('Ali',{'r':8})])
        reports.archive_sessions('10',START+2500)
        with db.connect() as conn:
            conn.execute("UPDATE session_history SET message_id='100' WHERE session_id=?",(sid,))
        await self.poll(START+2600)
        self.channel.send.assert_not_awaited()  # no queue marker: older recap stays quiet
        db.set_setting('10',f'potd:pending:{sid}','1')
        await self.poll(START+90000)
        self.channel.send.assert_not_awaited()
        self.assertIsNone(db.get_setting('10',f'potd:pending:{sid}'))

    async def test_disabled_cards_clear_queue_without_later_catchup(self):
        sid=self.session(START)
        self.game(START+600,players=[('Ali',{'r':8})])
        reports.archive_sessions('10',START+2500)
        with db.connect() as conn:
            conn.execute("UPDATE session_history SET message_id='100' WHERE session_id=?",(sid,))
        db.set_setting('10',f'potd:pending:{sid}','1')
        db.set_setting('10','reports:potd','0')
        await self.poll(START+2600)
        self.assertIsNone(db.get_setting('10',f'potd:pending:{sid}'))
        db.set_setting('10','reports:potd','1')
        await self.poll(START+2700)
        self.channel.send.assert_not_awaited()
