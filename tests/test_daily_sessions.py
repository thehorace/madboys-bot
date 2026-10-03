import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import discord

import db
from cogs import sessions


class DailyTimeTests(unittest.TestCase):
    def test_post_window_and_kickoff(self):
        with patch.object(sessions, "BOT_TZ", "Asia/Singapore"):
            tz = ZoneInfo("Asia/Singapore")
            expected = int(datetime(2026, 10, 3, 18, 30, tzinfo=tz).timestamp())
            for hour, minute, due in [(10, 59, False), (11, 0, True),
                                      (18, 29, True), (18, 30, False), (23, 0, False)]:
                now = datetime(2026, 10, 3, hour, minute, tzinfo=tz)
                self.assertEqual(sessions.daily_start(now), expected if due else None)
            # A UTC clock must produce the same Singapore calendar day and time.
            self.assertEqual(sessions.daily_start(datetime(2026, 10, 3, 3, tzinfo=timezone.utc)), expected)
            self.assertEqual(sessions.daily_start(datetime(2026, 10, 4, 11, tzinfo=tz)), expected + 86400)


class DailyPostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "test.db"))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init_all()
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.id, self.channel.name = 20, "general"
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=30))
        self.guild = SimpleNamespace(id=10, text_channels=[self.channel],
                                     get_channel=lambda _: self.channel)
        self.cog = object.__new__(sessions.SessionsCog)
        self.cog.bot = Mock()
        self.channel_patch = patch.object(sessions, "SESSION_CHANNEL_ID", None)
        self.channel_patch.start()
        self.addCleanup(self.channel_patch.stop)

    def rows(self):
        with db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM sessions")]

    async def test_restart_does_not_duplicate_and_no_bot_rsvp(self):
        await self.cog.post_daily_session(self.guild, 123)
        await self.cog.post_daily_session(self.guild, 123)
        self.channel.send.assert_awaited_once()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["message_id"], "30")
        self.assertEqual(sessions.get_rsvps(self.rows()[0]["id"])["yes"], [])
        await self.cog.post_daily_session(self.guild, 123 + 86400)
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_cancelled_and_manual_sessions_suppress_post(self):
        with db.connect() as conn:
            conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by,cancelled) "
                         "VALUES ('10','20',123,'daily',1), ('10','20',456,'user',0)")
        await self.cog.post_daily_session(self.guild, 123)
        await self.cog.post_daily_session(self.guild, 456)
        self.channel.send.assert_not_awaited()

    async def test_failed_send_reuses_pending_row(self):
        self.channel.send.side_effect = discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "retry")
        with self.assertRaises(discord.HTTPException):
            await self.cog.post_daily_session(self.guild, 123)
        self.assertIsNone(self.rows()[0]["message_id"])
        self.channel.send.side_effect = None
        await self.cog.post_daily_session(self.guild, 123)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["message_id"], "30")

    async def test_ambiguous_general_requires_channel_id(self):
        self.guild.text_channels.append(self.channel)
        await self.cog.post_daily_session(self.guild, 123)
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.rows(), [])
        with patch.object(sessions, "SESSION_CHANNEL_ID", "20"):
            await self.cog.post_daily_session(self.guild, 123)
        self.channel.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
