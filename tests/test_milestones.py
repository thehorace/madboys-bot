"""Milestone announcements require newly detected, club-specific attendance."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import clubs
import db
import match_data as md
from cogs.matchday import MatchdayCog
from cogs.link import set_link


class MilestoneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        database = patch.object(db, 'DB_PATH', str(Path(temp.name) / 'milestones.db'))
        database.start()
        self.addCleanup(database.stop)
        db.init_all()
        self.primary, self.secondary = clubs.monitored_clubs()[:2]
        self.guild = SimpleNamespace(id=10)
        self.channel = SimpleNamespace(send=AsyncMock())
        self.cog = object.__new__(MatchdayCog)
        self.cog.ea = SimpleNamespace(get_member_stats=AsyncMock())
        self.cog._channel = AsyncMock(return_value=self.channel)
        db.set_setting('10', 'matchday_channel', '20')
        set_link('10', '42', 'Connor', 'self')

    def baseline(self, club, name, goals=49):
        with db.connect() as conn:
            conn.execute('INSERT INTO stat_snapshots (club_id,player_name,stat,value) VALUES (?,?,?,?)',
                         (club['club_id'], name, 'goals', goals))

    def game(self, club, mid, ts, names, opponent='Rivals'):
        return md.ParsedMatch(mid, 'leagueMatch', ts, 2, 1, '99', opponent,
                              players=[md.PlayerLine(str(i), n, 'forward') for i,n in enumerate(names)],
                              club_id=club['club_id'])

    async def check(self, club, members, matches=None, announce=True):
        self.cog.ea.get_member_stats.return_value=members
        with clubs.club_scope(club):
            await self.cog.check_milestones(self.guild, announce=announce, matches=matches)

    async def test_absent_connor_is_silent_and_old_increase_is_not_replayed(self):
        self.baseline(self.primary, 'Connor')
        members=[{'name':'Connor', 'goals':50}]
        await self.check(self.primary, members, [self.game(self.primary, 'a',100,['Other'])])
        self.channel.send.assert_not_awaited()
        with db.connect() as conn:
            value=conn.execute("SELECT value FROM stat_snapshots WHERE club_id=? AND player_name='Connor' AND stat='goals'",(self.primary['club_id'],)).fetchone()[0]
        self.assertEqual(value,50)
        await self.check(self.primary, members, [self.game(self.primary,'b',200,['Connor'])])
        self.channel.send.assert_not_awaited()

    async def test_present_player_is_linked_and_message_identifies_game(self):
        self.baseline(self.primary,'Connor')
        await self.check(self.primary,[{'name':'Connor','goals':50}], [self.game(self.primary,'match-123',123,['CONNOR'],'Big Birds FC')])
        text=self.channel.send.call_args.args[0]
        self.assertIn('<@42>',text)
        self.assertIn(self.primary['name'],text)
        self.assertIn('50 career goals',text)
        self.assertIn('2–1 vs Big Birds FC',text)
        self.assertIn('Match match-123',text)
        self.assertIn('<t:123:R>',text)

    async def test_other_club_appearance_does_not_qualify(self):
        self.baseline(self.primary,'Connor')
        self.baseline(self.secondary,'Connor')
        gray_game=self.game(self.secondary,'gray',200,['Connor'])
        await self.check(self.primary,[{'name':'Connor','goals':50}],[gray_game])
        self.channel.send.assert_not_awaited()
        await self.check(self.secondary,[{'name':'Connor','goals':50}],[gray_game])
        self.channel.send.assert_awaited_once()
        text=self.channel.send.call_args.args[0]
        self.assertIn(self.secondary['name'],text)
        self.assertNotIn(self.primary['name'],text)
        self.cog.ea.get_member_stats.assert_awaited_with(self.secondary['club_id'],career=True,bypass_cache=True)

    async def test_catchup_uses_each_players_latest_game_not_last_club_game(self):
        self.baseline(self.primary,'Connor')
        games=[self.game(self.primary,'latest-club',300,['Other'],'Other opponent'),
               self.game(self.primary,'old',100,['Connor'],'Old opponent'),
               self.game(self.primary,'latest-connor',200,['Connor'],'Connors opponent')]
        await self.check(self.primary,[{'name':'Connor','goals':50}],games)
        text=self.channel.send.call_args.args[0]
        self.assertIn('Match latest-connor',text)
        self.assertIn('Connors opponent',text)
        self.assertNotIn('Other opponent',text)
        self.assertNotIn('Old opponent',text)

    async def test_no_matches_backfill_and_new_baseline_are_quiet(self):
        self.baseline(self.primary,'Connor')
        await self.check(self.primary,[{'name':'Connor','goals':50}])
        self.channel.send.assert_not_awaited()
        game=self.game(self.primary,'a',100,['Connor','New player'])
        await self.check(self.primary,[{'name':'Connor','goals':75}], [game],announce=False)
        await self.check(self.primary,[{'name':'New player','goals':100}],[game])
        self.channel.send.assert_not_awaited()

    async def test_unchanged_totals_do_not_repeat_announcement(self):
        self.baseline(self.primary,'Connor')
        members=[{'name':'Connor','goals':50}]
        await self.check(self.primary,members,[self.game(self.primary,'a',100,['Connor'])])
        await self.check(self.primary,members,[self.game(self.primary,'b',200,['Connor'])])
        self.channel.send.assert_awaited_once()

    async def test_missing_ea_total_does_not_reset_baseline_and_create_false_award(self):
        self.baseline(self.primary,'Connor',50)
        games=[self.game(self.primary,'a',100,['Connor'])]
        await self.check(self.primary,[{'name':'Connor'}],games)
        await self.check(self.primary,[{'name':'Connor','goals':50}],games)
        self.channel.send.assert_not_awaited()
