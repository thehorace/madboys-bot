import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import db
import clubs
from cogs import session_reports as reports


class RecapLayoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p=patch.object(db,'DB_PATH',str(Path(temp.name)/'recap.db'))
        p.start()
        self.addCleanup(p.stop)
        db.init_all()
        self.clubs=clubs.monitored_clubs()[:2]
        self.record={'session_id':9,'starts_at':1800000000,'outcome':'completed','match_ids':'[]','players':'[]'}

    def populate(self, count=9, roster=12):
        ids, players=[],[]
        with db.connect() as conn:
            for ci, club in enumerate(self.clubs):
                for i in range(count):
                    mid=f'{ci}-{i}'
                    ids.append(clubs.match_key(mid,club['club_id']))
                    conn.execute("INSERT INTO matches (club_id,match_id,match_type,ts,our_goals,opp_goals,result,opp_name,stored_at) VALUES (?,?,'leagueMatch',?,2,1,'W','Rivals','now')",(club['club_id'],mid,1800000000+i))
                for i in range(roster):
                    players.append({'name':f'Player {i:02} **with a long display name**','club_id':club['club_id'],'club_name':club['name'],
                                    'games':count,'goals':i,'assists':roster-i,'rating':8,'discord_ids':['42']})
        self.record.update(match_ids=json.dumps(ids),players=json.dumps(players))

    async def test_public_recap_is_compact_and_club_specific(self):
        self.populate()
        embed=reports.recap_embed(self.record)
        self.assertEqual(len(embed.fields),2)
        self.assertIn('18 wins',embed.description)
        self.assertIn('12 players',embed.description)
        self.assertLess(len(embed),1700)
        self.assertNotIn('7.41',embed.description)
        for club, field in zip(self.clubs,embed.fields):
            self.assertIn(club['name'],field.name)
            self.assertIn('11 goals',field.value)
            self.assertIn('12 assists',field.value)
            self.assertLessEqual(len(field.value),1024)

    async def test_full_details_keep_every_player_and_match_without_cutting_lines(self):
        self.populate(count=18,roster=32)
        pages=reports.detail_pages(self.record)
        self.assertGreater(len(pages),4)
        for club in self.clubs:
            relevant=[p for p in pages if club['name'] in p.title]
            results='\n'.join(p.description for p in relevant if 'Match results' in p.title)
            squad='\n'.join(p.description for p in relevant if 'Squad stats' in p.title)
            self.assertEqual(results.count('Rivals'),18)
            for i in range(32):
                self.assertIn(f'Player {i:02}',squad)
        self.assertTrue(all(len(p.description)<=950 and len(p)<=6000 for p in pages))

    async def test_details_navigation_is_private_and_bounded(self):
        self.populate()
        interaction=SimpleNamespace(user=SimpleNamespace(id=42),response=SimpleNamespace(send_message=AsyncMock(),edit_message=AsyncMock()))
        await reports.send_details(interaction,self.record)
        kwargs=interaction.response.send_message.call_args.kwargs
        self.assertTrue(kwargs['ephemeral'])
        view=kwargs['view']
        self.assertTrue(view.previous.disabled)
        await view.move(interaction,1000)
        self.assertTrue(view.next_page.disabled)
        self.assertEqual(view.index,len(view.pages)-1)
        interaction.user.id=99
        self.assertFalse(await view.interaction_check(interaction))

    async def test_persistent_details_button_finds_original_session(self):
        self.populate()
        with db.connect() as conn:
            conn.execute("INSERT INTO sessions (id,guild_id,channel_id,starts_at,created_by) VALUES (9,'10','20',?,'daily')",(self.record['starts_at'],))
            conn.execute("INSERT INTO session_history VALUES (9,'10',?,'completed',?,'[]',?,'99')",(1800009000,self.record['match_ids'],self.record['players']))
        interaction=SimpleNamespace(guild_id=10,user=SimpleNamespace(id=42),message=SimpleNamespace(id=99),extras={},response=SimpleNamespace(send_message=AsyncMock()))
        view=reports.SummaryView()
        with patch('interaction_tracking.record'):
            await view.details.callback(interaction)
        kwargs=interaction.response.send_message.call_args.kwargs
        self.assertTrue(kwargs['ephemeral'])
        self.assertIn('Session #9',kwargs['embed'].footer.text)

    async def test_jump_picker_opens_correct_club_and_section(self):
        self.populate(count=18)
        view=reports.DetailsView(self.record,42)
        option=next(o for o in view.section.options if 'Squad stats' in o.label and self.clubs[1]['name'] in o.label)
        interaction=SimpleNamespace(data={'values':[option.value]},response=SimpleNamespace(edit_message=AsyncMock(),send_message=AsyncMock()))
        await view.jump(interaction)
        self.assertEqual(view.pages[view.index].title,option.label)
        self.assertTrue(option.default)
        self.assertEqual(sum(o.default for o in view.section.options),1)
        before=view.index
        interaction.data={'values':['99999']}
        await view.jump(interaction)
        self.assertEqual(view.index,before)
        interaction.response.send_message.assert_awaited_once()

    async def test_history_counts_same_player_once_across_clubs(self):
        self.populate(roster=2)
        with db.connect() as conn:
            conn.execute("INSERT INTO sessions (id,guild_id,channel_id,starts_at,created_by) VALUES (9,'10','20',?,'daily')",(self.record['starts_at'],))
            conn.execute("INSERT INTO session_history VALUES (9,'10',?,'completed',?,'[]',?,'99')",(1800009000,self.record['match_ids'],self.record['players']))
        interaction=SimpleNamespace(guild_id=10,user=SimpleNamespace(id=42),response=SimpleNamespace(send_message=AsyncMock()))
        await reports.SessionReportsCog(SimpleNamespace()).show_history(interaction)
        embed=interaction.response.send_message.call_args.kwargs['embed']
        self.assertIn('2 actual players',embed.fields[0].value)

    async def test_match_loading_batches_by_club_and_ignores_duplicate_history_ids(self):
        self.populate(count=30)
        ids=json.loads(self.record['match_ids'])
        self.record['match_ids']=json.dumps([*ids,ids[0],'missing'])
        original=db.connect
        from contextlib import contextmanager
        statements=[]
        @contextmanager
        def traced():
            with original() as conn:
                conn.set_trace_callback(statements.append)
                yield conn
        with patch.object(reports,'connect',traced):
            rows=reports.session_matches(self.record)
        self.assertEqual(len(rows),60)
        selects=[s for s in statements if s.startswith('SELECT * FROM matches')]
        self.assertEqual(len(selects),2)
        self.assertEqual(rows,sorted(rows,key=lambda r:(r['ts'],r['club_id'],r['match_id'])))

    async def test_expired_details_disable_controls_using_latest_click(self):
        self.populate()
        view=reports.DetailsView(self.record,42)
        original=SimpleNamespace(edit_original_response=AsyncMock())
        latest=SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()),edit_original_response=AsyncMock())
        view.anchor=original
        await view.move(latest,1)
        await view.on_timeout()
        self.assertTrue(all(c.disabled for c in view.children))
        original.edit_original_response.assert_not_awaited()
        latest.edit_original_response.assert_awaited_once()
        self.assertIn('expired',latest.edit_original_response.call_args.kwargs['content'])
        self.assertIsNone(reports.SummaryView().timeout)

    async def test_history_replacement_stops_old_timeout(self):
        record={**self.record,'rsvps':'[]'}
        view=reports.HistoryView(42,'10',[record])
        interaction=SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
        with patch.object(reports,'history',return_value=[record]):
            await view.paginate(interaction,20)
        self.assertTrue(view.is_finished())
        replacement=interaction.response.edit_message.call_args.kwargs['view']
        self.assertIs(replacement.anchor,interaction)
        self.assertFalse(replacement.is_finished())

    async def test_expired_token_does_not_break_timeout_cleanup(self):
        import discord
        view=reports.DetailsView(self.record,42)
        error=discord.NotFound(SimpleNamespace(status=404,reason='Not Found'), 'Expired token')
        view.anchor=SimpleNamespace(edit_original_response=AsyncMock(side_effect=error))
        await view.on_timeout()
        self.assertTrue(all(c.disabled for c in view.children))
