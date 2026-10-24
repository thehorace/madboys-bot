"""Forfeits / kick-off disconnects (EA's 0–3 default) don't count as real games."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

import db
import ea_client
import match_data as md
from cogs import matchday


def raw(mid, ts, secs=5400, goals=2, conceded=1, our_result="1", their_result="2", dnf=0, players=True, club=None):
    club = club or matchday.CLUB_ID
    ps = {"p1": {"playername": "Ali", "goals": "1", "rating": "7.5", "pos": "forward", "secondsPlayed": str(secs)},
          "p2": {"playername": "Bob", "rating": "7.0", "pos": "defender", "secondsPlayed": str(secs)}}
    return {"matchId": mid, "timestamp": ts,
            "clubs": {str(club): {"goals": str(goals), "result": our_result, "winnerByDnf": str(dnf)},
                      "42": {"goals": str(conceded), "result": their_result, "winnerByDnf": "0",
                             "details": {"name": "Rivals"}}},
            "players": {str(club): ps} if players else {}}


class VoidRuleTests(unittest.TestCase):
    def test_rule(self):
        c = matchday.CLUB_ID
        self.assertIsNone(md.void_reason(md.parse_match(raw("full", 1), c)))
        self.assertIsNotNone(md.void_reason(md.parse_match(raw("kickoff", 1, secs=40, goals=0, conceded=3,
                                                                our_result="10", their_result="16385"), c)))
        late_quit = raw("late", 1, secs=5300, goals=4, conceded=1, our_result="16385", their_result="10", dnf=1)
        self.assertIsNone(md.void_reason(md.parse_match(late_quit, c)))      # they quit late: a real win
        self.assertIsNotNone(md.void_reason(md.parse_match(
            raw("empty", 1, goals=0, conceded=3, our_result="10", players=False), c)))
        old = raw("old", 1, secs=40)
        for p in old["players"][str(c)].values():
            p.pop("secondsPlayed")
        self.assertIsNone(md.void_reason(md.parse_match(old, c)))           # no minutes in old data: keep it


class VoidTrackerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(db, "DB_PATH", str(Path(temp.name) / "v.db"))
        p.start()
        self.addCleanup(p.stop)
        db.init_all()
        self.club = matchday.CLUB_ID
        with db.connect() as conn:
            conn.execute("UPDATE monitored_clubs SET enabled=0 WHERE club_id<>?", (self.club,))
        self.channel = SimpleNamespace(send=AsyncMock())
        self.guild = SimpleNamespace(id=10)
        self.tracker = object.__new__(matchday.MatchdayCog)
        self.tracker.bot = SimpleNamespace(get_cog=lambda name: SimpleNamespace(after_poll=AsyncMock())
                                           if name == "SessionReportsCog" else None)
        self.tracker.home_guild = lambda: self.guild
        self.tracker._linked_in, self.tracker._unlinked = {}, {}
        self.tracker._log_rotation = lambda *args: {}
        self.tracker.check_milestones = AsyncMock()
        self.tracker._channel = AsyncMock(return_value=self.channel)
        db.set_setting("10", "matchday_channel", "20")
        self.tracker.ea = ea_client.EAClient()
        self.tracker.last_poll_partial = False
        self.tracker._migrated = False
        base = raw("baseline", 1000)
        md.store_match(self.club, md.parse_match(base, self.club), base)

    async def test_forfeit_gets_a_note_not_a_result_and_isnt_stored(self):
        forfeit = raw("ff", 2000, secs=30, goals=0, conceded=3, our_result="10", their_result="16385")
        self.tracker.ea.get_recent_matches = AsyncMock(return_value=[forfeit])
        with patch("config.MATCH_TYPES", ["leagueMatch"]), \
             patch.object(md, "match_post", AsyncMock(return_value=(discord.Embed(), None))) as post:
            self.assertEqual(await self.tracker.poll_once(), 0)
            await self.tracker.poll_once()                      # seen again next check: no repeat note
        post.assert_not_awaited()
        self.channel.send.assert_awaited_once()
        self.assertIn("not counted", self.channel.send.await_args.args[0])
        self.assertFalse(md.is_stored(self.club, "ff"))
        self.assertEqual(md.stored_ids(self.club, ["ff", "baseline"]), {"ff", "baseline"})

    def test_old_void_games_leave_the_stats_once(self):
        ff = raw("old-ff", 1500, secs=20, goals=0, conceded=3, our_result="10")
        md.store_match(self.club, md.parse_match(ff, self.club), ff)
        with db.connect() as conn:
            conn.execute("INSERT INTO rotation_log (guild_id,club,discord_id,position,logged_at,source,match_id) "
                         "VALUES ('10','x','5','ST','t','ea','old-ff')")
        md.purge_void_matches()
        self.assertFalse(md.is_stored(self.club, "old-ff"))
        self.assertTrue(md.is_stored(self.club, "baseline"))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM match_players WHERE match_id='old-ff'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rotation_log").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM void_matches").fetchone()[0], 1)

    async def test_last_game_skips_void(self):
        from clubs import club_scope
        from cogs import stats as S
        forfeit = raw("ff2", 3000, secs=30, goals=0, conceded=3, our_result="10")
        real = raw("real", 2500, goals=3, conceded=0)
        ea = SimpleNamespace(get_recent_matches_multi=AsyncMock(return_value=[forfeit, real]), stale_note=lambda: None)
        with club_scope({"club_id": self.club, "name": "MADBOYS FC"}):
            screen = await S.build_lastgame(ea)
        embed = screen[0] if isinstance(screen, tuple) else screen
        self.assertIn("3", json.dumps(embed.to_dict()))
        self.assertNotIn("0–3", json.dumps(embed.to_dict(), ensure_ascii=False))
