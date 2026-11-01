"""A whole evening must stay together; monthly winners combine both clubs."""
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import awards as A
import badges as B
import db
import potd
from clubs import monitored_clubs
from config import CLUB_ID
from cogs import session_reports as R
from cogs.awards import AwardsCog, build_awards
from tests.test_squad_features import Base, SYD, ts


class CombinedEveningTests(Base):
    async def test_delayed_last_game_does_not_create_second_recap_or_potd(self):
        start=ts(2026,10,31,18)
        db.set_setting('10','reports:since',str(start-100))
        db.set_setting('10','reports:gap','30')  # migrate the problematic deployed default
        db.set_setting('10','reports:dms','0')
        with db.connect() as conn:
            conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by) VALUES ('10','20',?,'daily')",(start,))
        other=next(c['club_id'] for c in monitored_clubs() if c['club_id']!=CLUB_ID)
        mids=[self.game(start+3000+i*600,players=[('Ali',{'r':8,'g':1})],club=CLUB_ID if i<2 else other) for i in range(5)]
        channel=SimpleNamespace(id=20,name='general',send=AsyncMock(return_value=SimpleNamespace(id=99)))
        guild=SimpleNamespace(id=10,text_channels=[channel],get_channel=lambda _:channel,voice_channels=[])
        cog=R.SessionReportsCog(SimpleNamespace(get_cog=lambda _:None))
        with patch.object(R,'report_health',AsyncMock()), patch.object(potd,'render_card',return_value=b'png'):
            for now in (start+7300,start+8500):
                with patch.object(R.time,'time',return_value=now):
                    await cog.after_poll(guild)
                channel.send.assert_not_awaited()
                if now==start+7300:
                    mids.append(self.game(start+7200,players=[('Ali',{'r':8,'g':1})]))
            with patch.object(R.time,'time',return_value=start+14410):
                await cog.after_poll(guild)
        records=R.history('10')
        self.assertEqual(len(records),1)
        self.assertEqual(len(json.loads(records[0]['match_ids'])),6)
        self.assertEqual(channel.send.await_count,2)  # one recap + one combined POTD
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions WHERE created_by='auto'").fetchone()[0],0)


class CombinedMonthlyTests(Base):
    def setUp(self):
        super().setUp()
        minimum=patch.object(A,'MIN_POTM_GAMES',3)
        minimum.start()
        self.addCleanup(minimum.stop)

    def fill_month(self):
        secondary=next(c['club_id'] for c in monitored_clubs() if c['club_id']!=CLUB_ID)
        self.game(ts(2026,10,3),players=[('Ali',{'r':9,'g':1,'a':1}),('Bob',{'r':7.6})])
        self.game(ts(2026,10,4),players=[('Bob',{'r':7.6})])
        for i in range(3):
            self.game(ts(2026,10,5+i),players=[('ali',{'r':7,'g':2,'a':1}),('Bob',{'r':7.6})],club=secondary)

    async def test_combined_rating_is_weighted_by_games_and_totals_add_up(self):
        self.fill_month()
        result=A.compute_combined(2026,10,SYD)
        self.assertEqual(result['record']['games'],5)
        self.assertEqual(result['potm']['name'],'Bob') # 7.6 beats Ali's true 7.5, not mean(9,7)=8
        self.assertEqual(result['scorer']['value'],7)
        self.assertEqual(result['assister']['value'],4)
        self.assertEqual(result['min_games'],3)
        self.assertEqual(len(result['club_ids']),2)
        with db.connect() as conn:
            conn.execute('UPDATE monitored_clubs SET enabled=0 WHERE club_id<>?',(CLUB_ID,))
        self.assertEqual(A.compute_combined(2026,10,SYD)['record']['games'],5)
        with patch('cogs.awards._tz',return_value=SYD):
            embed,png=await build_awards('10','2026-10')
        self.assertIn('GRAYSBOYS',embed.title)
        self.assertIn('MADBOYS',embed.title)
        self.assertTrue(png.startswith(b'\x89PNG'))

    async def test_combined_award_supersedes_per_club_badges_and_winners(self):
        from cogs.link import set_link
        set_link('10','7','Ali','self')
        set_link('10','9','Bob','self')
        self.fill_month()
        for cid in (CLUB_ID,754785):
            A.save('10',cid,{'month':'2026-10','potm':{'name':'Ali'}},'old',1)
        A.save('10',A.COMBINED_ID,A.compute_combined(2026,10,SYD),'new',2)
        self.assertEqual(B._potm_wins('10','Ali'),[])
        self.assertEqual(len(B._potm_wins('10','Bob')),1)
        cog=object.__new__(AwardsCog)
        self.assertEqual(cog.month_winners('10','2026-10'),['9'])

    async def test_existing_per_club_posts_do_not_block_combined_correction(self):
        self.fill_month()
        for cid in (CLUB_ID,754785):
            A.save('10',cid,{'month':'2026-10','potm':{'name':'Ali'}},'old',1)
        channel=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=99)))
        cog=object.__new__(AwardsCog)
        cog.bot=SimpleNamespace(get_cog=lambda _:SimpleNamespace(channel_id_for=lambda _:20),get_channel=lambda _:channel)
        cog.update_role=AsyncMock()
        guild=SimpleNamespace(id=10)
        with patch('cogs.awards._tz',return_value=SYD),patch('cogs.awards.report_health',AsyncMock()):
            # No linked previous winners: do not need a BadgesCog in this fixture.
            self.assertEqual(await cog.post_due(guild,datetime(2026,11,1,11,tzinfo=SYD)),1)
            self.assertEqual(await cog.post_due(guild,datetime(2026,11,1,12,tzinfo=SYD)),0)
        channel.send.assert_awaited_once()
        self.assertTrue(A.already_posted('10',A.COMBINED_ID,'2026-10'))


