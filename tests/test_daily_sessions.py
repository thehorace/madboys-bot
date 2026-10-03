import asyncio
import tempfile
import time
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
        self.cog.bot.get_cog.return_value = None
        self.cog.bot.user = SimpleNamespace(id=999)
        self.cog._sticky_lock = asyncio.Lock()
        self.channel_patch = patch.object(sessions, "SESSION_CHANNEL_ID", None)
        self.channel_patch.start()
        self.addCleanup(self.channel_patch.stop)

    def sticky_session(self, starts_at=None, cancelled=0):
        with db.connect() as conn:
            sid = conn.execute(
                "INSERT INTO sessions (guild_id,channel_id,message_id,starts_at,created_by,cancelled) "
                "VALUES ('10','20','50',?,'daily',?)",
                (starts_at if starts_at is not None else int(time.time()) + 3600, cancelled)).lastrowid
        new = SimpleNamespace(id=1000, edit=AsyncMock())
        self.channel.send.return_value = new
        self.channel.get_partial_message.return_value = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())
        return sid

    def chat_message(self, message_id, author_id=1):
        return SimpleNamespace(id=message_id, guild=self.guild, channel=self.channel,
                               author=SimpleNamespace(id=author_id))

    def rows(self):
        with db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM sessions")]

    async def test_restart_does_not_duplicate_and_no_bot_rsvp(self):
        await self.cog.post_daily_session(self.guild, 123)
        await self.cog.post_daily_session(self.guild, 123)
        self.channel.send.assert_awaited_once()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["message_id"], "30")
        first = self.channel.send.call_args.kwargs
        self.assertEqual(first["content"], "@everyone")
        self.assertTrue(first["allowed_mentions"].everyone)
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

    async def test_sticky_threshold_keeps_votes_and_uses_new_message(self):
        sid = self.sticky_session()
        sessions.set_rsvp(sid, "42", "yes")
        with patch.object(sessions, "resolve_name", AsyncMock(return_value="Fauz")):
            for mid in range(100, 109):
                await self.cog.on_message(self.chat_message(mid))
            self.channel.send.assert_not_awaited()
            await self.cog.on_message(self.chat_message(109))
        self.channel.send.assert_awaited_once()
        self.assertFalse(self.channel.send.call_args.kwargs["allowed_mentions"].everyone)
        self.assertNotIn("content", self.channel.send.call_args.kwargs)
        self.assertEqual(self.rows()[0]["message_id"], "1000")
        self.assertEqual(self.rows()[0]["sticky_messages"], 0)
        self.assertEqual(sessions.get_rsvps(sid)["yes"], ["42"])
        self.assertEqual(sessions.get_session_by_message(1000)["id"], sid)
        self.assertIsNone(sessions.get_session_by_message(50))
        self.channel.get_partial_message.assert_called_once_with(50)
        self.channel.get_partial_message.return_value.delete.assert_awaited_once()

    async def test_manual_creation_pings_everyone(self):
        interaction = SimpleNamespace(
            guild=self.guild, guild_id=10, channel_id=20, user=SimpleNamespace(id=42),
            extras={},
            response=SimpleNamespace(send_message=AsyncMock()),
            original_response=AsyncMock(return_value=SimpleNamespace(id=50)))
        with patch.object(sessions, "resolve_name", AsyncMock(return_value="Fauz")):
            await sessions.SessionsCog.session_create.callback(
                self.cog, interaction, SimpleNamespace(value="tomorrow"), "6:30pm")
        sent = interaction.response.send_message.call_args.kwargs
        self.assertEqual(sent["content"], "@everyone")
        self.assertTrue(sent["allowed_mentions"].everyone)
        self.assertEqual(self.rows()[0]["message_id"], "50")

    async def test_sticky_removes_buttons_if_old_message_cannot_be_deleted(self):
        self.sticky_session()
        old = self.channel.get_partial_message.return_value
        old.delete.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
        for mid in range(100, 110):
            await self.cog.on_message(self.chat_message(mid))
        old.edit.assert_awaited_once()
        self.assertIsNone(old.edit.call_args.kwargs["view"])
        self.assertEqual(self.rows()[0]["message_id"], "1000")

    async def test_sticky_counter_survives_restart_and_concurrent_messages(self):
        self.sticky_session()
        for mid in range(100, 105):
            await self.cog.on_message(self.chat_message(mid))
        replacement = object.__new__(sessions.SessionsCog)
        replacement.bot = self.cog.bot
        replacement._sticky_lock = asyncio.Lock()
        await asyncio.gather(*(replacement.on_message(self.chat_message(mid)) for mid in range(105, 110)))
        self.channel.send.assert_awaited_once()

    async def test_sticky_ignores_own_posts_started_cancelled_and_other_channels(self):
        self.sticky_session()
        await self.cog.on_message(self.chat_message(100, author_id=999))
        await self.cog.on_message(self.chat_message(49))
        other = self.chat_message(101)
        other.channel = SimpleNamespace(id=21)
        await self.cog.on_message(other)
        self.assertEqual(self.rows()[0]["sticky_messages"], 0)
        with db.connect() as conn:
            conn.execute("UPDATE sessions SET cancelled=1")
        for mid in range(110, 120):
            await self.cog.on_message(self.chat_message(mid))
        with db.connect() as conn:
            conn.execute("UPDATE sessions SET cancelled=0, starts_at=?", (int(time.time()),))
        for mid in range(120, 130):
            await self.cog.on_message(self.chat_message(mid))
        self.channel.send.assert_not_awaited()

    async def test_sticky_send_failure_preserves_old_post_and_retries(self):
        self.sticky_session()
        self.channel.send.side_effect = discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "retry")
        for mid in range(100, 110):
            await self.cog.on_message(self.chat_message(mid))
        self.assertEqual(self.rows()[0]["message_id"], "50")
        self.channel.get_partial_message.assert_not_called()
        self.channel.send.side_effect = None
        await self.cog.on_message(self.chat_message(110))
        self.assertEqual(self.rows()[0]["message_id"], "1000")

    async def test_changed_daily_time_does_not_duplicate_a_posted_session(self):
        await self.cog.post_daily_session(self.guild, 100000)
        await self.cog.post_daily_session(self.guild, 100000 + 1800)
        self.channel.send.assert_awaited_once()
        self.assertEqual(len(self.rows()), 1)

    async def test_unsent_daily_session_can_adopt_changed_time(self):
        self.channel.send.side_effect = discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "retry")
        with self.assertRaises(discord.HTTPException):
            await self.cog.post_daily_session(self.guild, 100000)
        self.channel.send.side_effect = None
        await self.cog.post_daily_session(self.guild, 101800)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["starts_at"], 101800)
        self.assertEqual(self.rows()[0]["message_id"], "30")


if __name__ == "__main__":
    unittest.main()
