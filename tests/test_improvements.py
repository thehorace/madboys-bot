import asyncio
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

import db
from cogs import sessions, usage, operations, onboarding, admin
from interaction_tracking import TrackedView, failed


class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = str(Path(self.temp.name) / "bot.db")
        self.patch = patch.object(db, "DB_PATH", self.database)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        db.init_all()
        usage.init_usage()

    def interaction(self, data=None, kind=discord.InteractionType.application_command, iid=100):
        return SimpleNamespace(id=iid, data=data or {"name": "lastgame"}, type=kind, extras={},
                               guild_id=10, channel_id=20, guild=SimpleNamespace(id=10),
                               user=SimpleNamespace(id=42, name="fauz", display_name="Fauz", bot=False),
                               message=None, response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()))


class SettingsTests(DatabaseCase):
    async def test_all_private_commands_reject_unlisted_server_owner(self):
        for cog_class, command, kwargs in (
            (sessions.SessionsCog, sessions.SessionsCog.settings, {"enabled": False}),
            (sessions.SessionsCog, sessions.SessionsCog.skip, {}),
            (usage.UsageCog, usage.UsageCog.usage, {}),
            (operations.OperationsCog, operations.OperationsCog.status, {}),
            (operations.OperationsCog, operations.OperationsCog.backup, {}),
        ):
            with self.subTest(command=command.name):
                interaction = self.interaction()
                interaction.user.name = "other_owner"
                interaction.guild.owner_id = interaction.user.id
                interaction.user.guild_permissions = SimpleNamespace(administrator=True, manage_channels=True)
                cog = object.__new__(cog_class)
                with patch.object(sessions, "is_manager", return_value=True):
                    await command.callback(cog, interaction, **kwargs)
                interaction.response.send_message.assert_awaited_once()
                self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])
        self.assertTrue(sessions.session_settings("10")["enabled"])

    async def test_old_database_migration_preserves_sessions_and_unknown_usage(self):
        with db.connect() as conn:
            conn.execute("DROP TABLE sessions")
            conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, guild_id TEXT, channel_id TEXT, message_id TEXT, "
                         "starts_at INTEGER, note TEXT, created_by TEXT, reminded INTEGER DEFAULT 0, cancelled INTEGER DEFAULT 0)")
            conn.execute("INSERT INTO sessions (id,guild_id,channel_id,message_id,starts_at,created_by) VALUES (1,'10','20','50',9999999999,'daily')")
            conn.execute("DROP TABLE usage_log")
            conn.execute("CREATE TABLE usage_log (id INTEGER PRIMARY KEY,ts INTEGER,guild_id TEXT,user_id TEXT,user_name TEXT,kind TEXT,action TEXT,detail TEXT,channel_id TEXT)")
            conn.execute("INSERT INTO usage_log VALUES (1,9999999999,'10','42','Fauz','button','Player stats','Killa','20')")
        db.init_all()
        usage.init_usage()
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=1").fetchone()
            self.assertEqual(row["message_id"], "50")
            self.assertEqual(row["sticky_at"], 0)
            self.assertEqual(row["started_shown"], 0)
        self.assertEqual(usage.rows_since("10", None), [])
        self.assertEqual(usage.rows_since("10", None, raw=True)[0]["outcome"], "unknown")

    async def test_permission_denial_does_not_change_settings(self):
        interaction = self.interaction()
        interaction.user.name = "someone_else"
        with patch.object(sessions, "is_manager", return_value=True):
            await sessions.SessionsCog.settings.callback(object.__new__(sessions.SessionsCog), interaction, enabled=False)
        self.assertTrue(sessions.session_settings("10")["enabled"])
        self.assertEqual(interaction.extras["usage_status"], "failed")

    async def test_settings_validate_atomically_and_persist(self):
        cog = object.__new__(sessions.SessionsCog)
        interaction = self.interaction()
        with patch.object(sessions, "is_manager", return_value=True):
            await sessions.SessionsCog.settings.callback(cog, interaction, post_time="7pm", kickoff_time="6pm", enabled=False)
            self.assertTrue(sessions.session_settings("10")["enabled"])
            await sessions.SessionsCog.settings.callback(cog, interaction, post_time="10am", kickoff_time="7pm",
                                                        days="monday,friday", cooldown=120, waitlist=False)
        saved = sessions.session_settings("10")
        self.assertEqual(saved["post_time"], "10:00")
        self.assertEqual(saved["kickoff_time"], "19:00")
        self.assertEqual(saved["days"], ["monday", "friday"])
        self.assertEqual(saved["cooldown"], 120)
        self.assertFalse(saved["waitlist"])
        now = datetime(2026, 10, 5, 10, tzinfo=ZoneInfo("Asia/Singapore"))
        self.assertIsNotNone(sessions.daily_start(now, saved))
        saved["skip_date"] = "2026-10-05"
        self.assertIsNone(sessions.daily_start(now, saved))
        saved["skip_date"] = ""
        self.assertIsNone(sessions.daily_start(now.replace(day=6), saved))

    async def test_skip_today_leaves_tomorrow_enabled(self):
        cog = object.__new__(sessions.SessionsCog)
        interaction = self.interaction()
        interaction.response.defer = AsyncMock()
        with patch.object(sessions, "is_manager", return_value=True):
            await sessions.SessionsCog.skip.callback(cog, interaction)
        settings = sessions.session_settings("10")
        self.assertEqual(settings["skip_date"], datetime.now(sessions._tz()).date().isoformat())
        self.assertTrue(settings["enabled"])

    async def test_waitlist_promotes_in_order_and_repeated_in_does_not_requeue(self):
        with db.connect() as conn:
            sid = conn.execute("INSERT INTO sessions (guild_id,channel_id,starts_at,created_by) VALUES ('10','20',9999999999,'daily')").lastrowid
        for i in range(13):
            sessions.set_rsvp(sid, str(i), "yes")
        before = sessions.get_rsvps(sid)
        self.assertEqual(len(before["yes"]), 11)
        self.assertEqual(before["waitlist"], ["11", "12"])
        sessions.set_rsvp(sid, "0", "yes")
        self.assertEqual(sessions.get_rsvps(sid), before)
        sessions.set_rsvp(sid, "0", "no")
        after = sessions.get_rsvps(sid)
        self.assertIn("11", after["yes"])
        self.assertEqual(after["waitlist"], ["12"])
        db.set_setting("10", "session:waitlist", "0")
        self.assertEqual(len(sessions.get_rsvps(sid)["yes"]), 12)

    async def test_sticky_cooldown_is_persistent(self):
        with db.connect() as conn:
            conn.execute("INSERT INTO sessions (guild_id,channel_id,message_id,starts_at,created_by,sticky_messages,sticky_at) "
                         "VALUES ('10','20','50',9999999999,'daily',9,?)", (int(time.time()),))
        cog = object.__new__(sessions.SessionsCog)
        cog.bot = SimpleNamespace(user=SimpleNamespace(id=999))
        cog._sticky_lock = asyncio.Lock()
        cog.repost_session = AsyncMock()
        msg = SimpleNamespace(id=100, guild=SimpleNamespace(id=10), channel=SimpleNamespace(id=20), author=SimpleNamespace(id=1))
        await cog.on_message(msg)
        cog.repost_session.assert_not_awaited()
        with db.connect() as conn:
            conn.execute("UPDATE sessions SET sticky_at=?", (int(time.time()) - 301,))
        await cog.on_message(msg)
        cog.repost_session.assert_awaited_once()


