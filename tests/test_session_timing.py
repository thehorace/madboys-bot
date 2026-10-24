"""Tonight's case: forfeits mid-session, switching clubs, and games after a session closed."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import db
from cogs import session_reports as R
from match_data import VOID_SCHEMA

S = 1_800_000_000          # kick-off


class SessionTimingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(db, "DB_PATH", str(Path(temp.name) / "t.db"))
        p.start()
        self.addCleanup(p.stop)
        db.init_all()
        db.set_setting("10", "reports:since", str(S - 100))
        db.set_setting("10", "reports:gap_v30", "1")
        with db.connect() as conn:
            conn.executescript(VOID_SCHEMA)
            self.sid = conn.execute("INSERT INTO sessions (guild_id,channel_id,message_id,starts_at,created_by) "
                                    "VALUES ('10','1','5',?,'daily')", (S,)).lastrowid

    def game(self, mid, ts):
        with db.connect() as conn:
            conn.execute("INSERT INTO matches (club_id,match_id,match_type,ts,our_goals,opp_goals,result,stored_at) "
                         "VALUES (?,?,'leagueMatch',?,2,1,'W','now')", (R.CLUB_ID, mid, ts))
            conn.execute("INSERT INTO match_players (club_id,match_id,persona_id,name,rating) VALUES (?,?,'p','Ali',7)",
                         (R.CLUB_ID, mid))

    def void(self, mid, ts):
        with db.connect() as conn:
            conn.execute("INSERT INTO void_matches VALUES (?,?,?,0,3,'FLB','forfeit','{}')", (R.CLUB_ID, mid, ts))

    def done(self):
        return [r for r in R.history("10") if r["outcome"] == "completed"]

    def test_forfeit_keeps_the_session_open_and_club_switch_stays_in_one_session(self):
        self.game("krix", S + 1700)          # ~5:58
        self.void("ff", S + 2460)            # 6:11 forfeit
        R.archive_sessions("10", S + 3720)   # 6:32: this is when it closed tonight
        self.assertEqual(self.done(), [])
        for i, t in enumerate((4320, 5640, 6900, 8220, 9480, 10080)):   # MADBOYS games 6:42 → 8:18
            self.game(f"mb{i}", S + t)
        R.archive_sessions("10", S + 10080 + 1700)   # 28 min after the last game: still open
        self.assertEqual(self.done(), [])
        R.archive_sessions("10", S + 10080 + 1810)   # 30 min: one session with every real game
        ids = json.loads(self.done()[0]["match_ids"])
        self.assertEqual(len(ids), 7)
        self.assertNotIn("ff", " ".join(ids))

    def test_still_in_voice_waits(self):
        self.game("a", S + 600)
        R.archive_sessions("10", S + 600 + 1900, in_voice=True)
        self.assertEqual(self.done(), [])
        R.archive_sessions("10", S + 600 + 1900, in_voice=False)
        self.assertEqual(len(self.done()), 1)

    def test_long_break_still_splits(self):
        self.game("a", S + 600)
        self.game("b", S + 600 + 2700 + 60)   # 46 min after: a real break
        R.archive_sessions("10", S + 9000)
        self.assertEqual(json.loads(self.done()[0]["match_ids"]), ["a"])

    def test_games_after_a_closed_session_get_their_own_recap(self):
        self.game("a", S + 600)
        R.archive_sessions("10", S + 3000)           # first session closes
        self.game("late1", S + 6000)
        self.game("late2", S + 7000)
        R.add_followup_sessions("10", S + 7100)
        R.add_followup_sessions("10", S + 7200)      # no second auto session for the same games
        with db.connect() as conn:
            autos = conn.execute("SELECT COUNT(*) FROM sessions WHERE created_by='auto'").fetchone()[0]
        self.assertEqual(autos, 1)
        R.archive_sessions("10", S + 7000 + 1900)
        done = self.done()
        self.assertEqual(len(done), 2)
        self.assertEqual(sorted(json.loads(done[0]["match_ids"]) + json.loads(done[1]["match_ids"])),
                         ["a", "late1", "late2"])
        self.assertIsNone([r for r in done if "late1" in r["match_ids"]][0]["message_id"])   # will be posted

    def test_squad_in_voice(self):
        from cogs.link import set_link
        set_link("10", "1", "Ali", "self")
        set_link("10", "2", "Bob", "self")
        m = lambda i: SimpleNamespace(id=i, bot=False)
        guild = SimpleNamespace(id=10, voice_channels=[SimpleNamespace(members=[m(1), m(2)])])
        self.assertTrue(R.squad_in_voice(guild))
        guild.voice_channels[0].members = [m(1), m(9)]
        self.assertFalse(R.squad_in_voice(guild))


class TonightEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Session #24 already posted (kriX only), then six MADBOYS games: the update must post their recap + POTD."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(db, "DB_PATH", str(Path(temp.name) / "e.db"))
        p.start()
        self.addCleanup(p.stop)
        db.init_all()
        from unittest.mock import AsyncMock
        from cogs.link import set_link
        db.set_setting("10", "reports:since", str(S - 86400))
        db.set_setting("10", "reports:gap_v30", "1")
        set_link("10", "42", "fauzan0410", "self")
        with db.connect() as conn:
            conn.executescript(VOID_SCHEMA)
            sid = conn.execute("INSERT INTO sessions (guild_id,channel_id,message_id,starts_at,created_by) "
                               "VALUES ('10','1','5',?,'daily')", (S,)).lastrowid
            conn.execute("INSERT INTO matches (club_id,match_id,match_type,ts,our_goals,opp_goals,result,stored_at) "
                         "VALUES (?,?,'leagueMatch',?,1,2,'L','now')", (R.CLUB_ID, "krix", S + 1700))
            conn.execute("INSERT INTO session_history VALUES (?,?,?,?,?,?,?,?)",
                         (sid, "10", S + 1700, "completed", json.dumps([R.match_key("krix", R.CLUB_ID)]), "[]", "[]", "99"))
            for i, t in enumerate((4320, 5640, 6900, 8220, 9480, 10080)):
                conn.execute("INSERT INTO matches (club_id,match_id,match_type,ts,our_goals,opp_goals,opp_name,result,stored_at) "
                             "VALUES (?,?,'leagueMatch',?,3,1,'Opp','W','now')", (R.CLUB_ID, f"mb{i}", S + t))
                for name, rating, goals in (("fauzan0410", 8.4, 2), ("thehorace", 7.0, 0)):
                    conn.execute("INSERT INTO match_players (club_id,match_id,persona_id,name,rating,goals,pos) "
                                 "VALUES (?,?,?,?,?,?,'forward')", (R.CLUB_ID, f"mb{i}", name, name, rating, goals))
        self.channel = SimpleNamespace(id=1, name="general", send=AsyncMock(return_value=SimpleNamespace(id=500)))
        self.member = SimpleNamespace(id=42, send=AsyncMock(return_value=SimpleNamespace(id=7)))
        self.guild = SimpleNamespace(id=10, text_channels=[self.channel], voice_channels=[],
                                     get_channel=lambda c: self.channel if c == 1 else None,
                                     get_member=lambda u: self.member if u == 42 else None, fetch_member=AsyncMock())

    async def test_recap_and_player_of_the_day_post(self):
        from unittest.mock import AsyncMock
        cog = R.SessionReportsCog(SimpleNamespace(get_cog=lambda n: None))
        now = S + 10080 + 80 * 60          # ~9:40pm
        with patch.object(R.time, "time", return_value=now), patch("cogs.session_reports.report_health", AsyncMock()), \
                patch("cogs.sessions._tz", return_value=None):
            await cog.after_poll(self.guild)
            await cog.after_poll(self.guild)          # next check: nothing posted twice
        sent = self.channel.send.await_args_list
        self.assertEqual(len(sent), 2)                                  # recap + Player of the Day
        self.assertIn("6 matches", sent[0].kwargs["embed"].description)
        self.assertIn("<@42>", sent[1].kwargs["embed"].description)      # fauzan0410 is Player of the Day
        self.member.send.assert_awaited_once()                          # personal summary DM
