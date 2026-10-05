import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import db
import discord
from cogs import usage
from cogs import session_reports as reports, matchday, admin, operations
from cogs.link import set_link


class SessionReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = str(Path(self.temp.name) / "bot.db")
        self.patcher = patch.object(db, "DB_PATH", self.database)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        db.init_all()
        usage.init_usage()
        self.start = 1_800_000_000
        db.set_setting("10", "reports:since", str(self.start - 100))
        with db.connect() as conn:
            self.sid = conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by) VALUES ('10','20',?,'daily')", (self.start,)).lastrowid
            conn.execute("INSERT INTO session_rsvps VALUES (?,'42','yes','now','button')", (self.sid,))
            conn.execute("INSERT INTO session_rsvps VALUES (?,'43','yes','now','button')", (self.sid,))
        set_link("10", "42", "Fauz", "self")
        self.channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=99)))
        self.guild = SimpleNamespace(id=10, get_channel=lambda cid: self.channel if cid == 20 else None)
        self.bot = SimpleNamespace(get_cog=lambda name: None)
        self.cog = reports.SessionReportsCog(self.bot)

    def game(self, mid, ts, name="Fauz", rating=8.5):
        with db.connect() as conn:
            conn.execute("INSERT INTO matches (club_id,match_id,match_type,ts,our_goals,opp_goals,result,stored_at) VALUES (?,?,'leagueMatch',?,2,1,'W','now')", (reports.CLUB_ID, mid, ts))
            conn.execute("INSERT INTO match_players (club_id,match_id,persona_id,name,goals,assists,rating,motm,saves,tackles_made,passes_made,pass_attempts) VALUES (?,?,'p',?,1,1,?,1,0,3,8,10)", (reports.CLUB_ID, mid, name, rating))

    def interaction(self, user=42, username="fauz"):
        return SimpleNamespace(id=500, type=discord.InteractionType.component,
            user=SimpleNamespace(id=user, name=username, display_name=username, bot=False), guild=self.guild,
            guild_id=10, channel_id=20, message=SimpleNamespace(id=99), data={"custom_id": "sessions:personal-summary", "component_type": 2}, extras={},
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))

    async def poll(self, now):
        with patch.object(reports.time, "time", return_value=now):
            await self.cog.after_poll(self.guild)

    async def test_gap_resets_and_recap_sent_once_across_restart(self):
        self.game("a", self.start + 600)
        await self.poll(self.start + 7700)
        self.channel.send.assert_not_awaited()
        self.game("b", self.start + 7500)
        await self.poll(self.start + 8000)
        self.channel.send.assert_not_awaited()
        await self.poll(self.start + 14700)
        self.channel.send.assert_awaited_once()
        self.assertEqual(len(json.loads(reports.history("10")[0]["match_ids"])), 2)
        self.cog = reports.SessionReportsCog(self.bot)
        await self.poll(self.start + 15000)
        self.channel.send.assert_awaited_once()
        self.assertFalse(self.channel.send.call_args.kwargs["allowed_mentions"].everyone)

    async def test_personal_summary_uses_actual_matches_and_stays_private(self):
        self.game("a", self.start + 600)
        await self.poll(self.start + 8000)
        record = reports.history("10")[0]
        embed = reports.recap_embed(record, "42")
        self.assertIn("1 goals", embed.fields[0].value)
        self.assertIn("80%", embed.fields[0].value)
        self.assertNotIn("1 goals", reports.recap_embed(record, "43").fields[0].value)
        view = reports.SummaryView()  # New persistent view resolves saved message -> exact session.
        i = self.interaction()
        await view.children[0].callback(i)
        self.assertTrue(i.response.send_message.call_args.kwargs["ephemeral"])
        self.assertEqual(len(json.loads(record["players"])), 1)
        self.assertEqual(len(json.loads(record["rsvps"])), 2)

    async def test_snapshot_not_changed_by_later_link_or_rsvp_edits(self):
        self.game("a", self.start + 600)
        await self.poll(self.start + 8000)
        set_link("10", "42", "SomeoneElse", "self")
        with db.connect() as conn:
            conn.execute("DELETE FROM session_rsvps WHERE session_id=?", (self.sid,))
        record = reports.history("10")[0]
        self.assertEqual(len(json.loads(record["rsvps"])), 2)
        self.assertIn("1 goals", reports.recap_embed(record, "42").fields[0].value)

    async def test_failed_send_retries_without_rearchiving(self):
        self.game("a", self.start + 600)
        self.channel.send.side_effect = RuntimeError("Discord unavailable")
        with self.assertRaises(RuntimeError):
            await self.poll(self.start + 8000)
        self.assertIsNone(reports.history("10")[0]["message_id"])
        self.channel.send.side_effect = None
        await self.poll(self.start + 8100)
        self.assertEqual(reports.history("10")[0]["message_id"], "99")
        self.assertEqual(len(reports.history("10")), 1)

    async def test_first_check_old_sessions_silent_and_no_game_history_saved(self):
        db.set_setting("10", "reports:since", None)
        self.game("a", self.start + 600)
        await self.poll(self.start + 86400)
        self.channel.send.assert_not_awaited()
        self.assertEqual(reports.history("10")[0]["message_id"], "suppressed")
        with db.connect() as conn:
            conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by) VALUES ('10','20',?,'daily')", (self.start + 90000,))
        await self.poll(self.start + 98000)
        self.assertEqual(reports.history("10")[0]["outcome"], "no games")
        self.channel.send.assert_not_awaited()

    async def test_disabled_recaps_save_history_without_later_spam(self):
        db.set_setting("10", "reports:enabled", "0")
        self.game("a", self.start + 600)
        await self.poll(self.start + 8000)
        self.channel.send.assert_not_awaited()
        self.assertEqual(reports.history("10")[0]["outcome"], "completed")
        db.set_setting("10", "reports:enabled", "1")
        await self.poll(self.start + 8100)
        self.channel.send.assert_not_awaited()

    async def test_configurable_gap_and_cancelled_session(self):
        db.set_setting("10", "reports:gap", "60")
        self.game("a", self.start + 600)
        await self.poll(self.start + 4200)
        self.channel.send.assert_awaited_once()
        with db.connect() as conn:
            conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by,cancelled) VALUES ('10','20',?,'daily',1)", (self.start + 5000,))
        await self.poll(self.start + 6000)
        self.assertEqual(reports.history("10")[0]["outcome"], "cancelled")
        self.channel.send.assert_awaited_once()

    async def test_later_game_after_inactivity_not_included_and_not_double_counted(self):
        self.game("a", self.start + 600)
        self.game("b", self.start + 8000)
        with db.connect() as conn:
            conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by) VALUES ('10','20',?,'manual')", (self.start,))
        await self.poll(self.start + 16000)
        completed = [r for r in reports.history("10") if r["outcome"] == "completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(json.loads(completed[0]["match_ids"]), ["a"])

    async def test_tracker_never_finishes_session_on_failed_ea_check(self):
        tracker = object.__new__(matchday.MatchdayCog)
        reporter = SimpleNamespace(after_poll=AsyncMock())
        tracker.bot = SimpleNamespace(get_cog=lambda name: reporter)
        tracker.home_guild = lambda: self.guild
        tracker.last_poll_ok = False
        tracker._poll_once = AsyncMock(return_value=0)
        await tracker.poll_once()
        reporter.after_poll.assert_not_awaited()
        tracker.last_poll_ok = True
        await tracker.poll_once()
        reporter.after_poll.assert_awaited_once_with(self.guild)

    async def test_private_settings_and_history_enforce_owner_and_allowlist(self):
        anchor = SimpleNamespace(edit_original_response=AsyncMock())
        panel = admin.AdminView(self.bot, self.interaction().user, self.guild, anchor)
        for action in ("gap60", "toggle_reports", "toggle_tracker"):
            await panel.act(self.interaction(43, "other"), action)
        self.assertEqual(reports.report_settings("10")["gap"], 120)
        self.assertTrue(reports.report_settings("10")["enabled"])
        await panel.act(self.interaction(), "gap60")
        self.assertEqual(reports.report_settings("10")["gap"], 60)
        self.game("a", self.start + 600)
        await self.poll(self.start + 8000)
        view = reports.HistoryView(42, "10", reports.history("10"), private=True)
        self.assertFalse(await view.interaction_check(self.interaction(43, "other")))
        self.assertFalse(await view.interaction_check(self.interaction(42, "revoked")))

    async def test_status_unknown_never_claims_tracker_is_healthy(self):
        tracker = SimpleNamespace(last_poll_ok=None, last_poll_at=None, last_success_at=None, next_poll_at=0,
            ticker=SimpleNamespace(is_running=lambda: True), channel_id_for=lambda gid: None, posting_enabled=lambda gid: True)
        bot = SimpleNamespace(get_cog=lambda name: tracker if name == "MatchdayCog" else None)
        embed = operations.status_embed(bot, "10")
        self.assertIn("Waiting for first check", embed.fields[0].value)
        self.assertIn("Not checked yet", embed.fields[0].value)

    async def test_history_pages_older_records_and_is_guild_scoped(self):
        with db.connect() as conn:
            for i in range(25):
                sid = conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by) VALUES ('10','20',?,'daily')", (self.start + i * 86400,)).lastrowid
                conn.execute("INSERT INTO session_history VALUES (?,'10',?,'cancelled','[]','[]','[]','suppressed')", (sid, self.start + i * 86400))
        records = reports.history("10")
        self.assertEqual(len(records), 20)
        view = reports.HistoryView(42, "10", records)
        interaction = self.interaction()
        await view.older.callback(interaction)
        next_view = interaction.response.edit_message.call_args.kwargs["view"]
        self.assertEqual(next_view.offset, 20)
        self.assertEqual(len(reports.history("10", offset=20)), 5)
        self.assertEqual(reports.history("other-guild"), [])
        self.assertEqual(reports.history("other-guild", records[0]["session_id"]), [])

    async def test_next_post_respects_timezone_weekdays_skip_and_existing_post(self):
        db.set_setting("10", "session:timezone", "Asia/Singapore")
        db.set_setting("10", "session:enabled", "1")
        db.set_setting("10", "session:days", "monday,tuesday")
        now = datetime(2026, 10, 5, 10, tzinfo=ZoneInfo("Asia/Singapore"))
        expected = now.replace(hour=11)
        self.assertEqual(operations.next_session_post("10", now), int(expected.timestamp()))
        db.set_setting("10", "session:skip_date", "2026-10-05")
        self.assertEqual(operations.next_session_post("10", now), int(expected.timestamp()) + 86400)
        db.set_setting("10", "session:enabled", "0")
        self.assertIsNone(operations.next_session_post("10", now))
