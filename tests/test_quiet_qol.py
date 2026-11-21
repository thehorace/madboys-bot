"""Saved-session recovery, late results, private controls and confidence evidence."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import discord
import db
import delivery
import potd
import progress
import match_data as md
from clubs import monitored_clubs, club_scope
from cogs import session_reports as R, rotation
from tests import test_session_reports as fixtures


class QuietQolTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SessionReportTests.setUp
    game = fixtures.SessionReportTests.game
    poll = fixtures.SessionReportTests.poll
    interaction = fixtures.SessionReportTests.interaction

    def editable(self):
        message = SimpleNamespace(edit=AsyncMock())
        self.channel.fetch_message = AsyncMock(return_value=message)
        return message

    async def test_late_game_updates_existing_recap_and_snapshot_without_second_session_or_dm(self):
        self.game('a',self.start+600)
        await self.poll(self.start+8000)
        message = self.editable()
        self.game('late',self.start+1200)
        await self.poll(self.start+8100)
        await self.poll(self.start+8200)
        records = R.history('10')
        self.assertEqual(len(records),1)
        self.assertEqual(set(json.loads(records[0]['match_ids'])),{'a','late'})
        self.assertEqual(json.loads(records[0]['players'])[0]['games'],2)
        self.channel.send.assert_awaited_once()
        self.member.send.assert_awaited_once()
        message.edit.assert_awaited_once()
        self.assertIn('Updated with late EA results',message.edit.call_args.kwargs['embed'].footer.text)
        with db.connect() as conn:
            saved=json.loads(conn.execute('SELECT data FROM session_revision_audit').fetchone()[0])
        self.assertEqual(json.loads(saved['match_ids']),['a'])

    async def test_genuinely_later_game_still_gets_its_own_session(self):
        self.game('a',self.start+600)
        await self.poll(self.start+8000)
        self.game('next',self.start+12000)
        await self.poll(self.start+15000)
        self.assertEqual(len(R.history('10')),2)
        self.assertEqual(self.channel.send.await_count,2)

    async def test_edit_failure_retries_after_restart_in_original_channel(self):
        self.game('a',self.start+600)
        await self.poll(self.start+8000)
        message=self.editable()
        message.edit.side_effect=RuntimeError('Temporary failure')
        self.game('late',self.start+1200)
        await self.poll(self.start+8100)
        self.assertIsNotNone(delivery.get('recap_edit',f'10:{self.sid}'))
        message.edit.side_effect=None
        db.set_setting('10','reports:channel','777')  # existing recap stays in its original channel
        self.cog=R.SessionReportsCog(self.bot)
        await self.poll(self.start+8200)
        self.assertIsNone(delivery.get('recap_edit',f'10:{self.sid}'))
        self.assertEqual(message.edit.await_count,2)
        self.channel.send.assert_awaited_once()

    async def test_owner_recovery_edits_delivered_recap_and_rejects_foreign_or_suppressed(self):
        self.game('a',self.start+600)
        await self.poll(self.start+8000)
        message=self.editable()
        with patch.object(R.time,'time',return_value=self.start+8100):
            await self.cog.recover_record(self.guild,self.sid)
            with self.assertRaises(ValueError):
                await self.cog.recover_record(SimpleNamespace(id=11),self.sid)
            db.set_setting('10','reports:enabled','0')
            with self.assertRaises(ValueError):
                await self.cog.recover_record(self.guild,self.sid)
        message.edit.assert_awaited_once()
        self.channel.send.assert_awaited_once()

    async def test_late_game_recomputes_and_edits_existing_potd_without_ping(self):
        db.set_setting('10','reports:potd','1')
        self.game('a',self.start+600,rating=5)
        with patch.object(potd,'render_card',return_value=b'png'):
            await self.poll(self.start+8000)
            message=self.editable()
            self.game('late',self.start+1200,name='Other',rating=9)
            await self.poll(self.start+8100)
        with db.connect() as conn:
            winner=conn.execute('SELECT name FROM session_potd WHERE session_id=?',(self.sid,)).fetchone()[0]
        self.assertEqual(winner,'Other')
        self.assertEqual(message.edit.await_count,2)
        self.assertEqual(self.channel.send.await_count,2)  # original recap + original award only
        self.assertFalse(message.edit.call_args.kwargs['allowed_mentions'].users)

    async def test_history_filters_club_date_opponent_and_player_are_scoped(self):
        self.game('a',self.start+600)
        cid=monitored_clubs()[1]['club_id']
        with db.connect() as conn:
            conn.execute("UPDATE matches SET opp_name='Percent % Rivals' WHERE match_id='a'")
        await self.poll(self.start+8000)
        self.assertEqual(len(R.history('10',filters={'club':R.CLUB_ID,'player':'fauz','opponent':'% Rivals','since':self.start,'until':self.start+1})),1)
        for filters in ({'club':cid},{'player':'Other'},{'opponent':'_'},{'since':self.start+1}):
            self.assertEqual(R.history('10',filters=filters),[])
        self.assertEqual(R.history('11',filters={'player':'Fauz'}),[])

    async def test_empty_filtered_history_keeps_filters_and_owner_controls_private(self):
        view=R.HistoryView(42,'10',[],False,filters={'player':'Nobody'})
        self.assertTrue(any(c.label=='Clear filters' for c in view.children if isinstance(c,discord.ui.Button)))
        self.assertFalse(any(c.label=='Retry delivery' for c in view.children if isinstance(c,discord.ui.Button)))
        private=R.HistoryView(42,'10',[],True)
        self.assertFalse(await private.interaction_check(self.interaction(user=43,username='Other')))
        self.assertFalse(await private.interaction_check(self.interaction(username='Other')))

    async def test_progress_distinguishes_saved_delivery_from_open_session_and_names_blocked_club(self):
        self.game('a',self.start+600)
        embed=progress.session_embed('10',self.start+8000,tracker_ok=False,club_states=[{'name':'GRAYSBOYS','ok':False}])
        self.assertIn('Waiting on: GRAYSBOYS',embed.description)
        self.channel.send.side_effect=RuntimeError('Discord failed')
        with self.assertRaises(RuntimeError):
            await self.poll(self.start+8000)
        embed=progress.session_embed('10',self.start+8001)
        self.assertIn('Recap saved · delivery pending',embed.description)
        self.assertIn('Next retry',embed.description)

    async def test_inferred_slot_alone_is_not_rotation_evidence(self):
        self.assertEqual(rotation.evidence_position({'position':'CB','source':'saved','ea_role':None}),'Unknown')
        self.assertEqual(rotation.evidence_position({'position':'CB','source':'saved','ea_role':'DEF'}),'DEF')
        self.assertEqual(rotation.evidence_position({'position':'CB','source':'player'}),'CB')
        self.assertEqual(rotation.evidence_position({'position':'ST','source':'auto'}),'Unknown')

    async def test_late_game_same_id_in_other_club_keeps_separate_stats_and_saved_links(self):
        from cogs.link import set_link
        self.game('same',self.start+600)
        await self.poll(self.start+8000)
        self.editable()
        cid=monitored_clubs()[1]['club_id']
        raw={'matchId':'same','timestamp':self.start+1200,'clubs':{str(cid):{'goals':3},'999':{'goals':0}},
             'players':{str(cid):{'p':{'playername':'Fauz','pos':'forward','goals':2,'rating':9}}}}
        md.store_match(cid,md.parse_match(raw,cid),raw)
        set_link('10','42','Other','self')
        await self.poll(self.start+8100)
        record=R.history('10')[0]
        self.assertEqual(set(json.loads(record['match_ids'])),{'same',f'{cid}:same'})
        players=json.loads(record['players'])
        primary=next(p for p in players if p['club_id']==R.CLUB_ID)
        secondary=next(p for p in players if p['club_id']==cid)
        self.assertEqual(primary['discord_ids'],['42'])
        self.assertEqual(primary['goals'],1)
        self.assertEqual(secondary['goals'],2)
        self.assertEqual(len(R.history('10')),1)

    async def test_admin_milestone_controls_persist_and_refuse_other_users(self):
        import milestones
        from cogs.admin import AdminView
        interaction=self.interaction()
        panel=AdminView(self.bot,interaction.user,self.guild,interaction)
        panel.page='tracker_settings'
        panel.render()
        self.assertTrue(panel.to_components())
        interaction.data={'values':['goals']}
        await panel.choose_milestones(interaction)
        self.assertEqual(milestones.settings('10'),{'goals':True,'assists':False,'gamesPlayed':False})
        other=self.interaction(user=43,username='Other')
        other.data={'values':[]}
        await panel.choose_milestones(other)
        self.assertTrue(milestones.settings('10')['goals'])
        self.assertTrue(other.response.send_message.call_args.kwargs['ephemeral'])

    async def test_late_reconciliation_preserves_finish_gap_and_skips_unchanged_data(self):
        from session_updates import reconcile
        self.game('a',self.start+600)
        await self.poll(self.start+8000)
        self.editable()
        db.set_setting('10','reports:gap','120')
        self.game('too-late',self.start+5000)  # would fit the new gap, but not the archived 30-minute gap
        self.assertEqual(reconcile('10',self.start+8100,R.report_settings('10')),[])
        with patch('cogs.link.get_all_links',side_effect=AssertionError('unchanged data should skip link reads')):
            self.assertEqual(reconcile('10',self.start+8200,R.report_settings('10')),[])

    async def test_history_filter_modal_and_pagination_keep_selected_filters(self):
        from cogs.sessions import _tz
        from datetime import datetime
        self.game('a',self.start+600)
        await self.poll(self.start+8000)
        view=R.HistoryView(42,'10',R.history('10'),False)
        modal=R.HistoryFilter(view)
        modal.club._value=str(R.CLUB_ID)
        modal.player._value='fauz'
        modal.dates._value=datetime.fromtimestamp(self.start,_tz('10')).strftime('%Y-%m-%d')
        interaction=self.interaction()
        await modal.on_submit(interaction)
        replacement=interaction.response.edit_message.call_args.kwargs['view']
        self.assertEqual(replacement.filters['player'],'fauz')
        self.assertEqual(replacement.selected,self.sid)
        self.assertEqual(replacement.filters['club'],R.CLUB_ID)
        modal=R.HistoryFilter(replacement)
        modal.dates._value='2026-11-18..2026-10-01'
        invalid=self.interaction()
        await modal.on_submit(invalid)
        self.assertTrue(invalid.response.send_message.call_args.kwargs['ephemeral'])

    async def test_async_timings_cover_awaited_work(self):
        from performance import timed, snapshot
        label='QoL awaited timing test'
        @timed(label, slow=10)
        async def sample():
            self.assertNotIn(label,snapshot())  # no measurement until the body has actually run
            return 42
        with patch('performance.time.perf_counter',side_effect=[100,105]):
            self.assertEqual(await sample(),42)
        self.assertEqual(snapshot()[label]['mean'],5)

    async def test_compact_result_skips_image_render_and_persistent_details_restore_club(self):
        from cogs.result_details import ResultDetails
        raw={'matchId':'a','timestamp':self.start,'clubs':{str(R.CLUB_ID):{'goals':2},'999':{'goals':1}},'players':{}}
        pm=md.parse_match(raw,R.CLUB_ID)
        md.store_match(R.CLUB_ID,pm,raw)
        with patch('match_card.render_match_card') as render:
            embed,file=await md.match_post(pm,compact=True)
        render.assert_not_called()
        self.assertIsNone(file)
        restored=await ResultDetails.from_custom_id(None,None,{'club':str(R.CLUB_ID),'mid':'a'})
        interaction=self.interaction()
        interaction.response.defer=AsyncMock()
        interaction.followup=SimpleNamespace(send=AsyncMock())
        with patch.object(md,'match_post',AsyncMock(return_value=(discord.Embed(),None))) as post:
            await restored.callback(interaction)
        self.assertEqual(post.call_args.args[0].club_id,R.CLUB_ID)
        self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])


class MatchStatFieldTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SessionReportTests.setUp
    interaction = fixtures.SessionReportTests.interaction

    def raw(self):
        def p(name, **kw):
            return {'playername': name, **{k: str(v) for k, v in kw.items()}}   # EA sends numbers as strings
        return {'matchId': 'sc', 'timestamp': self.start,
                'clubs': {str(R.CLUB_ID): {'goals': 3}, '999': {'goals': 1, 'details': {'name': 'Rivals FC'}}},
                'players': {str(R.CLUB_ID): {
                    '1': p('Ace', goals=2, shots=4, passesmade=20, passattempts=25, tacklesmade=1, tackleattempts=4, rating=8.5),
                    '2': p('Bee', goals=1, shots=1, passesmade=30, passattempts=40, tacklesmade=5, tackleattempts=6, rating=7.0),
                    '3': p('Keeper', saves=4, rating=6.5)},
                    '999': {'9': p('Opp', goals=1, shots=8, passesmade=50, passattempts=80, tacklesmade=6,
                                   tackleattempts=20, saves=2, redcards=1)}}}

    def test_team_and_player_stats(self):
        fields = dict(md.match_stat_fields(self.raw(), R.CLUB_ID))
        team, players = fields['📊 Team stats'], fields['🎯 Our players']
        self.assertIn('3/5 (60%) · Rivals FC 1/8 (12%)', team)
        self.assertIn('50/65 (77%) · Rivals FC 50/80 (62%)', team)
        self.assertIn('6/10 (60%) · Rivals FC 6/20 (30%)', team)
        self.assertIn('4 · 0 · Rivals FC 2 · 1', team)
        self.assertIn('**Ace** — 2/4 shots · 80% passes (20/25) · 1/4 tackles', players)
        self.assertIn('**Keeper** — 4 saves', players)
        self.assertLess(players.index('Ace'), players.index('Bee'))   # highest rating first
        self.assertTrue(all(len(v) <= 1024 for v in fields.values()))

    def test_no_data_adds_nothing(self):
        raw = self.raw()
        for team in raw['players'].values():
            for p in team.values():
                for k in ('shots', 'passattempts', 'tackleattempts', 'saves'):
                    p[k] = '0'
        self.assertEqual(md.match_stat_fields(raw, R.CLUB_ID), [])

    async def test_details_button_shows_stats_privately(self):
        from cogs.result_details import ResultDetails
        raw = self.raw()
        md.store_match(R.CLUB_ID, md.parse_match(raw, R.CLUB_ID), raw)
        button = ResultDetails(R.CLUB_ID, 'sc')
        interaction = self.interaction()
        interaction.response.defer = AsyncMock()
        interaction.followup = SimpleNamespace(send=AsyncMock())
        with patch.object(md, 'match_post', AsyncMock(return_value=(discord.Embed(), None))):
            await button.callback(interaction)
        kwargs = interaction.followup.send.call_args.kwargs
        self.assertTrue(kwargs['ephemeral'])
        self.assertEqual([f.name for f in kwargs['embed'].fields], ['📊 Team stats', '🎯 Our players'])

    async def test_lastgame_command_has_private_details_button(self):
        from cogs.stats import StatsCog
        from cogs.result_details import ResultDetails
        raw = self.raw()
        md.store_match(R.CLUB_ID, md.parse_match(raw, R.CLUB_ID), raw)
        ea = SimpleNamespace(get_recent_matches_multi=AsyncMock(return_value=[raw]), stale_note=lambda: None)
        cog = StatsCog(SimpleNamespace(ea=ea, get_cog=lambda name: None))
        interaction = self.interaction()
        interaction.response.defer = AsyncMock()
        interaction.followup = SimpleNamespace(send=AsyncMock())
        with club_scope(R.CLUB_ID):
            await cog.lastgame.callback(cog, interaction)
        view = interaction.followup.send.call_args.kwargs['view']
        button = next(i.item for i in view.children if isinstance(i, ResultDetails))
        self.assertEqual(button.label, 'Full match stats')
        self.assertEqual(button.custom_id, f'result:details:{R.CLUB_ID}:sc')
