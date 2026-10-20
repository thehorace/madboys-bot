import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import db
import clubs
import match_data as md
from announce import announce_channel_id, set_announce_channel
from cogs import matchday, scout, sessions, session_reports, patchnotes


class AnnounceChannelTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(db, "DB_PATH", str(Path(temp.name) / "a.db"))
        p.start()
        self.addCleanup(p.stop)
        db.init_all()

    def test_every_scheduled_post_follows_announce_channel(self):
        db.set_setting("10", "matchday_channel", "1")
        db.set_setting("10", "session:channel_id", "2")
        db.set_setting("10", "reports:channel", "3")
        db.set_setting("10", "patchnotes:channel", "4")
        tracker = object.__new__(matchday.MatchdayCog)
        # before: each feature keeps its own channel
        self.assertEqual(tracker.channel_id_for("10"), 1)
        self.assertEqual(sessions.session_settings("10")["channel_id"], "2")
        self.assertEqual(session_reports.report_settings("10")["channel"], "3")
        self.assertEqual(patchnotes.patch_settings("10")["channel_id"], "4")
        # after /announce set: all of them move
        set_announce_channel("10", 99)
        self.assertEqual(tracker.channel_id_for("10"), 99)
        self.assertEqual(sessions.session_settings("10")["channel_id"], "99")
        self.assertEqual(session_reports.report_settings("10")["channel"], "99")
        self.assertEqual(patchnotes.patch_settings("10")["channel_id"], "99")
        # other servers are unaffected; clearing restores the old channels
        self.assertEqual(announce_channel_id("11"), None)
        set_announce_channel("10", None)
        self.assertEqual(tracker.channel_id_for("10"), 1)

    def test_unset_means_old_behaviour(self):
        self.assertIsNone(announce_channel_id("10"))
        self.assertEqual(announce_channel_id("10", "7"), "7")
        self.assertEqual(sessions.session_settings("10")["channel_id"], "")


class ScoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(db, "DB_PATH", str(Path(temp.name) / "s.db"))
        p.start()
        self.addCleanup(p.stop)
        db.init_all()
        self.us = clubs.monitored_clubs()[0]
        for mid, ts, goals in (("m1", 100, 3), ("m2", 200, 0)):
            raw = {"matchId": mid, "timestamp": ts,
                   "clubs": {str(self.us["club_id"]): {"goals": goals}, "555": {"goals": 1, "details": {"name": "Rivals_FC"}}},
                   "players": {str(self.us["club_id"]): {}}}
            md.store_match(self.us["club_id"], md.parse_match(raw, self.us["club_id"]), raw)
        self.ea = SimpleNamespace(
            get_overall_stats=AsyncMock(return_value={"wins": "5", "ties": "1", "losses": "2", "gamesPlayed": "8",
                                                     "goals": "20", "goalsAgainst": "9", "skillRating": "1500"}),
            get_member_stats=AsyncMock(return_value=[
                {"name": "Striker", "goals": "9", "assists": "2", "gamesPlayed": "8", "ratingAve": "7.9"},
                {"name": "Keeper", "goals": "0", "assists": "0", "gamesPlayed": "8", "ratingAve": "7.0"}]),
            get_club_info=AsyncMock(return_value={"name": "Rivals_FC"}), stale_note=lambda: None)

    def test_resolve_and_autocomplete(self):
        self.assertEqual(scout.resolve_club("555"), (555, "Rivals_FC"))
        self.assertEqual(scout.resolve_club("rivals_fc"), (555, "Rivals_FC"))
        self.assertEqual(scout.resolve_club("123456"), (123456, None))   # any EA id works
        self.assertEqual(scout.resolve_club("nobody"), (None, None))
        self.assertEqual([o["name"] for o in scout.opponent_rows("rival")], ["Rivals_FC"])
        self.assertEqual(scout.opponent_rows("100%"), [])                # LIKE wildcards are escaped

    async def test_report_has_stats_players_and_h2h(self):
        embed = await scout.build_scout(self.ea, 555, self.us["club_id"], "Rivals_FC")
        fields = {f.name: f.value for f in embed.fields}
        self.assertIn("W5 D1 L2", fields["Record"])
        self.assertIn("Striker", fields["Danger men (this season, G+A)"])
        self.assertTrue(any("W1 D0 L1 in 2" in v for v in fields.values()))

    async def test_unreachable_club_is_a_friendly_message(self):
        self.ea.get_overall_stats.return_value = None
        self.ea.get_member_stats.return_value = None
        self.assertIsInstance(await scout.build_scout(self.ea, 555), str)

    def test_button_survives_restart_via_custom_id(self):
        view = scout.scout_view("555", "Rivals_FC", self.us["club_id"])
        cid = view.children[0].custom_id
        self.assertRegex(cid, r"^scout:555:\d+$")
        self.assertIsNotNone(scout.ScoutButton.__discord_ui_compiled_template__.fullmatch(cid))
        self.assertIsNone(scout.scout_view(None, "x", 1))

    async def test_result_post_gets_the_button(self):
        channel = SimpleNamespace(id=20, send=AsyncMock(return_value=SimpleNamespace(id=1)))
        tracker = object.__new__(matchday.MatchdayCog)
        tracker.bot = SimpleNamespace(get_cog=lambda n: None)
        tracker._unlinked = {}
        raw = {"matchId": "m9", "timestamp": 300, "clubs": {str(self.us["club_id"]): {"goals": 2},
               "555": {"goals": 1, "details": {"name": "Rivals_FC"}}}, "players": {str(self.us["club_id"]): {}}}
        pm = md.parse_match(raw, self.us["club_id"])
        with clubs.club_scope(self.us):
            await tracker._post_results(channel, [pm])
        kwargs = channel.send.await_args_list[0].kwargs
        self.assertIsInstance(kwargs["view"], discord.ui.View)


class AnnounceByNameTests(AnnounceChannelTests):
    def test_channel_named_announcements_is_found_and_wins_over_old_channels(self):
        import announce
        mk = lambda i, n: SimpleNamespace(id=i, name=n)
        guild = SimpleNamespace(id=10, text_channels=[mk(1, "general"), mk(77, "Announcements")])
        self.addCleanup(announce._named.clear)
        self.assertEqual(announce.refresh_named(guild), "77")
        db.set_setting("10", "matchday_channel", "1")
        self.assertEqual(announce.announce_channel_id("10", "1"), "77")
        set_announce_channel("10", 5)                      # explicit /announce set still wins
        self.assertEqual(announce.announce_channel_id("10"), "5")
        set_announce_channel("10", None)
        guild.text_channels.append(mk(78, "announcements"))   # two matches = ambiguous -> ignored
        self.assertIsNone(announce.refresh_named(guild))
        self.assertEqual(announce.announce_channel_id("10", "1"), "1")
