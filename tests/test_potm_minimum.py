"""Production Player of the Month minimum: 31 games in the calendar month."""
import awards as A
from clubs import monitored_clubs
from config import CLUB_ID
from tests.test_squad_features import Base, SYD, ts


class PlayerOfMonthMinimumTests(Base):
    async def test_30_is_ineligible_and_31_across_both_clubs_qualifies(self):
        secondary=next(c['club_id'] for c in monitored_clubs() if c['club_id'] != CLUB_ID)
        for i in range(30):
            self.game(ts(2026,10,3)+i*600,players=[('Eligible',{'r':8}),('Thirty games',{'r':9.9})],
                      club=CLUB_ID if i<16 else secondary)
        result=A.compute_combined(2026,10,SYD)
        self.assertEqual(result['min_games'],31)
        self.assertNotIn('potm',result)
        self.game(ts(2026,11,1),players=[('Thirty games',{'r':10})]) # next month doesn't count
        self.assertNotIn('potm',A.compute_combined(2026,10,SYD))
        self.game(ts(2026,10,4),players=[('Eligible',{'r':8})],club=secondary)
        result=A.compute_combined(2026,10,SYD)
        self.assertEqual(result['potm']['name'],'Eligible')
        self.assertEqual(result['potm']['games'],31)

    async def test_minimum_is_fixed_not_a_percentage_of_squad_games(self):
        for i in range(201):
            players=[('Regular',{'r':7})]
            if i<31:
                players.append(('Eligible',{'r':8}))
            self.game(ts(2026,10,3)+i*600,players=players)
        result=A.compute_combined(2026,10,SYD)
        self.assertEqual(result['record']['games'],201)
        self.assertEqual(result['min_games'],31)
        self.assertEqual(result['potm']['name'],'Eligible')
