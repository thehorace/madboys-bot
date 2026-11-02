"""Weekly bounds, combined identities, accurate form labels and delivery retries."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import db
from clubs import monitored_clubs, club_scope
from config import CLUB_ID
from cogs import matchday
from tests.test_squad_features import Base


class WeeklyRecapTests(Base):
    def populate(self):
        self.end=datetime(2026,11,1,21,tzinfo=timezone.utc)
        self.start=self.end-timedelta(days=7)
        self.primary,self.secondary=monitored_clubs()[:2]
        for i in range(16):
            self.game(int(self.start.timestamp())+i*600,'W',players=[('Shared',{'g':1,'a':1,'r':8})],club=CLUB_ID)
        self.game(int(self.end.timestamp())-600,'L',players=[('shared',{'g':2,'a':2,'r':6})],gf=2,ga=3,club=self.secondary['club_id'])
        self.game(int(self.start.timestamp())-1,players=[('Outside',{'g':99})])
        self.game(int(self.end.timestamp()),players=[('Boundary',{'g':99})])

    async def test_combined_week_has_exact_bounds_weighted_ratings_and_labelled_form(self):
        self.populate()
        # A duplicate persona must not double this player's goals or appearances.
        with db.connect() as conn:
            conn.execute("INSERT INTO match_players (club_id,match_id,persona_id,name,goals,rating) VALUES (?,'m1','zz','SHARED',99,10)",(CLUB_ID,))
        embed=matchday.build_recap_embed(end=self.end,combined=True)
        self.assertIn('17 matches',embed.description)
        self.assertIn('16 wins · 0 draws · 1 loss',embed.description)
        self.assertIn(str(int(self.start.timestamp())),embed.description)
        self.assertIn(str(int(self.end.timestamp())),embed.description)
        fields={f.name:f.value for f in embed.fields}
        self.assertIn('18 goals',fields['⚽ Top scorer'])
        self.assertIn('18 assists',fields['🅰️ Most assists'])
        self.assertIn('7.88 over 17 rated games',fields['📈 Best average rating'])
        self.assertIn('Last 15 results',fields[self.primary['name']])
        self.assertIn('Last 1 result',fields[self.secondary['name']])
        self.assertNotIn('Outside',str(embed.to_dict()))
        self.assertNotIn('Boundary',str(embed.to_dict()))
        self.assertIn(self.primary['name'],embed.title)
        self.assertIn(self.secondary['name'],embed.title)

    async def test_manual_recap_still_respects_selected_club(self):
        self.populate()
        with club_scope(self.secondary):
            embed=matchday.build_recap_embed(end=self.end)
        self.assertIn('1 match',embed.description)
        self.assertNotIn(self.primary['name'],embed.title)

    async def test_weekly_delivery_is_once_and_existing_legacy_post_is_not_repeated(self):
        self.populate()
        channel=SimpleNamespace(send=AsyncMock())
        cog=object.__new__(matchday.MatchdayCog)
        cog.home_guild=lambda:SimpleNamespace(id=10)
        cog.posting_enabled=lambda _:True
        cog.channel_id_for=lambda _:20
        cog._channel=AsyncMock(return_value=channel)
        with patch.object(matchday,'datetime') as clock, patch.object(matchday,'RECAP_WEEKDAY',6), patch.object(matchday,'RECAP_HOUR',21):
            clock.now.return_value=self.end+timedelta(minutes=4)
            await matchday.MatchdayCog.recap_loop.coro(cog)
            await matchday.MatchdayCog.recap_loop.coro(cog)
        channel.send.assert_awaited_once()
        self.assertFalse(channel.send.call_args.kwargs['allowed_mentions'].everyone)
        db.set_setting('10',f'{matchday.K_LAST_RECAP}:combined',None)
        db.set_setting('10',matchday.K_LAST_RECAP,f'{self.end.isocalendar().year}-W{self.end.isocalendar().week}')
        with patch.object(matchday,'datetime') as clock, patch.object(matchday,'RECAP_WEEKDAY',6), patch.object(matchday,'RECAP_HOUR',21):
            clock.now.return_value=self.end
            await matchday.MatchdayCog.recap_loop.coro(cog)
        channel.send.assert_awaited_once()

    async def test_failed_weekly_send_does_not_mark_week_delivered(self):
        import discord
        self.populate()
        channel=SimpleNamespace(send=AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=503,reason='Unavailable'),'try later')))
        cog=object.__new__(matchday.MatchdayCog)
        cog.home_guild=lambda:SimpleNamespace(id=10)
        cog.posting_enabled=lambda _:True
        cog.channel_id_for=lambda _:20
        cog._channel=AsyncMock(return_value=channel)
        with patch.object(matchday,'datetime') as clock, patch.object(matchday,'RECAP_WEEKDAY',6), patch.object(matchday,'RECAP_HOUR',21):
            clock.now.return_value=self.end
            await matchday.MatchdayCog.recap_loop.coro(cog)
            self.assertIsNone(db.get_setting('10',f'{matchday.K_LAST_RECAP}:combined'))
            channel.send.side_effect=None
            await matchday.MatchdayCog.recap_loop.coro(cog)
        self.assertEqual(channel.send.await_count,2)
        self.assertIsNotNone(db.get_setting('10',f'{matchday.K_LAST_RECAP}:combined'))