class UsageTests(DatabaseCase):
    async def test_completion_and_late_listener_do_not_duplicate_or_downgrade(self):
        interaction = self.interaction()
        usage.log_interaction(interaction, "success")
        usage.log_interaction(interaction, "pending")
        rows = usage.rows_since("10", None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "success")
        self.assertEqual(rows[0]["category"], "lookup")
        failed(interaction)
        usage.log_interaction(interaction, "failed")
        self.assertEqual(usage.rows_since("10", None), [])
        self.assertEqual(usage.rows_since("10", None, raw=True)[0]["outcome"], "failed")

    async def test_navigation_and_build_changes_are_not_searches(self):
        self.assertEqual(usage.category_for("Home", ""), "navigation")
        self.assertEqual(usage.category_for("/stats", ""), "navigation")
        self.assertEqual(usage.category_for("Set someone's builds", "CB, ST"), "action")
        self.assertEqual(usage.category_for("Player stats", "Killa"), "lookup")
        self.assertEqual(usage.category_for("/compare", "player_a=Killa"), "lookup")

    async def test_tracked_view_success_and_exception(self):
        view = TrackedView(timeout=None)
        item = discord.ui.Button(label="Home", custom_id="home")
        item.callback = AsyncMock()
        view.add_item(item)
        interaction = self.interaction({"custom_id": "home", "component_type": 2}, discord.InteractionType.component)
        await item.callback(interaction)
        self.assertEqual(usage.rows_since("10", None)[0]["outcome"], "success")
        bad = discord.ui.Button(label="Broken", custom_id="bad")
        bad.callback = AsyncMock(side_effect=RuntimeError("fail"))
        view.add_item(bad)
        other = self.interaction({"custom_id": "bad", "component_type": 2}, discord.InteractionType.component, 101)
        with self.assertRaises(RuntimeError):
            await bad.callback(other)
        self.assertEqual(len(usage.rows_since("10", None)), 1)
        self.assertEqual(usage.rows_since("10", None, raw=True)[-1]["outcome"], "failed")

    async def test_repeat_users_weekly_trends_and_report_layout(self):
        now = int(time.time())
        for iid, age in [(1, 0), (2, 86400), (3, 8 * 86400)]:
            interaction = self.interaction(iid=iid)
            with patch.object(usage.time, "time", return_value=now - age):
                usage.log_interaction(interaction, "success")
        rows = usage.rows_since("10", None)
        embed = usage.build_overview(rows, "all")
        self.assertIn("**1**", next(f.value for f in embed.fields if "Returning" in f.name))
        trend = usage.build_trends("10", now)
        self.assertIn("**2** vs 1", trend.fields[0].value)
        guild = SimpleNamespace(id=10)
        view = usage.UsageView(guild, self.interaction().user)
        view.build()
        self.assertLessEqual(sum(getattr(c, "row", None) == 0 for c in view.children), 5)
        self.assertTrue(any(c.label == "Trends" for c in view.children if isinstance(c, discord.ui.Button)))


