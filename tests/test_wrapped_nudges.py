"""Season Wrapped card and nudging regulars who haven't answered the sign-up."""
import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

import wrapped as Wr
from cogs import nudges as N
from cogs import sessions
from config import CLUB_ID
from cogs.link import set_link
from db import connect, set_setting
from tests.test_squad_features import SYD, Base, ts


class WrappedTests(Base):
    async def test_season_numbers_and_card(self):
        self.game(ts(2026, 8, 20), "W", [("Ali", {"g": 5, "r": 9.9})])             # last season: ignored
        self.game(ts(2026, 9, 5), "W", [("Ali", {"g": 2, "r": 8.5, "pos": "forward"}), ("Bob", {})], gf=4, ga=0)
        self.game(ts(2026, 9, 12), "L", [("Ali", {"a": 1, "r": 6.5, "pos": "forward"}), ("Bob", {})], gf=1, ga=2)
        self.game(ts(2026, 10, 2), "D", [("ali", {"g": 1, "r": 7.0, "pos": "midfielder"})], gf=1, ga=1)
        w = Wr.compute("10", "Ali", datetime(2026, 10, 20, tzinfo=SYD))
        self.assertEqual(w["season"], "FC 27")
        self.assertEqual((w["games"], w["w"], w["d"], w["l"], w["goals"], w["assists"]), (3, 1, 1, 1, 3, 1))
        self.assertEqual(w["best"]["rating"], 8.5)
        self.assertEqual(w["fav_pos"], "Attack")
        self.assertEqual((w["biggest_win"]["our_goals"], w["biggest_win"]["opp_goals"]), (4, 0))
        self.assertEqual(w["ranks"]["goals"], 1)
        self.assertTrue(any("Best game" in h for h in Wr.highlights(w)))
        self.assertTrue(Wr.render_card(w, SYD).startswith(b"\x89PNG"))
        self.assertIsNone(Wr.compute("10", "Nobody", datetime(2026, 10, 20, tzinfo=SYD)))

    def test_season_boundaries(self):
        self.assertEqual(Wr.season_bounds(datetime(2027, 3, 1, tzinfo=SYD))[1], "FC 27")
        self.assertEqual(Wr.season_bounds(datetime(2027, 9, 1, tzinfo=SYD))[1], "FC 28")

    async def test_command_screen_and_menu_page(self):
        from cogs.insights import build_wrapped
        from cogs.hub import SQUAD_PAGES, StatsMenu
        self.game(int(time.time()) - 3600, "W", [("Ali", {"g": 1, "r": 8.0})])
        with patch("cogs.sessions._tz", return_value=SYD):
            screen = await build_wrapped("10", "Ali")
            self.assertIsInstance(screen, tuple)
            self.assertIsInstance(await build_wrapped("10", None), str)
            set_link("10", "42", "Ali", "self")
            guild = SimpleNamespace(id=10, get_member=lambda _: None, fetch_member=AsyncMock())
            bot = SimpleNamespace(ea=SimpleNamespace(), get_guild=lambda _: guild, get_cog=lambda n: None)
            menu = StatsMenu(bot, SimpleNamespace(id=42), "10", ["Ali", "Bob"], [])
            menu.stat = "wrapped"
            await menu.go("leaderboard")
        self.assertIn("Wrapped", menu.embed.title)
        self.assertIn("wrapped", SQUAD_PAGES)
        picker = next(c for c in menu.children if getattr(c, "row", None) == 3)
        self.assertTrue(picker.placeholder.startswith("🎁"))


class NudgeTests(Base):
    def setUp(self):
        super().setUp()
        N.init_nudges()
        self.now = int(time.time())
        with connect() as conn:
            for i in range(4):   # last 4 finished sessions: 42 played 3, 43 played 1, 44 played 2
                sid = conn.execute("INSERT INTO sessions (guild_id,channel_id,message_id,starts_at,created_by) "
                                   "VALUES ('10','1','m',?,'daily')", (self.now - (i + 1) * 86400,)).lastrowid
                played = [["42", "44"], ["42"], ["42", "44"], ["43"]][i]
                conn.execute("INSERT INTO session_history VALUES (?,?,?,?,?,?,?,?)",
                             (sid, "10", self.now - (i + 1) * 86400, "completed", "[]", "[]",
                              json.dumps([{"name": "x", "discord_ids": played}]), "suppressed"))
            self.sid = conn.execute("INSERT INTO sessions (guild_id,channel_id,message_id,starts_at,created_by) "
                                    "VALUES ('10','1','500',?,'daily')", (self.now + 50 * 60,)).lastrowid
        self.members = {i: SimpleNamespace(id=i, bot=False, send=AsyncMock()) for i in (42, 43, 44)}
        guild = SimpleNamespace(id=10, get_member=lambda i: self.members.get(i))
        self.cog = N.NudgesCog.__new__(N.NudgesCog)
        self.cog.bot = SimpleNamespace(get_guild=lambda _: guild)

    async def test_regulars_who_havent_answered_get_one_dm(self):
        self.assertEqual(N.regulars("10"), {"42": 3, "44": 2})
        sessions.set_rsvp(self.sid, "44", "no")                 # answered (even Out): left alone
        for s in N.due_sessions(self.now):
            await self.cog.send_nudges(s)
        for s in N.due_sessions(self.now):                       # next minute: nothing new
            await self.cog.send_nudges(s)
        self.members[42].send.assert_awaited_once()
        self.members[43].send.assert_not_awaited()               # not a regular
        self.members[44].send.assert_not_awaited()
        text = self.members[42].send.await_args.args[0]
        self.assertIn("3 of the last 4", text)

    async def test_opt_out_and_admin_off(self):
        set_setting("10", "nudge:optout:42", "1")
        await self.cog.send_nudges(N.due_sessions(self.now)[0])
        self.members[42].send.assert_not_awaited()
        self.members[44].send.assert_awaited_once()
        set_setting("10", "session:nudges", "0")
        self.assertFalse(sessions.session_settings("10")["nudges"])

    async def test_not_too_early_or_too_late(self):
        self.assertEqual(N.due_sessions(self.now - 3600), [])          # 110 min before kick-off
        self.assertEqual(N.due_sessions(self.now + 45 * 60), [])        # 5 min before kick-off

    async def test_dm_buttons_answer_the_sign_up(self):
        refresh = AsyncMock()
        client = SimpleNamespace(get_cog=lambda n: SimpleNamespace(refresh_today=refresh),
                                 get_guild=lambda _: SimpleNamespace(id=10))
        inter = SimpleNamespace(user=SimpleNamespace(id=42), client=client, extras={},
                                response=SimpleNamespace(edit_message=AsyncMock()))
        await N.NudgeButton("yes", self.sid).callback(inter)
        self.assertEqual(sessions.get_rsvps(self.sid)["yes"], ["42"])
        refresh.assert_awaited_once()
        self.assertIn("In", inter.response.edit_message.await_args.kwargs["content"])
        await N.NudgeButton("stop", self.sid).callback(inter)
        self.assertTrue(N.opted_out("10", "42"))
        with connect() as conn:
            conn.execute("UPDATE sessions SET starts_at=? WHERE id=?", (self.now - 60, self.sid))
        await N.NudgeButton("no", self.sid).callback(inter)
        self.assertEqual(sessions.get_rsvps(self.sid)["yes"], ["42"])   # closed at kick-off: unchanged
        view = N.nudge_view(self.sid)
        self.assertEqual(len(view.children), 4)
