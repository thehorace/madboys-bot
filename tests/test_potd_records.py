"""Player of the Day (pick, card, posting) and club records (page + 'new record' posts)."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

import badges as B
import potd
import records as R
from config import CLUB_ID
from cogs.link import set_link
from db import connect, set_setting
from tests.test_squad_features import Base, ts


def rows(mids):
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM matches WHERE club_id=? AND match_id IN (SELECT value FROM json_each(?)) ORDER BY ts",
            (CLUB_ID, json.dumps(mids)))]


class PotdPickTests(Base):
    def setUp(self):
        super().setUp()
        potd.init_potd()

    async def test_needs_half_the_games_and_rewards_contributions(self):
        mids = [self.game(ts(2026, 10, 3, 20) + i * 1200, "W" if i < 3 else "L",
                          [("Ali", {"r": 8.0, "g": 1}), ("Bob", {"r": 7.6, "a": 1})]
                          + ([("Cat", {"r": 9.9, "g": 3})] if i == 0 else []))
                for i in range(4)]
        result = potd.compute(rows(mids))
        self.assertEqual(result["winner"]["name"], "Ali")          # Cat: 1 of 4 games isn't enough
        self.assertEqual(result["winner"]["goals"], 4)
        self.assertEqual([r["name"] for r in result["runners"]], ["Bob"])
        self.assertEqual(result["session"]["w"], 3)
        self.assertEqual(result["winner"]["best"]["opp"], "Rivals")
        self.assertTrue(potd.render_card(result).startswith(b"\x89PNG"))

    async def test_keeper_gets_clean_sheet_credit(self):
        mids = [self.game(ts(2026, 10, 3) + i * 1200, "W",
                          [("Keeper", {"r": 7.4, "pos": "goalkeeper", "sv": 4, "cs": 1}), ("Mid", {"r": 7.5})],
                          gf=1, ga=0) for i in range(2)]
        result = potd.compute(rows(mids))
        self.assertEqual(result["winner"]["name"], "Keeper")
        self.assertEqual(result["winner"]["clean_sheets"], 2)
        self.assertTrue(result["winner"]["keeper"])

    async def test_nothing_to_pick(self):
        self.assertIsNone(potd.compute([]))

    async def test_badge_and_leaders(self):
        set_link("10", "5", "Ali", "self")
        mid = self.game(ts(2026, 10, 3), "W", [("Ali", {"r": 8.0})])
        result = potd.compute(rows([mid]))
        potd.save("10", 1, result, "5", "99", 1000)
        potd.save("10", 2, result, "5", "98", 2000)
        self.assertEqual(potd.count_for("10", "ali"), 2)
        self.assertEqual(potd.leaders("10", CLUB_ID), [("Ali", 2)])
        self.assertEqual(B.evaluate("10", "Ali")["potd"]["count"], 2)
        self.assertTrue(potd.already_done(1))


class PotdPostTests(Base):
    async def test_card_posts_after_the_recap_once(self):
        import cogs.session_reports as reports
        set_link("10", "42", "Ali", "self")
        mid = self.game(ts(2026, 10, 3), "W", [("Ali", {"r": 8.5, "g": 2})])
        cog = reports.SessionReportsCog(SimpleNamespace(get_cog=lambda n: None))
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=77)))
        record = {"session_id": 5, "match_ids": json.dumps([mid])}
        guild = SimpleNamespace(id=10)
        with patch("cogs.session_reports.report_health", AsyncMock()), patch("cogs.sessions._tz", return_value=None):
            await cog.post_player_of_the_day(guild, channel, record)
            await cog.post_player_of_the_day(guild, channel, record)   # second call: already posted
        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertIn("<@42>", kwargs["embed"].description)
        self.assertEqual(kwargs["file"].filename, "player-of-the-day.png")
        self.assertTrue(potd.already_done(5))

    async def test_can_be_turned_off(self):
        import cogs.session_reports as reports
        set_setting("10", "reports:potd", "0")
        mid = self.game(ts(2026, 10, 3), "W", [("Ali", {"r": 8.5})])
        cog = reports.SessionReportsCog(SimpleNamespace(get_cog=lambda n: None))
        channel = SimpleNamespace(send=AsyncMock())
        with patch("cogs.session_reports.report_health", AsyncMock()):
            await cog.post_player_of_the_day(SimpleNamespace(id=10), channel,
                                             {"session_id": 6, "match_ids": json.dumps([mid])})
        channel.send.assert_not_awaited()


class RecordTests(Base):
    async def test_records_page(self):
        self.game(ts(2026, 9, 1), "W", [("Ali", {"g": 2, "r": 7.0})], gf=3, ga=0)
        self.game(ts(2026, 9, 2), "W", [("Ali", {"g": 1, "r": 9.1}), ("Bob", {"a": 4})], gf=5, ga=1)
        self.game(ts(2026, 9, 3), "L", [("Bob", {"g": 3})], gf=3, ga=6)
        self.game(ts(2026, 9, 4), "W", [("Bob", {"g": 3})], gf=4, ga=0)   # same goals as Bob's earlier: first one keeps it
        rec = R.compute(CLUB_ID)
        self.assertEqual((rec["team"]["biggest_win"]["our"], rec["team"]["biggest_win"]["their"]), (5, 1))   # 4-goal margin, more goals than 4–0
        self.assertEqual(rec["team"]["heaviest_defeat"]["their"], 6)
        self.assertEqual(rec["team"]["win"]["length"], 2)
        self.assertEqual(rec["team"]["current"], {"result": "W", "length": 1})
        self.assertEqual(rec["player_game"]["goals"]["ts"], ts(2026, 9, 3))
        self.assertEqual(rec["player_game"]["rating"]["name"], "Ali")
        self.assertEqual(rec["player_total"]["goals"], {"name": "Bob", "value": 6})
        embed = R.build_embed("MADBOYS FC", CLUB_ID, [("Ali", 3)])
        self.assertIn("Most Player of the Day", embed.fields[-1].value)
        self.assertLessEqual(len(embed), 6000)
        self.assertIsInstance(R.build_embed("Empty", 999), str)

    async def test_new_record_detection(self):
        for i in range(R.MIN_GAMES_TO_ANNOUNCE):
            self.game(ts(2026, 9, 1) + i * 3600, "WL"[i % 2], [("Ali", {"g": 1, "r": 7.0})], gf=2, ga=1)
        big = self.game(ts(2026, 10, 1), "W", [("Bob", {"g": 4, "r": 9.5})], gf=6, ga=0)
        lines, _ = R.broken_by(CLUB_ID, {big})
        text = "\n".join(lines)
        self.assertIn("Biggest win ever", text)
        self.assertIn("Most goals in a game:** Bob — 4 goals", text)
        self.assertIn("Highest match rating:** Bob — 9.5", text)
        same = self.game(ts(2026, 10, 2), "W", [("Cat", {"g": 1, "r": 7.0})], gf=2, ga=1)
        self.assertEqual(R.broken_by(CLUB_ID, {same})[0], [])     # nothing beaten

    async def test_win_streak_announced_once(self):
        for i in range(R.MIN_GAMES_TO_ANNOUNCE):
            self.game(ts(2026, 9, 1) + i * 3600, "WL"[i % 2], [("Ali", {"r": 7.0})], gf=2, ga=1)
        key = None
        said = []
        for i in range(5):
            mid = self.game(ts(2026, 10, 1) + i * 3600, "W", [("Ali", {"r": 7.0})], gf=2, ga=1)
            lines, key = R.broken_by(CLUB_ID, {mid}, key)
            said += [l for l in lines if "win streak" in l]
        self.assertEqual(len(said), 1)

    async def test_tiny_history_is_quiet(self):
        mid = self.game(ts(2026, 9, 1), "W", [("Ali", {"g": 5})], gf=9, ga=0)
        self.assertEqual(R.broken_by(CLUB_ID, {mid})[0], [])


class RecordPostTests(Base):
    async def test_tracker_posts_new_records(self):
        from cogs.matchday import MatchdayCog
        for i in range(R.MIN_GAMES_TO_ANNOUNCE):
            self.game(ts(2026, 9, 1) + i * 3600, "W", [("Ali", {"r": 7.0})], gf=1, ga=0)
        big = self.game(ts(2026, 10, 1), "W", [("Ali", {"r": 7.0})], gf=8, ga=0)
        cog = MatchdayCog.__new__(MatchdayCog)
        channel = SimpleNamespace(send=AsyncMock())
        cog.posting_enabled = lambda gid: True
        cog.channel_id_for = lambda gid: 20
        cog._channel = AsyncMock(return_value=channel)
        club = {"club_id": CLUB_ID, "name": "MADBOYS FC"}
        await cog._announce_records([(club, SimpleNamespace(id=10), [SimpleNamespace(match_id=big, club_id=CLUB_ID)])])
        channel.send.assert_awaited_once()
        self.assertIn("Biggest win ever", channel.send.await_args.kwargs["embed"].description)


class RecordsMenuTests(Base):
    async def test_records_in_menu_and_help(self):
        from cogs.hub import SQUAD_PAGES, StatsMenu
        from cogs.misc import HELP_SECTIONS
        self.assertIn("records", SQUAD_PAGES)
        self.assertIn("/records", [c for _, cmds in HELP_SECTIONS for c, _ in cmds])
        self.game(ts(2026, 9, 1), "W", [("Ali", {"g": 2})], gf=3, ga=0)
        guild = SimpleNamespace(id=10, get_member=lambda _: None, fetch_member=AsyncMock())
        bot = SimpleNamespace(ea=SimpleNamespace(), get_guild=lambda _: guild, get_cog=lambda n: None)
        menu = StatsMenu(bot, SimpleNamespace(id=42), "10", ["Ali"], [])
        menu.stat = "records"
        await menu.go("leaderboard")
        self.assertIn("Club records", menu.embed.title)