class BackupAndAlertsTests(DatabaseCase):
    async def test_backup_contains_wal_and_retains_only_configured_copies(self):
        directory = Path(self.temp.name) / "backups"
        with db.connect() as conn:
            conn.execute("INSERT INTO settings VALUES ('10','test','preserved')")
            conn.commit()
            for date in ["2026-10-01", "2026-10-02", "2026-10-03"]:
                result = operations.backup_database(self.database, directory, date, keep=2)
        self.assertEqual(len(list(directory.glob("*.sqlite3"))), 2)
        with closing(sqlite3.connect(result)) as backup:
            self.assertEqual(backup.execute("SELECT value FROM settings WHERE key='test'").fetchone()[0], "preserved")
            self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertFalse(list(directory.glob("*.tmp")))

    async def test_alert_once_per_episode_and_recovery(self):
        with db.connect() as conn:
            conn.execute("CREATE TABLE service_health (guild_id TEXT, service TEXT, problem TEXT, changed_at INTEGER, notified INTEGER DEFAULT 0, PRIMARY KEY(guild_id,service))")
        cog = object.__new__(operations.OperationsCog)
        cog._health_lock = asyncio.Lock()
        recipient = SimpleNamespace(send=AsyncMock(), id=42)
        cog.recipients = AsyncMock(return_value=[recipient])
        await cog.health("10", "Tracker", "down")
        await cog.health("10", "Tracker", "still down")
        recipient.send.assert_awaited_once()
        await cog.health("10", "Tracker")
        await cog.health("10", "Tracker")
        self.assertEqual(recipient.send.await_count, 2)
        await cog.health("10", "Tracker", "new outage")
        self.assertEqual(recipient.send.await_count, 3)

    async def test_failed_backup_keeps_existing_copy(self):
        directory = Path(self.temp.name) / "backups"
        good = operations.backup_database(self.database, directory, "2026-10-03")
        original = good.read_bytes()
        with self.assertRaises(sqlite3.OperationalError):
            operations.backup_database(str(Path(self.temp.name) / "missing.db"), directory, "2026-10-03")
        self.assertEqual(good.read_bytes(), original)

    async def test_backup_permission_is_private(self):
        cog = object.__new__(operations.OperationsCog)
        cog.make_backup = AsyncMock()
        interaction = self.interaction()
        interaction.user.name = "someone_else"
        await operations.OperationsCog.backup.callback(cog, interaction)
        cog.make_backup.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()


class OnboardingTests(DatabaseCase):
    async def test_setup_resumes_and_saves_link_and_positions(self):
        user = self.interaction().user
        view = onboarding.SetupView(Mock(), user, "10", [f"Player{i}" for i in range(30)])
        self.assertEqual(view.step, 0)
        self.assertEqual(len(view.children[0].options), 25)
        self.assertTrue(any(c.label == "More players" for c in view.children if isinstance(c, discord.ui.Button)))
        interaction = self.interaction({"values": ["Player1"]}, discord.InteractionType.component)
        await view.pick_name(interaction)
        self.assertEqual(view.step, 1)
        resumed = onboarding.SetupView(Mock(), user, "10", [])
        self.assertEqual(resumed.step, 1)
        interaction.data = {"values": ["CB", "ST"]}
        await resumed.pick_positions(interaction)
        self.assertEqual(resumed.step, 2)
        ready = onboarding.SetupView(Mock(), user, "10", [])
        self.assertEqual(ready.step, 2)
        self.assertIn("CB, ST", ready.embed().description)

    async def test_all_cogs_load_with_commands_and_persistent_views(self):
        names = ["link", "rotation", "stats", "lineup", "sessions", "motm", "positions", "matchday", "hub", "misc", "usage", "operations", "onboarding", "admin", "patchnotes", "session_reports"]
        async with commands.Bot(command_prefix="!", intents=discord.Intents.none()) as bot:
            bot.ea = Mock()
            for name in names:
                await bot.load_extension("cogs." + name)
            self.assertIsNotNone(bot.tree.get_command("setup"))
            self.assertIsNotNone(bot.tree.get_command("maintenance"))
            self.assertIsNotNone(bot.tree.get_command("admin"))
            self.assertIsNotNone(bot.tree.get_command("mysession"))
            self.assertIsNotNone(bot.tree.get_command("sessionhistory"))
            group = bot.tree.get_command("session")
            self.assertIsNotNone(group.get_command("settings"))
            self.assertIsNotNone(group.get_command("skip"))
            for command in bot.tree.get_commands():
                command.to_dict(bot.tree)

    async def test_static_decorated_button_callbacks_are_tracked(self):
        class TestView(TrackedView):
            @discord.ui.button(label="Home", custom_id="home")
            async def home(self, interaction, button):
                pass
        view = TestView(timeout=None)
        interaction = self.interaction({"custom_id": "home", "component_type": 2}, discord.InteractionType.component)
        await view.children[0].callback(interaction)
        self.assertEqual(usage.rows_since("10", None)[0]["outcome"], "success")


