"""Role-aware style matching, truthful evidence, privacy and shareable cards."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import discord

import player_twin as T
import db
from clubs import monitored_clubs
from config import CLUB_ID
from cogs.insights import InsightsCog, build_player_twin
from cogs.link import set_link
from tests.test_squad_features import Base, ts


class PlayerTwinTests(Base):
    def appearances(self,role,count=5,goals=0,assists=0,passes=0,attempts=0,tackles=0,saves=0,club=CLUB_ID,name='Ali',offset=0):
        mids=[]
        for i in range(count):
            mid=self.game(ts(2026,10,3)+offset+i*600,players=[(name,{'pos':role,'g':goals,'a':assists,'sv':saves})],club=club)
            with db.connect() as conn:
                conn.execute('UPDATE match_players SET passes_made=?,pass_attempts=?,tackles_made=? WHERE club_id=? AND match_id=?',
                             (passes,attempts,tackles,club,mid))
            mids.append(mid)
        return mids

    async def test_goal_focused_forward_and_creator_have_different_twins(self):
        self.appearances('forward',goals=2,passes=5,attempts=7,name='Finisher')
        self.appearances('forward',goals=1,assists=1,passes=25,attempts=30,name='Creator')
        self.assertEqual(T.analyse('Finisher')['matches'][0][0],'Erling Haaland')
        self.assertEqual(T.analyse('Creator')['matches'][0][0],'Harry Kane')

    async def test_mbappe_pedri_and_bellingham_can_each_be_the_closest_twin(self):
        cases=[('Mbappe sample','forward',1,14,17,0,'Kylian Mbappé'),
               ('Pedri sample','midfielder',0,33,35,1,'Pedri'),
               ('Bellingham sample','midfielder',1,23,28,3,'Jude Bellingham')]
        for name,role,goals,passes,attempts,tackles,expected in cases:
            mids=self.appearances(role,goals=goals,passes=passes,attempts=attempts,tackles=tackles,name=name)
            with db.connect() as conn:
                if expected=='Kylian Mbappé':
                    conn.execute('UPDATE match_players SET goals=2 WHERE club_id=? AND match_id=?',(CLUB_ID,mids[0]))
                if expected=='Pedri':
                    conn.execute('UPDATE match_players SET goals=1 WHERE club_id=? AND match_id=?',(CLUB_ID,mids[0]))
                for mid in mids[:3 if expected=='Jude Bellingham' else 2]:
                    conn.execute('UPDATE match_players SET assists=1 WHERE club_id=? AND match_id=?',(CLUB_ID,mid))
            result=T.analyse(name)
            self.assertEqual(result['matches'][0][0],expected)
            self.assertIn(expected,T.build_embed(result).description)
            self.assertTrue(T.render_card(result).startswith(b'\x89PNG'))

    async def test_expanded_catalog_is_role_consistent_and_has_unique_real_names(self):
        profiles=[p for group in T.PROFILES.values() for p in group]
        self.assertEqual(len(profiles),64)
        self.assertEqual(len({p[0] for p in profiles}),64)
        for role,group in T.PROFILES.items():
            keys=set(group[0][3])
            self.assertTrue(all(set(p[3])==keys for p in group))
            self.assertTrue(all(p[4].startswith('https://') for p in group))

    async def test_every_new_profile_can_win_from_its_own_recorded_pattern(self):
        from twin_roster import MORE_PROFILES
        for role,profiles in MORE_PROFILES.items():
            for name,_,_,targets,_ in profiles:
                with self.subTest(player=name):
                    stats={'goals':targets.get('goals',0)*1.5,'creation':targets.get('creation',0),
                           'circulation':targets['circulation']*(25 if role=='DEF' else 35),'defence':targets.get('defence',0)*(2 if role=='DEF' else 5),
                           'saving':targets.get('saving',0)*6}
                    for i in range(5):
                        mid=self.game(ts(2026,10,3)+i,players=[(name,{'pos':role})])
                        with db.connect() as conn:
                            conn.execute('UPDATE match_players SET goals=?,assists=?,passes_made=?,pass_attempts=?,tackles_made=?,saves=? WHERE match_id=?',
                                         (stats['goals'],stats['creation'],stats['circulation'],stats['circulation']/(.6+.35*targets['precision']),stats['defence'],stats['saving'],mid))
                    self.assertEqual(T.analyse(name)['matches'][0][0],name)

    async def test_midfield_and_defensive_styles(self):
        self.appearances('midfielder',assists=1,passes=20,attempts=25,tackles=1,name='Creator')
        self.appearances('midfielder',passes=40,attempts=43,tackles=4,name='Controller')
        self.appearances('defender',passes=10,attempts=15,tackles=5,name='Stopper')
        self.appearances('defender',assists=1,passes=28,attempts=32,tackles=2,name='Supplier')
        for name,expected in [('Creator','David Beckham'),('Controller','Rodri'),('Stopper','Nemanja Vidić'),('Supplier','Andy Robertson')]:
            self.assertEqual(T.analyse(name)['matches'][0][0],expected)

    async def test_identical_defenders_receive_distinct_stable_twins(self):
        for name in ['CB One','CB Two','CB Three','CB Four']:
            self.appearances('defender',passes=8,attempts=12,tackles=1,name=name)
        names=['CB One','CB Two','CB Three','CB Four']
        first={n:T.analyse(n)['matches'][0][0] for n in names}
        second={n:T.analyse(n)['matches'][0][0] for n in reversed(names)}
        self.assertEqual(first,second)
        self.assertEqual(len(set(first.values())),4)
        self.assertLessEqual(list(first.values()).count('Harry Maguire'),1)
        self.assertTrue(all(p in {p[0] for p in T.PROFILES['DEF']} for p in first.values()))
        embed=T.build_embed(T.analyse('CB One'))
        self.assertTrue(any(f.name=='Unique squad twin' for f in embed.fields))

    async def test_fauz_mbappe_is_reserved_with_standard_presentation(self):
        self.appearances('defender',passes=20,tackles=1,name='FAUZAN0410')
        self.appearances('forward',goals=1,passes=14,assists=1,name='Attacker')
        result=T.analyse('fauzan0410')
        self.assertEqual(result['matches'][0][0],'Kylian Mbappé')
        self.assertEqual(result['role'],'DEF')
        self.assertEqual(result['stats']['goals'],0)
        self.assertEqual(result['assignment'],'Unique squad twin')
        self.assertNotEqual(T.analyse('Attacker')['matches'][0][0],'Kylian Mbappé')
        embed=T.build_embed(result)
        self.assertIn('your squad style twin',embed.description)
        self.assertNotIn('club pick',str(embed.to_dict()).lower())
        self.assertNotIn('by request',str(embed.to_dict()).lower())
        self.assertTrue(T.render_card(result).startswith(b'\x89PNG'))

    async def test_same_ea_identity_across_clubs_uses_one_assignment(self):
        secondary=next(c['club_id'] for c in monitored_clubs() if c['club_id']!=CLUB_ID)
        self.appearances('defender',passes=10,tackles=1,name='Shared')
        self.appearances('defender',passes=10,tackles=1,name='shared',club=secondary,offset=10000)
        self.appearances('defender',passes=10,tackles=1,name='Other')
        result=T.analyse('Shared')
        self.assertEqual(result['games'],10)
        self.assertEqual(result['matches'][0][0],T.analyse('SHARED')['matches'][0][0])
        self.assertNotEqual(result['matches'][0][0],T.analyse('Other')['matches'][0][0])

    async def test_exhausted_role_pool_does_not_silently_duplicate_twins(self):
        for i in range(len(T.PROFILES['GK'])+1):
            self.appearances('goalkeeper',saves=2,name=f'Keeper {i}')
        results=[T.analyse(f'Keeper {i}') for i in range(len(T.PROFILES['GK'])+1)]
        assigned=[r['matches'][0][0] for r in results if isinstance(r,dict)]
        self.assertEqual(len(assigned),len(set(assigned)))
        self.assertEqual(len(assigned),len(T.PROFILES['GK']))
        self.assertTrue(any(isinstance(r,str) and 'already assigned' in r for r in results))

    async def test_defence_weights_do_not_treat_one_tackle_as_a_poor_spell(self):
        self.appearances('defender',passes=20,attempts=22,tackles=1)
        result=T.analyse('Ali')
        self.assertNotEqual(result['matches'][0][0],'Harry Maguire')
        self.assertIn('Solid spell',result['form'][0])

    async def test_keeper_only_matches_keeper_profiles(self):
        self.appearances('goalkeeper',saves=8,passes=3,attempts=4,name='Saves')
        self.appearances('goalkeeper',saves=3,passes=30,attempts=32,name='Passing')
        self.assertEqual(T.analyse('Saves')['matches'][0][0],'Thibaut Courtois')
        self.assertEqual(T.analyse('Passing')['matches'][0][0],'Marc-André ter Stegen')
        self.assertTrue(all(p in T.PROFILES['GK'] for p in T.analyse('Saves')['matches']))

    async def test_minimum_unknown_role_and_no_action_data(self):
        self.assertIsInstance(T.analyse(None),str)
        self.assertIsInstance(T.analyse('Missing'),str)
        self.appearances('forward',count=4,goals=2,name='New')
        self.assertIn('at least 5',T.analyse('New'))
        self.appearances('unknown',name='Unknown')
        self.assertIn('position data',T.analyse('Unknown'))
        self.appearances('forward',name='Quiet')
        self.assertIn('not enough recorded actions',T.analyse('Quiet'))

    async def test_recent_role_sample_does_not_mix_striker_stats_into_midfield(self):
        self.appearances('forward',count=4,goals=10)
        self.appearances('midfielder',count=5,assists=1,passes=25,attempts=30,offset=10000)
        result=T.analyse('ali')
        self.assertEqual(result['role'],'MID')
        self.assertEqual(result['games'],5)
        self.assertEqual(result['recent_games'],9)
        self.assertEqual(result['stats']['goals'],0)

    async def test_combines_clubs_but_not_other_players_and_caps_at_20(self):
        secondary=next(c['club_id'] for c in monitored_clubs() if c['club_id']!=CLUB_ID)
        self.appearances('forward',count=15,goals=1)
        self.appearances('forward',count=10,goals=1,club=secondary,offset=20000)
        self.appearances('forward',goals=99,name='Someone else')
        result=T.analyse('Ali')
        self.assertEqual(result['games'],20)
        self.assertEqual(result['stats']['goals'],1)
        self.assertEqual(result['confidence'],'More settled sample')

    async def test_duplicate_persona_does_not_inflate_appearances_or_stats(self):
        mids=self.appearances('forward',goals=1)
        with db.connect() as conn:
            conn.execute("INSERT INTO match_players (club_id,match_id,persona_id,name,pos,goals) VALUES (?,?,'zz','Ali','forward',99)",(CLUB_ID,mids[0]))
        result=T.analyse('Ali')
        self.assertEqual(result['games'],5)
        self.assertEqual(result['stats']['goals'],1)
        self.assertIsNone(result['stats']['precision'])

    async def test_card_embed_and_private_command(self):
        self.appearances('forward',goals=2)
        set_link('10','42','Ali','self')
        embed,png=await build_player_twin('Ali')
        self.assertTrue(png.startswith(b'\x89PNG'))
        self.assertLess(len(embed),6000)
        self.assertIn('not real-player stat matching',embed.fields[-1].value)
        self.assertIn('https://',embed.fields[-1].value)
        interaction=SimpleNamespace(guild_id=10,user=SimpleNamespace(id=42),extras={},response=SimpleNamespace(defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        bot=SimpleNamespace(ea=None,get_guild=lambda _:None)
        await InsightsCog.playertwin.callback(InsightsCog(bot),interaction)
        self.assertTrue(interaction.response.defer.call_args.kwargs['ephemeral'])
        self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])
        self.assertTrue(any(c.label=='Share' for c in interaction.followup.send.call_args.kwargs['view'].children if isinstance(c,discord.ui.Button)))

    async def test_menu_player_picker_and_discord_component_limits(self):
        from cogs.hub import StatsMenu,PanelView
        self.appearances('forward',goals=2)
        set_link('10','42','Ali','self')
        bot=SimpleNamespace(ea=SimpleNamespace(),get_guild=lambda _:SimpleNamespace(id=10))
        menu=StatsMenu(bot,SimpleNamespace(id=42),'10',['Ali','Other'],[])
        menu.stat='player_twin'
        await menu.go('leaderboard')
        self.assertTrue(menu.png.startswith(b'\x89PNG'))
        self.assertTrue(any(getattr(c,'placeholder','')=='🪞 Whose Player Twin…' for c in menu.children))
        self.assertTrue(all(len(c.options)<=25 for c in menu.children if isinstance(c,discord.ui.Select)))
        menu._update=AsyncMock()
        await menu._pick_squad_option(SimpleNamespace(data={'values':['Other']}))
        self.assertEqual(menu.twin_of,'Other')
        view=PanelView(bot)
        self.assertIsNone(view.timeout)
        self.assertTrue(any(c.custom_id=='madboys:panel:player-twin' for c in view.children))

    async def test_quiet_forward_has_non_star_pattern_and_honest_form(self):
        self.appearances('forward',passes=8,attempts=14,name='Quiet')
        with db.connect() as conn:
            conn.execute("UPDATE match_players SET rating=5.2,shots=4 WHERE name='Quiet'")
        result=T.analyse('Quiet')
        self.assertEqual(result['matches'][0][0],'Timo Werner')
        self.assertIn('Rough patch',result['form'][0])
        self.assertIn('Finishing has not clicked',result['form'][1])
        self.assertIn('0%',result['form'][1])
        self.assertTrue(T.render_card(result).startswith(b'\x89PNG'))

    async def test_supporting_roles_are_not_called_poor_for_no_goals(self):
        self.appearances('defender',passes=18,attempts=20,tackles=3)
        result=T.analyse('Ali')
        self.assertIn('Solid spell',result['form'][0])
        self.assertFalse(any('Finishing' in s for s in result['form']))
        with db.connect() as conn:
            conn.execute('UPDATE match_players SET rating=NULL')
        self.assertEqual(T.analyse('Ali')['form'],[])

    async def test_search_spans_saved_rosters_and_rejects_ambiguous_partials(self):
        secondary=next(c['club_id'] for c in monitored_clubs() if c['club_id']!=CLUB_ID)
        self.appearances('forward',goals=1,name='Alex One')
        self.appearances('forward',goals=2,name='Alex Two',club=secondary)
        with db.connect() as conn:
            conn.execute('UPDATE monitored_clubs SET enabled=0 WHERE club_id=?',(secondary,))
        self.assertEqual(T.resolve_name('TWO'),('Alex Two',[]))
        self.assertEqual(T.resolve_name('alex one'),('Alex One',[]))
        self.assertEqual(T.resolve_name('Alex'),(None,['Alex One','Alex Two']))
        self.assertEqual(T.resolve_name('missing'),(None,[]))
        from cogs.insights import twin_autocomplete
        choices=await twin_autocomplete(None,'Two')
        self.assertEqual([c.value for c in choices],['Alex Two'])

    async def test_other_player_command_has_share_and_keeps_card_snapshot(self):
        from cogs.hub import ShareChoice
        self.appearances('forward',goals=1,name='Other')
        bot=SimpleNamespace(ea=None,get_guild=lambda _:None)
        interaction=SimpleNamespace(guild_id=10,user=SimpleNamespace(id=42,mention='@me'),extras={},
            response=SimpleNamespace(defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        await InsightsCog.playertwin.callback(InsightsCog(bot),interaction,'oth')
        menu=interaction.followup.send.call_args.kwargs['view']
        self.assertEqual(menu.twin_of,'Other')
        self.assertIn('Other',menu.embed.description)
        target=SimpleNamespace(id=1,name='general',mention='#general',send=AsyncMock())
        choice=ShareChoice(menu,[target])
        original_embed,original_png=menu.embed,menu.png
        menu.embed=discord.Embed(title='Different screen')
        menu.png=None
        interaction.data={'values':['1']}
        interaction.edit_original_response=AsyncMock()
        await choice._pick(interaction)
        sent=target.send.call_args.kwargs
        self.assertIs(sent['embed'],original_embed)
        self.assertEqual(sent['file'].fp.getvalue(),original_png)
        self.assertFalse(sent['allowed_mentions'].everyone)

    async def test_search_modal_handles_names_beyond_dropdown_and_owner(self):
        from cogs.hub import StatsMenu,TwinSearch
        menu=StatsMenu(SimpleNamespace(ea=None),SimpleNamespace(id=42),'10',[],[])
        menu.twin_roster=[f'Player {n}' for n in range(40)]
        menu._update=AsyncMock()
        interaction=SimpleNamespace(user=SimpleNamespace(id=42),extras={},
                                    response=SimpleNamespace(send_message=AsyncMock()))
        modal=TwinSearch(menu)
        modal.name._value='Player 39'
        await modal.on_submit(interaction)
        self.assertEqual(menu.twin_of,'Player 39')
        menu._update.reset_mock()
        modal.name._value='Player'
        await modal.on_submit(interaction)
        menu._update.assert_not_awaited()
        self.assertIn('Several players',interaction.response.send_message.call_args.args[0])
        interaction.user.id=99
        modal.name._value='Player 12'
        await modal.on_submit(interaction)
        self.assertEqual(menu.twin_of,'Player 39')

    async def test_member_picker_requires_link_and_opens_selected_player(self):
        from cogs.hub import StatsMenu
        menu=StatsMenu(SimpleNamespace(ea=None),SimpleNamespace(id=42),'10',[],[])
        menu._update=AsyncMock()
        interaction=SimpleNamespace(data={'values':['99']},response=SimpleNamespace(send_message=AsyncMock()))
        await menu._pick_twin_member(interaction)
        menu._update.assert_not_awaited()
        self.assertIn('not linked',interaction.response.send_message.call_args.args[0])
        set_link('10','99','Other','self')
        await menu._pick_twin_member(interaction)
        self.assertEqual(menu.twin_of,'Other')
        menu._update.assert_awaited_once()
