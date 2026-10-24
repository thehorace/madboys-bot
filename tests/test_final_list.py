"""Sign-ups in #general, final squad list in announcements at kick-off."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import db
from announce import set_announce_channel
from cogs import sessions


class FinalListTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(db, "DB_PATH", str(Path(temp.name) / "f.db"))
        p.start()
        self.addCleanup(p.stop)
        db.init_all()
        self.now = int(time.time())
        with db.connect() as conn:
            self.sid = conn.execute("INSERT INTO sessions (guild_id,channel_id,message_id,starts_at,created_by) "
                                    "VALUES ('10','1','500',?,'daily')", (self.now - 60,)).lastrowid
        for i, status in enumerate(["yes", "yes", "maybe", "no"]):
            sessions.set_rsvp(self.sid, str(100 + i), status)
        self.general = SimpleNamespace(id=1, send=AsyncMock())
        self.announcements = SimpleNamespace(id=2, send=AsyncMock())
        chans = {1: self.general, 2: self.announcements}
        self.guild = SimpleNamespace(id=10, get_channel=lambda cid: chans.get(cid), get_member=lambda _: None,
                                     fetch_member=AsyncMock())
        self.cog = object.__new__(sessions.SessionsCog)
        self.cog.bot = SimpleNamespace(get_guild=lambda _: self.guild, fetch_channel=AsyncMock())

    async def run_loop(self):
        with patch("cogs.sessions.report_health", AsyncMock()):
            await self.cog.final_lists.coro(self.cog)

    async def test_final_list_goes_to_announcements_once(self):
        set_announce_channel("10", 2)
        await self.run_loop()
        await self.run_loop()
        self.announcements.send.assert_awaited_once()
        self.general.send.assert_not_awaited()
        embed = self.announcements.send.await_args.kwargs["embed"]
        self.assertIn("Final squad", embed.title)
        self.assertIn("<@100>", embed.fields[0].value)
        self.assertIn("Playing (2)", embed.fields[0].name)
        self.assertIn("Maybe (1)", embed.fields[1].name)
        self.assertNotIn("<@103>", str([f.value for f in embed.fields]))       # "out" isn't listed
        self.assertFalse(self.announcements.send.await_args.kwargs["allowed_mentions"].users)

    async def test_no_announcements_channel_posts_with_the_sign_up(self):
        await self.run_loop()
        self.general.send.assert_awaited_once()

    async def test_not_before_kick_off_and_not_for_cancelled(self):
        with db.connect() as conn:
            conn.execute("UPDATE sessions SET starts_at=? WHERE id=?", (self.now + 600, self.sid))
        await self.run_loop()
        with db.connect() as conn:
            conn.execute("UPDATE sessions SET starts_at=?, cancelled=1 WHERE id=?", (self.now - 60, self.sid))
        await self.run_loop()
        self.general.send.assert_not_awaited()

    def test_sign_ups_default_to_general_even_with_announcements_set(self):
        set_announce_channel("10", 2)
        settings = sessions.session_settings("10")
        self.assertEqual(settings["channel_id"], "")          # empty = #general at post time
        self.assertEqual(settings["final_channel_id"], "2")


def row_of(sid):
    with db.connect() as conn:
        return dict(conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())


class MoveTodayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(db, "DB_PATH", str(Path(temp.name) / "m.db"))
        p.start()
        self.addCleanup(p.stop)
        db.init_all()

    async def test_todays_open_sign_up_moves_to_general_once(self):
        import discord
        now = int(time.time())
        with db.connect() as conn:
            sid = conn.execute("INSERT INTO sessions (guild_id,channel_id,message_id,starts_at,created_by) "
                               "VALUES ('10','2','500',?,'daily')", (now + 3600,)).lastrowid
        sessions.set_rsvp(sid, "42", "yes")
        old = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())
        announcements = SimpleNamespace(id=2, name="announcements", get_partial_message=lambda _: old)
        class FakeText(discord.TextChannel):   # passes the isinstance check; attributes settable
            pass
        general = FakeText.__new__(FakeText)
        general.id, general.name = 1, "general"
        general.send = AsyncMock(return_value=SimpleNamespace(id=900))
        guild = SimpleNamespace(id=10, text_channels=[general, announcements],
                                get_channel=lambda cid: {1: general, 2: announcements}.get(cid),
                                get_member=lambda _: SimpleNamespace(display_name="Fauz"), fetch_member=AsyncMock())
        cog = object.__new__(sessions.SessionsCog)
        cog.bot = SimpleNamespace(get_channel=lambda cid: {1: general, 2: announcements}.get(cid),
                                  fetch_channel=AsyncMock())
        cog._checked_today_channel = False
        with patch.object(sessions, "GUILD_ID", "10"), patch.object(cog.bot, "get_guild", lambda _: guild, create=True), \
                patch.object(sessions, "daily_start", return_value=None), \
                patch.object(sessions.SessionsCog, "todays_daily_session", lambda self, gid: row_of(sid)):
            await cog.daily_sessions.coro(cog)
            await cog.daily_sessions.coro(cog)          # second minute: no second move
        general.send.assert_awaited_once()
        old.delete.assert_awaited_once()
        with db.connect() as conn:
            row = conn.execute("SELECT channel_id, message_id FROM sessions WHERE id=?", (sid,)).fetchone()
        self.assertEqual((row["channel_id"], row["message_id"]), ("1", "900"))
        self.assertEqual(sessions.get_rsvps(sid)["yes"], ["42"])     # sign-ups kept