class AdminPanelTests(DatabaseCase):
    def panel(self):
        interaction = self.interaction({"name": "admin"})
        interaction.edit_original_response = AsyncMock()
        sessions_cog = object.__new__(sessions.SessionsCog)
        sessions_cog.bot = Mock()
        bot = SimpleNamespace(get_cog=lambda name: sessions_cog if name == "SessionsCog" else None)
        return admin.AdminView(bot, interaction.user, interaction.guild, interaction), interaction

    async def test_admin_command_private_and_unlisted_owner_blocked(self):
        interaction = self.interaction({"name": "admin"})
        cog = admin.AdminCog(Mock())
        await admin.AdminCog.admin.callback(cog, interaction)
        sent = interaction.response.send_message.call_args.kwargs
        self.assertTrue(sent["ephemeral"])
        self.assertIsInstance(sent["view"], admin.AdminView)
        other = self.interaction({"name": "admin"})
        other.user.name = "owner"
        other.guild.owner_id = other.user.id
        await admin.AdminCog.admin.callback(cog, other)
        self.assertNotIn("view", other.response.send_message.call_args.kwargs)

    async def test_panel_ownership_and_allowlist_rechecked_on_buttons(self):
        panel, opening = self.panel()
        other = self.interaction()
        other.user.id = 43  # Even another allowed username cannot use this user's panel.
        await panel.act(other, "toggle_daily")
        self.assertTrue(sessions.session_settings("10")["enabled"])
        other.response.send_message.assert_awaited_once()
        revoked = self.interaction()
        revoked.user.name = "unlisted"
        await panel.act(revoked, "toggle_daily")
        self.assertTrue(sessions.session_settings("10")["enabled"])
        opening.edit_original_response.assert_not_awaited()

    async def test_toggles_use_saved_settings_and_refresh_original_panel(self):
        panel, opening = self.panel()
        await panel.act(self.interaction(), "sessions")
        self.assertEqual(panel.page, "sessions")
        await panel.act(self.interaction(), "toggle_daily")
        self.assertFalse(sessions.session_settings("10")["enabled"])
        await panel.act(self.interaction(), "toggle_waitlist")
        self.assertFalse(sessions.session_settings("10")["waitlist"])
        self.assertEqual(opening.edit_original_response.await_count, 2)

    async def test_schedule_modal_reuses_validation_and_blocks_unlisted_submitter(self):
        panel, opening = self.panel()
        modal = admin.ScheduleModal(panel)
        modal.post._value = "10am"
        modal.kickoff._value = "7pm"
        modal.days._value = "monday,friday"
        modal.cooldown._value = "90"
        await modal.on_submit(self.interaction())
        self.assertEqual(sessions.session_settings("10")["post_time"], "10:00")
        self.assertEqual(sessions.session_settings("10")["cooldown"], 90)
        modal.post._value = "8pm"  # Invalid: posting after kick-off.
        await modal.on_submit(self.interaction())
        self.assertEqual(sessions.session_settings("10")["post_time"], "10:00")
        other = self.interaction()
        other.user.name = "unlisted"
        modal.post._value = "9am"
        await modal.on_submit(other)
        self.assertEqual(sessions.session_settings("10")["post_time"], "10:00")

    async def test_admin_panel_browsing_is_excluded_from_usage(self):
        usage.log_interaction(self.interaction({"name": "admin"}), "success")
        usage.log_interaction(self.interaction({"custom_id": "admin:health", "component_type": 2},
                                               discord.InteractionType.component, 101), "success")
        self.assertEqual(usage.rows_since("10", None, raw=True), [])


if __name__ == "__main__":
    unittest.main()
