"""Exercise real match storage/posting across partial EA checks and recovery."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

import db
import ea_client
import match_data as md
from cogs import matchday, operations


class FinalReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patcher = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "review.db"))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        db.init_all()
        self.club = matchday.CLUB_ID
        self.channel = SimpleNamespace(send=AsyncMock())
        self.guild = SimpleNamespace(id=10)
        self.reports = SimpleNamespace(after_poll=AsyncMock())
        self.bot = SimpleNamespace(get_cog=lambda name: self.reports if name == "SessionReportsCog" else None)
        self.tracker = object.__new__(matchday.MatchdayCog)
        self.tracker.bot = self.bot
        self.tracker.home_guild = lambda: self.guild
        self.tracker._linked_in, self.tracker._unlinked = {}, {}
        self.tracker._log_rotation = lambda *args: {}
        self.tracker.check_milestones = AsyncMock()
        self.tracker._channel = AsyncMock(return_value=self.channel)
        db.set_setting("10", "matchday_channel", "20")
        self.ea = ea_client.EAClient()
        self.tracker.ea = self.ea
        self.tracker.last_poll_partial = False
        self.tracker._migrated = False

    def raw(self, mid, ts, goals=2, conceded=1, opponent="42", name="Rivals"):
        return {"matchId": mid, "timestamp": ts,
            "clubs": {str(self.club): {"goals": goals}, opponent: {"goals": conceded, "details": {"name": name}}}}

    def store(self, raw):
        match = md.parse_match(raw, self.club)
        md.store_match(self.club, match, raw)
        return match

    async def test_partial_check_posts_available_result_and_full_recovery_finishes_without_repost(self):
        self.store(self.raw("baseline", 1000))
        league = self.raw("league-new", 2000)
        # First check: league works, playoffs fails. Second: both work; no unseen games.
        self.ea.get_recent_matches = AsyncMock(side_effect=[[league], None, [league], []])
        with patch("config.MATCH_TYPES", ["leagueMatch", "playoffMatch"]), \
             patch.object(md, "match_post", AsyncMock(return_value=(discord.Embed(title="Full time"), None))):
            self.assertEqual(await self.tracker.poll_once(), 1)
            self.assertTrue(md.is_stored(self.club, "league-new"))
            self.channel.send.assert_awaited_once()
            self.assertTrue(self.tracker.last_poll_partial)
            self.assertFalse(self.tracker.last_poll_ok)
            self.reports.after_poll.assert_not_awaited()
            self.assertIsNone(db.get_setting("10", "matchday:last_success"))
            self.assertEqual(await self.tracker.poll_once(), 0)
        self.assertFalse(self.tracker.last_poll_partial)
        self.assertTrue(self.tracker.last_poll_ok)
        self.channel.send.assert_awaited_once()
        self.reports.after_poll.assert_awaited_once_with(self.guild)
        self.assertIsNotNone(db.get_setting("10", "matchday:last_success"))

    async def test_total_failure_neither_posts_nor_finishes(self):
        self.ea.get_recent_matches = AsyncMock(return_value=None)
        with patch("config.MATCH_TYPES", ["leagueMatch", "playoffMatch"]):
            self.assertEqual(await self.tracker.poll_once(), 0)
        self.channel.send.assert_not_awaited()
        self.reports.after_poll.assert_not_awaited()
        self.assertFalse(self.tracker.last_poll_ok)
        self.assertFalse(self.tracker.last_poll_partial)

    async def test_rematch_uses_opponent_id_and_excludes_future_catchup_matches(self):
        self.store(self.raw("first", 1000, 0, 3, name="Old club name"))
        self.store(self.raw("second", 2000, 2, 1))
        current = self.store(self.raw("current", 3000))
        self.store(self.raw("future", 4000))
        self.store(self.raw("other-club", 1500, opponent="99", name="Rivals"))
        text = md.rematch_line(self.club, current)
        self.assertEqual(text, "3rd meeting · Previously: ❌ 0–3 · ✅ 2–1")
        with patch.object(md, "match_post", AsyncMock(return_value=(discord.Embed(), None))):
            await self.tracker._post_results(self.channel, [current], "10")
        embed = self.channel.send.call_args.kwargs["embed"]
        self.assertIn(text, [field.value for field in embed.fields])

    async def test_rematch_first_meeting_unknown_and_ordinal(self):
        first = self.store(self.raw("first", 1000))
        self.assertIsNone(md.rematch_line(self.club, first))
        unknown = md.ParsedMatch("unknown", "leagueMatch", 9999, 0, 0, None, "Unknown")
        self.assertIsNone(md.rematch_line(self.club, unknown))
        for i in range(2, 11):
            self.store(self.raw(str(i), i * 1000))
        eleventh = md.parse_match(self.raw("eleventh", 11000), self.club)
        self.assertTrue(md.rematch_line(self.club, eleventh).startswith("11th meeting"))

    async def test_fetched_matches_do_not_mutate_shared_cache_payloads(self):
        raw = self.raw("a", 1000)
        self.ea.get_recent_matches = AsyncMock(side_effect=[[raw], []])
        result = await self.ea.get_recent_matches_multi(self.club, match_types=["leagueMatch", "playoffMatch"])
        self.assertTrue(result.complete)
        self.assertEqual(result[0]["_matchType"], "leagueMatch")
        self.assertNotIn("_matchType", raw)

    async def test_partial_status_is_explicit_and_does_not_claim_full_success(self):
        tracker = SimpleNamespace(last_poll_ok=False, last_poll_partial=True, last_poll_at=2000,
            last_success_at=None, next_poll_at=2100, ticker=SimpleNamespace(is_running=lambda: True),
            channel_id_for=lambda gid: 20, posting_enabled=lambda gid: True)
        bot = SimpleNamespace(get_cog=lambda name: tracker if name == "MatchdayCog" else None)
        embed = operations.status_embed(bot, "10")
        self.assertIn("Partial check", embed.fields[0].value)
        self.assertIn("session summaries waiting", embed.fields[0].value)
