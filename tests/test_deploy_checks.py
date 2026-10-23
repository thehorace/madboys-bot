"""Running-version notice and the settings self-check."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import db
import deploy_checks as D


class SettingsCheckTests(unittest.TestCase):
    def check(self, env, **kw):
        base = {"MIDDLEWARE_URL": "https://relay"}
        with patch.dict(os.environ, {**base, **env}, clear=True):
            return D.settings_problems(**{"db_path": "/data/madboys.db", **kw})

    def test_all_good(self):
        self.assertEqual(self.check({"RAILWAY_VOLUME_MOUNT_PATH": "/data"}), [])

    def test_missing_relay_url(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn("MIDDLEWARE_URL", D.settings_problems(db_path="x.db")[0])

    def test_database_off_the_volume(self):
        problems = self.check({"RAILWAY_VOLUME_MOUNT_PATH": "/data"}, db_path="madboys.db")
        self.assertEqual(len(problems), 1)
        self.assertIn("/data/madboys.db", problems[0])

    def test_rejected_key_only_while_still_failing(self):
        ea = SimpleNamespace(last_error="HTTP 401 — bad key", last_error_at=200, last_ok_at=100)
        self.assertIn("MIDDLEWARE_API_KEY", self.check({}, ea=ea)[0])
        ea.last_ok_at = 300   # worked again since
        self.assertEqual(self.check({}, ea=ea), [])
        ea = SimpleNamespace(last_error="timed out", last_error_at=200, last_ok_at=100)
        self.assertEqual(self.check({}, ea=ea), [])

    def test_channel_ids(self):
        guild = SimpleNamespace(get_channel=lambda cid: object() if cid == 1 else None)
        problems = self.check({"MATCHDAY_CHANNEL_ID": "1", "SESSION_CHANNEL_ID": " #22 ", "ANNOUNCE_CHANNEL_ID": "3"},
                              guild=guild)
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("SESSION_CHANNEL_ID" in p and "number" in p for p in problems))
        self.assertTrue(any("ANNOUNCE_CHANNEL_ID" in p and "can't see" in p for p in problems))

    def test_version_from_railway(self):
        D._version.cache_clear()
        self.addCleanup(D._version.cache_clear)
        with patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": "54a2feb0123", "RAILWAY_GIT_COMMIT_MESSAGE": "Speed up\n\nmore"}):
            self.assertEqual(D.running_version(), {"sha": "54a2feb", "message": "Speed up"})
            self.assertIn("`54a2feb` — Speed up", D.version_line())


class VersionNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(db, "DB_PATH", str(Path(temp.name) / "t.db"))
        p.start()
        self.addCleanup(p.stop)
        db.init_all()

    async def test_new_version_announced_once(self):
        from cogs.operations import OperationsCog
        cog = OperationsCog.__new__(OperationsCog)
        user = SimpleNamespace(id=1, send=AsyncMock())
        cog.recipients = AsyncMock(return_value=[user])
        with patch("cogs.operations.running_version", return_value={"sha": "abc1234", "message": "Fixes"}):
            await cog.announce_version("10")
            await cog.announce_version("10")
        user.send.assert_awaited_once()
        self.assertIn("abc1234", user.send.await_args.args[0])
        with patch("cogs.operations.running_version", return_value={"sha": "", "message": ""}):
            await cog.announce_version("10")   # unknown version: say nothing
        user.send.assert_awaited_once()
