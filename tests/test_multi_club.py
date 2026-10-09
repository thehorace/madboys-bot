import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import db
import clubs
import match_data as md
from config import CLUB_ID
from cogs import matchday, stats, motm, session_reports as reports, admin
from cogs.clubs import toggle_club
from cogs.link import set_link


class MultiClubTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(db, 'DB_PATH', str(Path(temp.name)/'clubs.db'))
        p.start()
        self.addCleanup(p.stop)
        db.init_all()
        self.primary, self.secondary = clubs.monitored_clubs()[:2]
        self.channel = SimpleNamespace(id=20, send=AsyncMock(return_value=SimpleNamespace(id=99)))
        self.reports = SimpleNamespace(after_poll=AsyncMock())
        self.guild = SimpleNamespace(id=10)
        self.bot = SimpleNamespace(get_cog=lambda n: self.reports if n=='SessionReportsCog' else None)
        self.tracker = object.__new__(matchday.MatchdayCog)
        self.tracker.bot = self.bot
        self.tracker.ea = SimpleNamespace(get_recent_matches_multi=AsyncMock())
        self.tracker.home_guild = lambda: self.guild
        self.tracker._linked_in, self.tracker._unlinked = {}, {}
        self.tracker._log_rotation = lambda *a: {}
        self.tracker._migrated = False
        self.tracker.check_milestones = AsyncMock()
        self.tracker._channel = AsyncMock(return_value=self.channel)
        db.set_setting('10','matchday_channel','20')

    def raw(self, club, mid, ts, goals=2):
        return {'matchId':mid, 'timestamp':ts, 'clubs':{str(club['club_id']):{'goals':goals}, '999':{'goals':1,'details':{'name':'Rivals'}}},
                'players':{str(club['club_id']):{'p':{'playername':'Fauz','goals':goals,'assists':1,'rating':8},'q':{'playername':'Other','rating':7}}}}

    def store(self, club, mid, ts, goals=2):
        raw = self.raw(club,mid,ts,goals)
        pm = md.parse_match(raw,club['club_id'])
        md.store_match(club['club_id'],pm,raw)
        return pm

    def seed(self):
        for c in (self.primary,self.secondary):
            self.store(c,'baseline',100)

    async def test_both_detect_same_id_and_restart_does_not_repost(self):
        self.seed()
        self.tracker.ea.get_recent_matches_multi.side_effect = lambda cid, **kw: [self.raw(clubs.club_for(cid),'same',200)]
        self.assertEqual(await self.tracker.poll_once(),2)
        self.assertEqual(self.channel.send.await_count,2)
        for club, call in zip(clubs.monitored_clubs(),self.channel.send.call_args_list):
            self.assertIn(club['name'],call.kwargs['content'])
            self.assertIn(club['name'],call.kwargs['embed'].title)
            self.assertTrue(md.is_stored(club['club_id'],'same'))
        # State from an earlier process is not needed for deduplication.
        self.tracker.club_status = {}
        self.assertEqual(await self.tracker.poll_once(),0)
        self.assertEqual(self.channel.send.await_count,2)

    async def test_switching_both_directions_and_empty_poll(self):
        self.seed()
        for club in (self.primary,self.secondary,self.primary):
            mid = str(self.channel.send.await_count)
            self.tracker.ea.get_recent_matches_multi.side_effect=lambda cid,**kw: [self.raw(club,mid,300)] if cid==club['club_id'] else []
            self.assertEqual(await self.tracker.poll_once(),1)
        self.tracker.ea.get_recent_matches_multi.side_effect=lambda cid,**kw: []
        self.assertEqual(await self.tracker.poll_once(),0)
        self.assertEqual(self.channel.send.await_count,3)

    async def test_one_failure_does_not_block_other_or_close_session(self):
        self.seed()
        async def fetch(cid, **kw):
            if cid==CLUB_ID:
                raise RuntimeError('temporary relay error')
            return [self.raw(self.secondary,'new',200)]
        self.tracker.ea.get_recent_matches_multi.side_effect=fetch
        self.assertEqual(await self.tracker.poll_once(),1)
        self.assertEqual(self.tracker.ea.get_recent_matches_multi.await_count,2)
        self.channel.send.assert_awaited_once()
        self.assertFalse(self.tracker.last_poll_ok)
        self.reports.after_poll.assert_not_awaited()
        self.tracker.ea.get_recent_matches_multi.side_effect=lambda cid,**kw: []
        await self.tracker.poll_once()
        self.assertTrue(self.tracker.last_poll_ok)
        self.reports.after_poll.assert_awaited_once()

    async def test_initial_secondary_history_is_quiet(self):
        self.store(self.primary,'baseline',100)
        self.tracker.ea.get_recent_matches_multi.side_effect=lambda cid,**kw: [self.raw(clubs.club_for(cid),'new',200)]
        self.assertEqual(await self.tracker.poll_once(),2)
        self.channel.send.assert_awaited_once()
        self.assertIn(self.primary['name'],self.channel.send.call_args.kwargs['content'])

    async def test_context_isolated_across_concurrent_stats(self):
        self.store(self.primary,'a',100,3)
        self.store(self.secondary,'a',200,0)
        async def screen(club):
            with clubs.club_scope(club):
                await asyncio.sleep(0)
                return stats.build_form(), clubs.club_id()
        (a,aid),(b,bid)=await asyncio.gather(screen(self.primary),screen(self.secondary))
        self.assertIn('W1 D0 L0',a.description)
        self.assertIn('W0 D0 L1',b.description)
        self.assertNotEqual(aid,bid)
        self.assertEqual(clubs.club_id(),CLUB_ID)

    async def test_auto_selection_preference_and_disabled_fallback(self):
        self.store(self.primary,'a',100)
        self.store(self.secondary,'b',200)
        self.assertEqual(clubs.selected_club('10','42'),self.secondary)
        db.set_setting('10','club:42',str(CLUB_ID))
        self.assertEqual(clubs.selected_club('10','42'),self.primary)
        toggle_club(CLUB_ID)
        self.assertEqual(clubs.selected_club('10','42'),self.secondary)
        with self.assertRaises(ValueError):
            toggle_club(self.secondary['club_id'])
        db.init_all()
        self.assertNotIn(self.primary,clubs.monitored_clubs())

    async def test_motm_identifiers_and_awards_stay_separate(self):
        cog=object.__new__(motm.MotmCog)
        for club in (self.primary,self.secondary):
            pm=self.store(club,'same',200)
            with clubs.club_scope(club):
                await cog.open_poll(self.channel,'10',pm)
                key=clubs.match_key('same')
                motm.cast_vote(key,'42','Fauz')
                with db.connect() as conn:
                    conn.execute("UPDATE motm_polls SET closed=1,winners=? WHERE match_id=?",(json.dumps(['Fauz']),key))
                self.assertEqual(motm.season_table(),[('Fauz',1,1)])
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM motm_polls').fetchone()[0],2)
        self.assertEqual(self.channel.send.await_count,2)

    async def test_session_includes_both_without_merging_player_stats(self):
        start=1800000000
        db.set_setting('10','reports:since',str(start-100))
        set_link('10','42','Fauz','self')
        with db.connect() as conn:
            conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by) VALUES ('10','20',?,'daily')",(start,))
        self.store(self.primary,'same',start+100,3)
        self.store(self.secondary,'same',start+6000,1)
        reports.archive_sessions('10',start+8000)
        self.assertEqual(reports.history('10'),[])
        reports.archive_sessions('10',start+14000)
        record=reports.history('10')[0]
        self.assertEqual(len(json.loads(record['match_ids'])),2)
        players=[p for p in json.loads(record['players']) if p['name']=='Fauz']
        self.assertEqual({p['goals'] for p in players},{1,3})
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM session_summary_dms').fetchone()[0],1)
        embed=reports.recap_embed(record,'42')
        self.assertEqual(len(embed.fields),2)
        self.assertIn(self.secondary['name'],embed.fields[1].name)

    async def test_legacy_snapshot_and_database_remain_usable(self):
        self.store(self.primary,'legacy',100)
        record={'match_ids':'["legacy"]','players':'[]','session_id':1,'starts_at':1,'outcome':'completed'}
        self.assertIn('Club: 1 games',reports.recap_embed(record).description)
        db.init_all()
        self.assertTrue(md.is_stored(CLUB_ID,'legacy'))

    async def test_wrong_club_payload_is_rejected(self):
        self.assertIsNone(md.parse_match(self.raw(self.primary,'wrong',100),self.secondary['club_id']))

    async def test_bound_view_click_keeps_its_club_after_preference_changes(self):
        from cogs.hub import StatsMenu
        self.bot.ea = SimpleNamespace()
        with clubs.club_scope(self.secondary):
            menu=StatsMenu(self.bot,SimpleNamespace(id=42),'10',[],[])
        db.set_setting('10','club:42',str(CLUB_ID))
        menu.go=AsyncMock()
        menu.screen_failed=False
        interaction=SimpleNamespace(guild_id=10,user=SimpleNamespace(id=42),extras={},response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())
        seen=[]
        async def go(page):
            seen.append(clubs.club_id())
        menu.go.side_effect=go
        # Execute the actual dynamically tracked callback.
        menu.render()
        with patch('interaction_tracking.record'):
            await next(c for c in menu.children if getattr(c,'label',None)=='Home').callback(interaction)
        self.assertEqual(seen,[self.secondary['club_id']])

    async def test_persistent_position_prompt_resolves_original_club(self):
        from cogs.positions import PositionPromptView
        self.store(self.primary,'same',100)
        self.store(self.secondary,'same',200)
        with db.connect() as conn:
            conn.execute('INSERT INTO position_prompts (message_id,guild_id,match_id,title,pending,created_at,club_id) VALUES (?,?,?,?,?,?,?)',
                         ('77','10','same','GRAYSBOYS',json.dumps({'42':'defender'}),200,self.secondary['club_id']))
        view=PositionPromptView()
        interaction=SimpleNamespace(guild_id=10,user=SimpleNamespace(id=42),message=SimpleNamespace(id=77,edit=AsyncMock()),extras={},data={'values':['CB']},client=SimpleNamespace(get_cog=lambda n:None),response=SimpleNamespace(send_message=AsyncMock()))
        with patch('interaction_tracking.record'):
            await next(c for c in view.children if getattr(c,'custom_id',None)=='madboys:pos:pick').callback(interaction)
        with db.connect() as conn:
            row=conn.execute('SELECT club,position FROM rotation_log WHERE discord_id=?',('42',)).fetchone()
        self.assertEqual(tuple(row),(self.secondary['name'],'CB'))

    async def test_concurrent_manual_and_background_poll_share_one_check(self):
        self.seed()
        started, resume = asyncio.Event(), asyncio.Event()
        async def fetch(cid, **kwargs):
            started.set()
            await resume.wait()
            return [self.raw(clubs.club_for(cid),'new',200)]
        self.tracker.ea.get_recent_matches_multi.side_effect=fetch
        first=asyncio.create_task(self.tracker.poll_once())
        await started.wait()
        self.assertEqual(await self.tracker.poll_once(),0)
        resume.set()
        self.assertEqual(await first,2)
        self.assertEqual(self.tracker.ea.get_recent_matches_multi.await_count,2)
        self.assertEqual(self.channel.send.await_count,2)

    async def test_old_prompt_and_poll_migrations_preserve_records(self):
        with db.connect() as conn:
            conn.execute('ALTER TABLE motm_polls DROP COLUMN club_id')
            conn.execute('ALTER TABLE position_prompts DROP COLUMN club_id')
            conn.execute("INSERT INTO motm_polls (match_id,guild_id,channel_id,title,candidates,closes_at) VALUES ('legacy','10','20','Old vote','[]',123)")
            conn.execute("INSERT INTO position_prompts (message_id,guild_id,match_id,title,pending,created_at) VALUES ('77','10','legacy','Old prompt','{}',123)")
        db.init_all()
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT club_id FROM motm_polls WHERE match_id='legacy'").fetchone()[0],CLUB_ID)
            self.assertEqual(conn.execute("SELECT club_id FROM position_prompts WHERE message_id='77'").fetchone()[0],CLUB_ID)

    async def test_admin_clubs_screen_and_unauthorized_toggle(self):
        user=SimpleNamespace(id=42,name='fauz')
        panel=admin.AdminView(self.bot,user,self.guild,SimpleNamespace())
        panel.page='clubs'
        panel.render()
        self.assertIn(self.secondary['name'],panel.embed().description)
        interaction=SimpleNamespace(guild_id=10,user=SimpleNamespace(id=99),extras={},response=SimpleNamespace(send_message=AsyncMock()),data={'values':[str(CLUB_ID)]})
        await panel.toggle_club(interaction)
        self.assertEqual(len(clubs.monitored_clubs()),2)
        interaction.response.send_message.assert_awaited_once()
