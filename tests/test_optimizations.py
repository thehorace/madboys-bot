"""Tests for the reliability/performance pass: EA client, loops, session edge cases, admin panel."""
import asyncio
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from aiohttp import web

import db
import ea_client
import utils
from cogs import sessions
import tests.test_patchnotes as TP
from tests.test_patchnotes import article


class FakeRelay:
    """A tiny local HTTP server standing in for the home EA relay."""

    def __init__(self, delay=0.0, fail=False):
        self.delay, self.fail, self.hits = delay, fail, 0

    async def handle(self, request):
        self.hits += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            return web.Response(status=502, text="EA down")
        return web.json_response([{"name": "Killa"}, {"name": "fauz"}])

    async def __aenter__(self):
        app = web.Application()
        app.router.add_get("/members", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()


class EAClientTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, url):
        with patch.dict("os.environ", {"MIDDLEWARE_URL": url}):
            ea = ea_client.EAClient()
        self.addAsyncCleanup(ea.close)
        return ea

    async def test_simultaneous_requests_share_one_relay_call(self):
        async with FakeRelay(delay=0.3) as relay:
            ea = await self.client(relay.url)
            results = await asyncio.gather(*[ea.get_member_stats(1) for _ in range(8)])
        self.assertEqual(relay.hits, 1)
        self.assertTrue(all(r and r[0]["name"] == "Killa" for r in results))
        self.assertEqual(sorted(ea.cached_member_names(1)), ["Killa", "fauz"])

    async def test_breaker_serves_cache_instantly_while_relay_is_unreachable(self):
        async with FakeRelay() as relay:
            ea = await self.client(relay.url)
            self.assertTrue(await ea.get_member_stats(1))          # warm the cache
        # relay gone (connection refused) and the cached copy has expired
        for key, (ts, data) in list(ea._cache.items()):
            ea._cache[key] = (ts - ea_client.CACHE_TTL - 1, data)
        self.assertTrue(await ea.get_member_stats(1))              # stale copy, breaker opens
        self.assertGreater(ea._down_until, time.time())
        ea._fail_until.clear()
        t0 = time.perf_counter()
        self.assertTrue(await ea.get_member_stats(1))              # answered without trying the relay
        self.assertLess(time.perf_counter() - t0, 0.05)
        self.assertIsNotNone(ea.stale_note())

    async def test_http_errors_do_not_trip_the_breaker(self):
        async with FakeRelay(fail=True) as relay:
            ea = await self.client(relay.url)
            self.assertIsNone(await ea.get_member_stats(1))
        self.assertEqual(ea._down_until, 0.0)


class SurviveTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_keeps_running_after_an_unexpected_error(self):
        calls = []

        @discord.ext.tasks.loop(seconds=0.01, count=3)
        @utils.survive
        async def job():
            calls.append(1)
            raise RuntimeError("database is locked")

        job.start()
        await asyncio.sleep(0.2)
        self.assertEqual(len(calls), 3)


class NameCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_people_who_left_are_only_looked_up_once(self):
        utils._LEFT.clear()
        guild = SimpleNamespace(get_member=lambda _: None,
                                fetch_member=AsyncMock(side_effect=discord.NotFound(
                                    SimpleNamespace(status=404, reason="Not Found"), "gone")))
        self.assertIn("left server", await utils.resolve_name(guild, "5"))
        self.assertIn("left server", await utils.resolve_name(guild, "5"))
        guild.fetch_member.assert_awaited_once()


class SessionEdgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "t.db"))
        p.start()
        self.addCleanup(p.stop)
        db.init_all()
        self.cog = object.__new__(sessions.SessionsCog)
        self.cog.bot = Mock()
        self.channel = Mock()
        self.channel.get_partial_message.return_value = SimpleNamespace(edit=AsyncMock())
        self.cog.bot.get_channel.return_value = self.channel

    def add(self, starts_at, created_by="daily"):
        with db.connect() as conn:
            return conn.execute("INSERT INTO sessions (guild_id,channel_id,message_id,starts_at,created_by) "
                                "VALUES ('10','20','50',?,?)", (starts_at, created_by)).lastrowid

    def interaction(self):
        return SimpleNamespace(guild_id=10, guild=SimpleNamespace(id=10), extras={},
                               user=SimpleNamespace(id=1, name="fauz", roles=[SimpleNamespace(name="Manager")]),
                               response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()))

    def cancelled(self, sid):
        with db.connect() as conn:
            return conn.execute("SELECT cancelled FROM sessions WHERE id=?", (sid,)).fetchone()[0]

    async def test_cancel_skips_the_session_already_being_played(self):
        now = int(time.time())
        playing, tomorrow = self.add(now - 1800), self.add(now + 86400)
        with patch.object(sessions, "resolve_name", AsyncMock(return_value="x")):
            await sessions.SessionsCog.session_cancel.callback(self.cog, self.interaction())
        self.assertEqual((self.cancelled(playing), self.cancelled(tomorrow)), (0, 1))

    async def test_create_rejects_a_time_that_already_passed(self):
        inter = self.interaction()
        inter.channel_id = 20
        past = (sessions.datetime.now(sessions._tz()) - timedelta(minutes=20)).strftime("%H:%M")
        if past > sessions.datetime.now(sessions._tz()).strftime("%H:%M"):
            self.skipTest("just after midnight")
        await sessions.SessionsCog.session_create.callback(
            self.cog, inter, SimpleNamespace(value="today"), past)
        self.assertIn("already passed", inter.response.send_message.await_args.args[0])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    async def test_small_kickoff_nudge_does_not_resend_the_reminder(self):
        now = int(time.time())
        sid = self.add(now + 600)
        with db.connect() as conn:
            conn.execute("UPDATE sessions SET reminded=1 WHERE id=?", (sid,))
        s = dict(starts_at=now + 600, reminded=1, id=sid, channel_id="20")
        with patch.object(self.cog, "todays_daily_session", return_value=s):
            new = sessions.datetime.fromtimestamp(now + 1200, sessions._tz("10"))
            if new.date() != sessions.datetime.fromtimestamp(now + 600, sessions._tz("10")).date():
                self.skipTest("crosses midnight")
            self.cog.update_today_in_db("10", {"kickoff_time": new.strftime("%H:%M")},
                                        {"kickoff_time": new.strftime("%H:%M"), "waitlist": True})
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT reminded FROM sessions WHERE id=?", (sid,)).fetchone()[0], 1)


class AdminPanelRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_uses_the_current_click_not_the_expired_command_token(self):
        from cogs.admin import AdminView
        anchor = SimpleNamespace(edit_original_response=AsyncMock())
        guild = SimpleNamespace(id=10)
        with patch("cogs.admin.AdminView.render"), patch("cogs.admin.AdminView.embed", return_value=None):
            panel = AdminView(SimpleNamespace(), SimpleNamespace(id=1), guild, anchor)
            click = SimpleNamespace(message=SimpleNamespace(id=99),
                                    followup=SimpleNamespace(edit_message=AsyncMock()))
            await panel.refresh_anchor(click)
        click.followup.edit_message.assert_awaited_once()
        anchor.edit_original_response.assert_not_awaited()


class AdminPagesTests(unittest.TestCase):
    def test_no_admin_page_repeats_a_component_id(self):
        """Discord refuses the whole message if two buttons share an id (Bot status used to)."""
        from collections import Counter
        from cogs.admin import AdminView
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.object(db, "DB_PATH", str(Path(temp.name) / "a.db")):
            db.init_all()
            with patch("cogs.admin.AdminView.embed", return_value=None):
                view = AdminView(SimpleNamespace(get_cog=lambda n: None), SimpleNamespace(id=1),
                                 SimpleNamespace(id=10), None)
            for page in ("home", "settings", "status", "sessions", "patchnotes", "reports", "tracker_settings"):
                view.page = page
                view.render()
                ids = Counter(c.custom_id for c in view.children)
                self.assertEqual([i for i, n in ids.items() if n > 1], [], page)
                self.assertTrue(all(sum(c.row == r for c in view.children) <= 5 for r in range(5)), page)


class PatchNotesSkipTests(TP.MonitorTests):
    """Reuses MonitorTests' fake EA site; only the test below runs here."""

    async def test_unreadable_article_does_not_block_newer_ones(self):
        await self.cog.check(self.guild)   # first check is quiet
        self.now += timedelta(hours=1)
        bad = article("bad-video-post", title="EA SPORTS FC 27 | Title Update video")
        bad["publishingDate"] = (self.now - timedelta(minutes=20)).isoformat()
        good = article("title-update-v2", title="EA SPORTS FC 27 | Title Update v2")
        good["publishingDate"] = (self.now - timedelta(minutes=10)).isoformat()
        self.items += [bad, good]
        P = TP.P   # the same module object the cog under test came from
        original = P.article_details

        def flaky(page, slug):
            if slug == "bad-video-post":
                raise ValueError("no body")
            return original(page, slug)

        with patch.object(P, "article_details", side_effect=flaky):
            result = await self.cog.check(self.guild)
            self.channel.send.assert_awaited_once()     # the good one still posted
            self.assertIn("skipped", result)
            await self.cog.check(self.guild)             # bad one isn't retried forever
            self.channel.send.assert_awaited_once()


for _name in dir(TP.MonitorTests):   # the inherited tests already run in test_patchnotes
    if _name.startswith("test_") and _name not in PatchNotesSkipTests.__dict__:
        setattr(PatchNotesSkipTests, _name, None)

if __name__ == "__main__":
    unittest.main()
