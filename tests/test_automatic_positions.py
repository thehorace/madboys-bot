"""Position provenance, temporal defaults, quiet tracking and scoped corrections."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import discord
import db
import match_data as md
import positions as P
from clubs import club_scope,monitored_clubs
from config import CLUB_ID
from cogs.link import set_link
from cogs import matchday,rotation
from cogs.positions import PositionsCog
from cogs.position_menu import PositionView
from cogs.hub import StatsMenu
from tests.test_squad_features import Base

T=1800000000

class AutomaticPositionTests(Base):
    def setUp(self):
        super().setUp()
        set_link('10','42','Ali','self')
        self.secondary=next(c for c in monitored_clubs() if c['club_id']!=CLUB_ID)
        self.tracker=object.__new__(matchday.MatchdayCog)

    def game_position(self,mid,pos,when=T,cid=CLUB_ID,track=True,name='Ali',**extra):
        raw={'matchId':mid,'timestamp':when,'clubs':{str(cid):{'goals':2},'999':{'goals':1,'details':{'name':'Rivals'}}},
             'players':{str(cid):{'p':{'playername':name,'pos':pos,'goals':1,'rating':8,**extra}}}}
        match=md.parse_match(raw,cid)
        md.store_match(cid,match,raw)
        if track:
            with club_scope(cid), db.connect() as conn:
                P.track_matches(conn, '10', [match])
        return match

    def records(self):
        with db.connect() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM rotation_log ORDER BY logged_at,id')]

    async def test_explicit_ea_match_values_are_verified_and_broad_roles_are_not_exact(self):
        for i,(raw,role,exact) in enumerate([('ST','forward','ST'),('CAM','midfielder','CAM'),('CB','defender','CB'),('goalkeeper','goalkeeper','GK'),('defender','defender',None)]):
            match=self.game_position(str(i),raw,T+i)
            self.assertEqual((match.players[0].pos,match.players[0].exact_position),(role,exact))
            with db.connect() as conn:
                row=conn.execute('SELECT pos,ea_exact_position FROM match_players WHERE match_id=?',(str(i),)).fetchone()
            self.assertEqual(tuple(row),(role,exact))
            record=self.records()[-1]
            self.assertEqual(record['source'],'ea' if exact else 'unknown')
            self.assertEqual(record['position'],exact or 'Unknown')

    async def test_unrelated_or_invalid_values_never_become_actual_positions(self):
        for i,value in enumerate([None,13,'13','not-a-position',{'code':'ST'}]):
            match=self.game_position(str(i),value,T+i,favoritePosition='ST',archetypeid='22')
            self.assertIsNone(match.players[0].exact_position)
            self.assertEqual(self.records()[-1]['position'],'Unknown')
            self.assertFalse(rotation.is_exact(self.records()[-1]['position']))
        self.assertEqual(rotation.current_streak(['Unknown']*5),(None,0))

    async def test_saved_positions_are_per_club_and_api_exact_wins(self):
        P.save_default('10','42','ST',when=T-10)
        P.save_default('10','42','CB',cid=self.secondary['club_id'],when=T-10)
        self.game_position('same','forward')
        self.game_position('same','defender',cid=self.secondary['club_id'])
        self.game_position('changed','CAM',T+100)
        rows=self.records()
        self.assertEqual([(r['position'],r['source']) for r in rows],[('ST','saved'),('CB','saved'),('CAM','ea')])
        self.assertEqual(rows[1]['club'],self.secondary['name'])

    async def test_conflicting_role_is_unknown_and_does_not_block_statistics(self):
        P.save_default('10','42','ST',when=T-10)
        self.game_position('def','defender')
        row=self.records()[0]
        self.assertEqual((row['position'],row['source'],row['ea_role']),('Unknown','unknown','DEF'))
        self.assertEqual(md.player_totals(CLUB_ID)[0]['goals'],1)
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT pos FROM match_players').fetchone()[0],'defender')

    async def test_changes_and_late_detection_do_not_rewrite_earlier_games(self):
        P.save_default('10','42','ST',when=T-10)
        first=self.game_position('1','forward')
        P.save_default('10','42','CAM',when=T+1200)
        self.game_position('3','midfielder',T+1500)
        self.game_position('2','forward',T+600)  # discovered late, uses the default effective then
        P.save_default('10','42','ST',when=T+2000)
        self.game_position('4','forward',T+2400)
        self.assertEqual([r['position'] for r in self.records()],['ST','ST','CAM','ST'])
        with db.connect() as conn:
            self.assertEqual(P.track_matches(conn, '10', [first]), set())
        self.assertEqual(len(self.records()),4)
        self.assertEqual(rotation.get_position_counts('10',self.records()[0]['club'])['42'],{'CAM':1,'ST':3})

    async def test_last_confirmation_is_inferred_and_unconfirmed_history_is_not_reused(self):
        self.game_position('old','defender',T-5*86400)
        with db.connect() as conn:
            conn.execute("UPDATE rotation_log SET position='CB',source='auto'")
        self.assertEqual(P.detect_position('10','42','defender',T).position,'Unknown')
        with db.connect() as conn:
            conn.execute("UPDATE rotation_log SET source='player'")
        result=P.detect_position('10','42','defender',T)
        self.assertEqual((result.position,result.source),('CB','confirmed_default'))
        self.assertEqual(P.detect_position('10','42','forward',T).position,'Unknown')
        self.assertEqual(P.detect_position('10','42','defender',T-6*86400).position,'Unknown')
        self.game_position('switched','CAM',T-100)
        self.assertEqual(P.detect_position('10','42','defender',T).position,'Unknown')

    async def test_planned_lineup_is_not_verified_match_evidence(self):
        with patch.object(P.time,'time',return_value=T-100):
            P.save_plan('10','formation',{'CB':'42'},'manager')
        self.game_position('m','defender')
        self.assertEqual(self.records()[0]['position'],'Unknown')

    async def test_clear_default_and_missing_match_time_do_not_invent_a_position(self):
        P.save_default('10','42','ST',when=T-100)
        P.save_default('10','42',None,when=T-10)
        self.assertEqual(P.detect_position('10','42','forward',T).position,'Unknown')
        P.save_default('10','42','ST',when=T+10)
        self.assertEqual(P.detect_position('10','42',None,0).position,'Unknown')
        self.assertEqual(P.detect_position('10','42',None,T+20).source,'saved')

    async def test_correction_only_updates_the_requested_club_game_and_player(self):
        P.save_default('10','42','ST',when=T-10)
        self.game_position('same','forward')
        self.game_position('same','defender',cid=self.secondary['club_id'])
        self.game_position('other','midfielder',T+100,cid=self.secondary['club_id'])
        with club_scope(self.secondary):
            P.correct_position('10','42','CB',f"{self.secondary['club_id']}:same")
        rows=self.records()
        self.assertEqual([(r['position'],r['source']) for r in rows],[('ST','saved'),('CB','player'),('Unknown','unknown')])
        self.assertEqual(P.default_event('10','42',T)['position'],'ST')
        self.assertEqual(md.player_totals(self.secondary['club_id'])[0]['games'],2)
        self.game_position('not-mine','ST',T+200,name='Bob')
        with self.assertRaises(ValueError):
            P.correct_position('10','42','CB','not-mine')

    async def test_migration_preserves_build_preferences_and_all_old_history(self):
        from cogs.lineup import set_prefs,get_prefs
        set_prefs('10','42',['ST','CB'])
        self.game_position('old','defender')
        before=[{k:v for k,v in r.items() if k!='ea_role'} for r in self.records()]
        with db.connect() as conn:
            conn.execute('ALTER TABLE rotation_log DROP COLUMN ea_role')
            conn.execute('ALTER TABLE match_players DROP COLUMN ea_exact_position')
            conn.execute('DROP TABLE position_defaults')
        db.init_all()
        db.init_all()
        self.assertEqual(before,[{k:v for k,v in r.items() if k!='ea_role'} for r in self.records()])
        self.assertEqual(get_prefs('10','42'),['ST','CB'])
        self.assertIsNone(P.default_event('10','42'))

    async def test_result_posts_never_request_a_position(self):
        match=self.game_position('m','forward')
        prompt=SimpleNamespace(open_prompt=AsyncMock())
        self.tracker.bot=SimpleNamespace(get_cog=lambda name:prompt if name=='PositionsCog' else None)
        channel=SimpleNamespace(send=AsyncMock())
        with patch.object(md,'match_post',AsyncMock(return_value=(discord.Embed(),None))):
            await self.tracker._post_results(channel,[match],'10',{'m':{'42':'forward'}})
        channel.send.assert_awaited_once()
        prompt.open_prompt.assert_not_awaited()

    async def test_command_saves_only_future_defaults_and_can_open_without_a_position(self):
        cog=PositionsCog(SimpleNamespace())
        self.game_position('m','forward')
        interaction=SimpleNamespace(guild_id=10,guild=SimpleNamespace(id=10),user=SimpleNamespace(id=42),extras={},
            response=SimpleNamespace(send_message=AsyncMock(),defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(P.time,'time',return_value=T+100):
            await PositionsCog.position.callback(cog,interaction,SimpleNamespace(value='ST'))
            self.assertEqual(self.records()[0]['position'],'Unknown')
            self.assertTrue(interaction.response.send_message.call_args.kwargs['ephemeral'])
            await PositionsCog.position.callback(cog,interaction)
        self.assertIsInstance(interaction.followup.send.call_args.kwargs['view'],PositionView)

    async def test_position_menu_is_private_and_retains_separate_club_preferences(self):
        user=SimpleNamespace(id=42)
        view=PositionView(SimpleNamespace(),user,'10',CLUB_ID)
        interaction=SimpleNamespace(guild_id=10,user=SimpleNamespace(id=99),data={'values':['ST']},
            response=SimpleNamespace(send_message=AsyncMock(),edit_message=AsyncMock()))
        await view.choose(interaction)
        self.assertIsNone(P.default_event('10','42'))
        interaction.user=user
        await view.choose(interaction)
        interaction.data={'values':[str(self.secondary['club_id'])]}
        await view.choose_club(interaction)
        interaction.data={'values':['CB']}
        await view.choose(interaction)
        self.assertEqual(P.default_event('10','42')['position'],'ST')
        self.assertEqual(P.default_event('10','42',cid=self.secondary['club_id'])['position'],'CB')
        self.assertTrue(all(len(c.options)<=25 for c in view.children if isinstance(c,discord.ui.Select)))

    async def test_profile_has_change_position_and_source_without_losing_other_buttons(self):
        self.game_position('m','CB')
        ea=SimpleNamespace(get_member_stats=AsyncMock(return_value=[{'name':'Ali','gamesPlayed':1}]),stale_note=lambda:None)
        menu=StatsMenu(SimpleNamespace(ea=ea,get_guild=lambda _:None),SimpleNamespace(id=42),'10',['Ali'],[])
        await menu.go('me')
        labels=[getattr(c,'label','') for c in menu.children]
        self.assertIn('Change position',labels)
        self.assertIn('My badges',labels)
        self.assertTrue(any(f.name=='📍 Position' and 'EA match' in f.value for f in menu.embed.fields))