class ExistingSessionRepairTests(Base):
    def setUp(self):
        super().setUp()
        from cogs.link import set_link
        set_link('10','42','Ali','self')
        set_link('10','43','Bob','self')
        self.start=ts(2026,10,31,18)
        db.set_setting('10','reports:gap','30')
        db.set_setting('10','reports:dms','0')
        db.set_setting('10','reports:channel','20')
        db.set_setting('10','reports:since',str(self.start-100))
        with db.connect() as conn:
            self.keep=conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by) VALUES ('10','20',?,'daily')",(self.start,)).lastrowid
            self.source=conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by) VALUES ('10','20',?,'auto')",(self.start+10000,)).lastrowid
        first=[self.game(self.start+600+i*600,players=[('Ali',{'r':8,'g':1})]) for i in range(3)]
        second=self.game(self.start+10600,players=[('Bob',{'r':9.9,'g':1})])
        R.archive_sessions('10',self.start+15000)
        potd.init_potd()
        with db.connect() as conn:
            conn.execute("UPDATE session_history SET message_id=CASE session_id WHEN ? THEN '100' ELSE '200' END",(self.keep,))
        for sid in (self.keep,self.source):
            record=R.history('10',sid)[0]
            result=potd.compute(R.session_matches(record))
            potd.save('10',sid,result,'42' if sid==self.keep else '43','101' if sid==self.keep else '201',self.start+15000)

    async def test_merge_is_audited_idempotent_and_recalculates_potd(self):
        from session_repair import merge_sessions
        result=merge_sessions('10',self.keep,self.source)
        self.assertEqual(len(R.history('10')),1)
        self.assertEqual(len(json.loads(result['target']['match_ids'])),4)
        self.assertEqual(result['result']['winner']['name'],'Ali')
        self.assertEqual(potd.count_for('10','Ali'),1)
        self.assertEqual(potd.count_for('10','Bob'),0)
        again=merge_sessions('10',self.keep,self.source)
        self.assertEqual(len(again['old_potd']),2)
        self.assertEqual(len(json.loads(again['target']['match_ids'])),4)
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM session_merge_audit').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM session_potd').fetchone()[0],1)

    async def test_merge_rejects_wrong_server_and_distant_evenings(self):
        from session_repair import merge_sessions
        with self.assertRaises(ValueError):
            merge_sessions('11',self.keep,self.source)
        with db.connect() as conn:
            conn.execute('UPDATE sessions SET starts_at=? WHERE id=?',(self.start+86400,self.source))
        with self.assertRaises(ValueError):
            merge_sessions('10',self.keep,self.source)
        self.assertEqual(len(R.history('10')),2)

    async def test_private_command_updates_existing_posts_without_sending_new_ones(self):
        messages={mid:SimpleNamespace(edit=AsyncMock()) for mid in (100,101,200,201)}
        channel=SimpleNamespace(fetch_message=AsyncMock(side_effect=lambda mid:messages[mid]),send=AsyncMock())
        guild=SimpleNamespace(id=10,text_channels=[],get_channel=lambda _:channel)
        bot=SimpleNamespace(get_cog=lambda _:None)
        cog=R.SessionReportsCog(bot)
        interaction=SimpleNamespace(guild_id=10,guild=guild,user=SimpleNamespace(id=42,name='fauz'),
            response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        with patch('cogs.usage.can_view',return_value=False):
            await R.SessionReportsCog.sessionmerge.callback(cog,interaction,self.keep,self.source)
        self.assertEqual(len(R.history('10')),2)
        with patch('cogs.usage.can_view',return_value=True),patch.object(potd,'render_card',return_value=b'png'):
            await R.SessionReportsCog.sessionmerge.callback(cog,interaction,self.keep,self.source)
        for message in messages.values():
            message.edit.assert_awaited_once()
        channel.send.assert_not_awaited()
        self.assertIn('full session',messages[101].edit.call_args.kwargs['embed'].description)
