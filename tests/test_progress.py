"""Progress windows, fairness, session-state accuracy and private UI routing."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import db
import clubs
import progress as P
from config import CLUB_ID
from cogs import progress as C
from cogs.hub import PanelView
from cogs.link import set_link
from match_data import VOID_SCHEMA
from tests.test_squad_features import Base

NOW=1_800_000_000


class WeeklyProgressTests(Base):
    async def test_per_match_comparison_normalizes_different_game_counts(self):
        for i in range(3):
            self.game(NOW-P.WEEK-100-i,players=[('Ali',{'g':1,'a':1,'r':7})])
        for i in range(6):
            self.game(NOW-100-i,players=[('Ali',{'g':1,'a':1,'r':8})])
        data=P.weekly_data(CLUB_ID,'Ali',NOW)
        self.assertEqual((data['current']['games'],data['previous']['games']),(6,3))
        text=P.weekly_embed(CLUB_ID,'Ali',NOW).fields[-1].value
        self.assertIn('Goals per match**: 1.00 → 1.00',text)
        self.assertIn('Average rating**: 7.00 → 8.00',text)
        self.assertIn('+1.00',text)

    async def test_window_boundaries_club_and_player_are_isolated(self):
        secondary=next(c['club_id'] for c in clubs.monitored_clubs() if c['club_id']!=CLUB_ID)
        self.game(NOW-P.WEEK,players=[('Ali',{'g':1}),('Bob',{'g':8})])
        self.game(NOW-2*P.WEEK,players=[('Ali',{'g':2})])
        self.game(NOW,players=[('Ali',{'g':99})])
        self.game(NOW-2*P.WEEK-1,players=[('Ali',{'g':99})])
        self.game(NOW-1,players=[('Ali',{'g':99})],club=secondary)
        result=P.weekly_data(CLUB_ID,'ali',NOW)
        self.assertEqual(result['current']['goals'],1)
        self.assertEqual(result['previous']['goals'],2)

    async def test_sparse_games_and_missing_ratings_do_not_claim_improvement(self):
        self.game(NOW-1,players=[('Ali',{'g':3})])
        embed=P.weekly_embed(CLUB_ID,'Ali',NOW)
        self.assertIn('Building your comparison',[f.name for f in embed.fields])
        for i in range(3):
            self.game(NOW-P.WEEK-100-i,players=[('Ali',{'r':None})])
            self.game(NOW-100-i,players=[('Ali',{'r':None})])
        embed=P.weekly_embed(CLUB_ID,'Ali',NOW)
        self.assertIn('Rating comparison',[f.name for f in embed.fields])
        changes=next(f.value for f in embed.fields if f.name=='What changed')
        self.assertNotIn('Average rating',changes)

    async def test_private_screen_and_player_club_pickers(self):
        self.game(NOW-1,players=[('Ali',{'g':1})])
        set_link('10','42','Ali','self')
        guild=SimpleNamespace(id=10)
        bot=SimpleNamespace(get_cog=lambda _:None)
        interaction=SimpleNamespace(guild=guild,guild_id=10,user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()),edit_original_response=AsyncMock())
        await C.ProgressCog(bot).open_weekly(interaction)
        self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])
        view=interaction.followup.send.call_args.kwargs['view']
        self.assertEqual(view.player,'Ali')
        interaction.data={'values':['club']}
        await view.pick_player(interaction)
        self.assertIsNone(view.player)
        secondary=next(c['club_id'] for c in clubs.monitored_clubs() if c['club_id']!=CLUB_ID)
        interaction.data={'values':[str(secondary)]}
        await view.pick_club(interaction)
        self.assertEqual(view.cid,secondary)
        self.assertIn(clubs.club_for(secondary)['name'],view.embed.title)
        interaction.user.id=99
        self.assertFalse(await view.interaction_check(interaction))


class SessionProgressTests(Base):
    def setUp(self):
        super().setUp()
        with db.connect() as conn:
            self.sid=conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by) VALUES ('10','20',?,'daily')",(NOW,)).lastrowid

    async def test_no_games_reports_waiting_without_creating_history(self):
        embed=P.session_embed('10',NOW+100,tracker_ok=True)
        self.assertIn('Waiting for the first recorded game',embed.description)
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM session_history').fetchone()[0],0)

    async def test_forfeit_extends_activity_but_not_results(self):
        self.game(NOW+600,players=[('Ali',{})])
        with db.connect() as conn:
            conn.executescript(VOID_SCHEMA)
            conn.execute("INSERT INTO void_matches VALUES (?, 'void', ?, 0,3,'Rivals','forfeit','{}')",(CLUB_ID,NOW+1800))
        embed=P.session_embed('10',NOW+2000,tracker_ok=True)
        self.assertIn(f'<t:{NOW+3600}:t>',embed.description)
        self.assertIn('1 counted matches',embed.fields[0].value)
        self.assertIn(str(NOW+1800),embed.fields[1].value)

    async def test_voice_hold_failed_check_and_stopped_tracker_explanations(self):
        self.game(NOW+600,players=[('Ali',{})])
        held=P.session_embed('10',NOW+2500,in_voice=True,tracker_ok=True)
        self.assertIn('Held open by voice',held.description)
        self.assertIn(str(NOW+600+7200),held.description)
        failed=P.session_embed('10',NOW+2500,tracker_ok=False)
        self.assertIn('Waiting for a successful EA check',failed.description)
        stopped=P.session_embed('10',NOW+2500,tracker_running=False)
        self.assertIn('unavailable or stopped',stopped.description)
        db.set_setting('10','reports:enabled','0')
        self.assertIn('Recap posting is off',[f.name for f in P.session_embed('10',NOW+2500).fields])

    async def test_finished_and_future_sessions_are_not_presented_as_active(self):
        with db.connect() as conn:
            conn.execute("INSERT INTO session_history VALUES (?,'10',?,'completed','[]','[]','[]','99')",(self.sid,NOW+100))
            conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by) VALUES ('10','20',?,'daily')",(NOW+86400,))
        embed=P.session_embed('10',NOW+200)
        self.assertIn('Next session:',embed.description)
        self.assertIn('completed',embed.description)
        self.assertNotIn('Waiting for the first',embed.description)

    async def test_panel_buttons_are_persistent_and_route_to_progress(self):
        cog=SimpleNamespace(open_weekly=AsyncMock(),open_session=AsyncMock())
        view=PanelView(SimpleNamespace(get_cog=lambda _:cog))
        self.assertIsNone(view.timeout)
        interaction=SimpleNamespace()
        # Call original handlers: tracking itself is covered by the interaction tests.
        await self._route(view,interaction)
        self.assertTrue(any(c.custom_id=='madboys:panel:weekly-progress' for c in view.children))

    async def _route(self,view,interaction):
        from unittest.mock import patch
        interaction.guild_id=10
        interaction.user=SimpleNamespace(id=42)
        interaction.extras={}
        with patch('interaction_tracking.record'):
            await view.weekly_progress.callback(interaction)
            await view.session_progress.callback(interaction)
        view.bot.get_cog('ProgressCog').open_weekly.assert_awaited_once_with(interaction)
        view.bot.get_cog('ProgressCog').open_session.assert_awaited_once_with(interaction)
