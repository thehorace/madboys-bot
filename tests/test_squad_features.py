"""Partnerships, attendance, badges (incl. nicknames) and monthly awards, against a real SQLite DB."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import db
import awards as A
import badges as B
import insights as I
from config import CLUB_ID
from cogs.link import set_link

SYD = ZoneInfo("Australia/Sydney")


def ts(y, m, d, h=20):
    return int(datetime(y, m, d, h, tzinfo=SYD).timestamp())


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(db, "DB_PATH", str(Path(temp.name) / "t.db"))
        p.start()
        self.addCleanup(p.stop)
        db.init_all()
        B.init_badges()
        A.init_awards()
        self.n = 0

    def game(self, when, result="W", players=(), gf=2, ga=1, club=CLUB_ID, opp="Rivals"):
        """players: (name, dict of stats). Inserts one match."""
        self.n += 1
        mid = f"m{self.n}"
        with db.connect() as conn:
            conn.execute("INSERT INTO matches (club_id,match_id,match_type,ts,our_goals,opp_goals,opp_name,result,stored_at) "
                         "VALUES (?,?,?,?,?,?,?,?,'x')", (club, mid, "leagueMatch", when, gf, ga, opp, result))
            for i, (name, s) in enumerate(players):
                conn.execute("INSERT INTO match_players (club_id,match_id,persona_id,name,pos,goals,assists,rating,saves,"
                             "clean_sheet) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (club, mid, f"{name}{i}", name, s.get("pos", "midfielder"), s.get("g", 0), s.get("a", 0),
                              s.get("r", 7.0), s.get("sv", 0), s.get("cs", 0)))
        return mid


class PartnershipTests(Base):
    async def test_best_pairs_rank_by_win_rate_with_minimum_games(self):
        for i in range(4):   # Ali+Zak win every game together
            self.game(ts(2026, 10, 1 + i), "W", [("Ali", {"pos": "defender"}), ("Zak", {"pos": "defender"})])
        for i in range(4):   # Ali+Bob lose
            self.game(ts(2026, 10, 10 + i), "L", [("Ali", {}), ("Bob", {})], gf=0, ga=2)
        self.game(ts(2026, 10, 20), "W", [("Cat", {}), ("Dan", {})])   # only 1 game: not ranked
        rows = I.best_partnerships(CLUB_ID)
        self.assertEqual([(r["a"], r["b"]) for r in rows][:2], [("Ali", "Zak"), ("Ali", "Bob")])
        self.assertNotIn(("Cat", "Dan"), [(r["a"], r["b"]) for r in rows])
        embed = I.build_partnerships()
        self.assertIn("**Ali** (DEF) + **Zak** (DEF)", embed.description)
        self.assertIn("100% wins", embed.description)
        mine = I.partners_of("ali", CLUB_ID)
        self.assertEqual(mine[0]["partner"], "Zak")
        self.assertIsInstance(I.build_partnerships("Nobody"), str)


class AttendanceTests(Base):
    def session(self, sid, starts, rsvps, played_ids, outcome="completed"):
        with db.connect() as conn:
            conn.execute("INSERT INTO sessions (id,guild_id,channel_id,starts_at,created_by) VALUES (?,?,?,?,?)",
                         (sid, "10", "20", starts, "daily"))
            conn.execute("INSERT INTO session_history VALUES (?,?,?,?,?,?,?,?)",
                         (sid, "10", starts, outcome, "[]", json.dumps(rsvps),
                          json.dumps([{"name": "x", "discord_ids": played_ids}]), "suppressed"))

    async def test_sign_ups_vs_playing(self):
        now = int(datetime.now().timestamp())
        self.session(1, now - 86400, [{"discord_id": "1", "status": "yes"}, {"discord_id": "2", "status": "yes"},
                                      {"discord_id": "3", "status": "yes", "source": "voice"}], ["1", "3", "4"])
        self.session(2, now - 2 * 86400, [{"discord_id": "1", "status": "yes"}], ["1"])
        self.session(3, now - 3 * 86400, [{"discord_id": "2", "status": "yes"}], [], outcome="cancelled")
        s = I.attendance("10", "30")
        self.assertEqual(s["1"], {"said_in": 2, "played": 2, "showed": 2, "no_show": 0, "surprise": 0})
        self.assertEqual(s["2"]["no_show"], 1)              # cancelled sessions don't count
        self.assertEqual(s["3"]["surprise"], 1)             # joined voice != promised
        self.assertEqual(s["4"]["surprise"], 1)
        guild = SimpleNamespace(id=10, get_member=lambda _: None,
                                fetch_member=AsyncMock(return_value=SimpleNamespace(display_name="P")))
        embed = await I.build_attendance(guild, "30", me="1")
        self.assertIn("100% showed", embed.description)
        self.assertIn("▶️", embed.description)


class BadgeRuleTests(Base):
    async def test_rules(self):
        base = ts(2026, 10, 1)
        self.game(base, players=[("Fauz", {"g": 3, "a": 3, "r": 9.2})])           # hat-trick, playmaker, star
        for i in range(5):                                                          # scored in 5 in a row
            self.game(base + (i + 1) * 3600, players=[("Fauz", {"g": 1})])
        for i in range(3):                                                          # 3 clean sheets in a row
            self.game(base + (i + 10) * 3600, players=[("Fauz", {"pos": "defender", "cs": 1})])
        self.game(base + 20 * 3600, players=[("Fauz", {"pos": "goalkeeper", "sv": 9})])
        got = B.evaluate("10", "Fauz")
        self.assertEqual(set(got), {"hat_trick", "playmaker", "star_man", "on_fire", "brick_wall", "safe_hands"})
        self.assertEqual(got["on_fire"]["count"], 1)       # once per run, not every game after
        with db.connect() as conn:
            for i in range(5):
                conn.execute("INSERT INTO motm_polls (match_id,guild_id,channel_id,title,candidates,closes_at,closed,winners)"
                             " VALUES (?,?,?,?,?,?,1,?)", (f"p{i}", "10", "20", "t", "[]", base + i, '["fauz"]'))
        self.assertIn("fan_favourite", B.evaluate("10", "Fauz"))

    async def test_first_check_is_quiet_then_new_badges_announce(self):
        self.game(ts(2026, 10, 1), players=[("Fauz", {"g": 3})])
        changed, quiet = B.refresh("10", "42", "Fauz")
        self.assertTrue(quiet and changed)
        self.assertEqual(B.refresh("10", "42", "Fauz")[0], [])        # nothing new
        self.game(ts(2026, 10, 2), players=[("Fauz", {"g": 4})])
        changed, quiet = B.refresh("10", "42", "Fauz")
        self.assertFalse(quiet)
        self.assertEqual([(k, v["count"]) for k, v in changed], [("hat_trick", 2)])
        self.assertEqual(B.display_badge("10", "42"), "hat_trick")
        B.set_user("10", "42", show="none")
        self.assertIsNone(B.display_badge("10", "42"))

    def test_nicknames_only_touch_the_bots_own_emoji(self):
        self.assertEqual(B.nickname_for(None, "Fauz", "🎩"), "Fauz 🎩")
        self.assertEqual(B.nickname_for("Fauz 🎩", "Fauz", "🔥", applied="🎩"), "Fauz 🔥")      # swaps, doesn't stack
        self.assertEqual(B.nickname_for("Fauz 🗳️", "Fauz", None, applied="🗳️"), None)         # back to account name
        self.assertEqual(B.nickname_for("Big Fauz 🎩", "Fauz", None, applied="🎩"), "Big Fauz")
        # people's own emojis are never removed, even ones that look like badges
        self.assertEqual(B.nickname_for("Fauz 🔥", "Fauz", None), "Fauz 🔥")
        self.assertEqual(B.nickname_for("Fauz 💯🔥", "Fauz", "🎩"), "Fauz 💯🔥 🎩")
        self.assertEqual(B.nickname_for("Fauz 🔥 🎩", "Fauz", None, applied="🎩"), "Fauz 🔥")
        self.assertIsNone(B.nickname_for(None, "Fauz🔥", None))                            # no nick: stays none
        self.assertEqual(B.nickname_for("🔥", "acct", None), "🔥")
        long = B.nickname_for("X" * 40, "acct", "🗳️")
        self.assertLessEqual(len(long.encode("utf-16-le")) // 2, 32)                     # Discord's limit
        self.assertTrue(long.endswith(" 🗳️"))

    async def test_relinking_to_another_name_starts_again_quietly(self):
        self.game(ts(2026, 10, 1), players=[("P1", {"g": 3, "a": 3})])
        self.game(ts(2026, 10, 2), players=[("P2", {"r": 9.5})])
        B.refresh("10", "42", "P1")
        changed, quiet = B.refresh("10", "42", "P2")
        self.assertTrue(quiet)                                       # P2's history isn't announced
        self.assertEqual(set(B.stored("10", "42")), {"star_man"})    # P1's badges are gone


class FakeMember:
    def __init__(self, uid, guild, nick=None, top=1):
        self.id, self.guild, self.nick, self.name, self.global_name = uid, guild, nick, f"user{uid}", f"User{uid}"
        self.top_role, self.roles, self.bot = top, [], False
        self.display_name = nick or self.global_name
        self.edit = AsyncMock(side_effect=self._edit)

    async def _edit(self, nick=None, reason=None):
        self.nick = nick


class BadgeCogTests(Base):
    async def test_new_badge_announced_and_nickname_updated(self):
        from cogs.badges import BadgesCog
        sent = []
        channel = SimpleNamespace(send=AsyncMock(side_effect=lambda *a, **k: sent.append(a[0] if a else k)))
        guild = SimpleNamespace(id=10, owner_id=1)
        guild.me = SimpleNamespace(guild_permissions=SimpleNamespace(manage_nicknames=True), top_role=5)
        member = FakeMember(42, guild)
        guild.get_member = lambda uid: member if uid == 42 else None
        set_link("10", "42", "Fauz", "self")
        cog = object.__new__(BadgesCog)
        cog.bot, cog._lock = SimpleNamespace(get_cog=lambda n: None), __import__("asyncio").Lock()
        cog._channel = AsyncMock(return_value=channel)
        m1 = self.game(ts(2026, 10, 1), players=[("Fauz", {"g": 1})])
        await cog.after_matches(guild, [SimpleNamespace(players=[SimpleNamespace(name="Fauz")])])   # seeds quietly
        self.assertEqual(sent, [])
        self.game(ts(2026, 10, 2), players=[("Fauz", {"g": 3})])
        await cog.after_matches(guild, [SimpleNamespace(players=[SimpleNamespace(name="Fauz")])])
        self.assertEqual(len(sent), 1)
        self.assertIn("Hat-trick Hero", sent[0])
        self.assertEqual(member.nick, "User42 🎩")
        owner = FakeMember(1, guild)                      # the server owner can't be renamed
        with db.connect() as conn:
            conn.execute("INSERT INTO player_badges VALUES ('10','1','hat_trick',1,1,'x')")
        self.assertIn("server owner", await cog.apply_nickname(owner) or "")
        del m1


class BadgeSafetyTests(Base):
    def cog(self, channel):
        from cogs.badges import BadgesCog
        cog = object.__new__(BadgesCog)
        cog.bot, cog._lock = SimpleNamespace(get_cog=lambda n: None), __import__("asyncio").Lock()
        cog._channel = AsyncMock(return_value=channel)
        return cog

    def guild(self, *members):
        guild = SimpleNamespace(id=10, owner_id=1)
        guild.me = SimpleNamespace(guild_permissions=SimpleNamespace(manage_nicknames=True), top_role=5)
        for m in members:
            m.guild = guild
        guild.get_member = lambda uid: next((m for m in members if m.id == uid), None)
        return guild

    async def test_opted_out_and_own_emojis_are_left_alone(self):
        member = FakeMember(42, None, nick="Fauz 🔥")
        guild = self.guild(member)
        set_link("10", "42", "Fauz", "self")
        B.set_user("10", "42", show="none")
        cog = self.cog(SimpleNamespace(send=AsyncMock()))
        self.game(ts(2026, 10, 1), players=[("Fauz", {"g": 3})])
        await cog.after_matches(guild, [SimpleNamespace(ts=ts(2026, 10, 1), players=[SimpleNamespace(name="Fauz")])])
        member.edit.assert_not_awaited()
        self.assertEqual(member.nick, "Fauz 🔥")

    async def test_unlinking_removes_badges_and_the_emoji(self):
        member = FakeMember(42, None)
        guild = self.guild(member)
        set_link("10", "42", "Fauz", "self")
        cog = self.cog(SimpleNamespace(send=AsyncMock()))
        self.game(ts(2026, 10, 1), players=[("Fauz", {"g": 3})])
        await cog.refresh_member(guild, "42")
        self.assertEqual(member.nick, "User42 🎩")
        await cog.forget(guild, "42")
        self.assertIsNone(member.nick)
        self.assertEqual(B.stored("10", "42"), {})

    async def test_only_badges_from_the_new_games_are_announced(self):
        sent = []
        member = FakeMember(42, None)
        guild = self.guild(member)
        set_link("10", "42", "Fauz", "self")
        cog = self.cog(SimpleNamespace(send=AsyncMock(side_effect=lambda *a, **k: sent.append(a[0]))))
        self.game(ts(2026, 10, 1), players=[("Fauz", {"g": 1})])
        await cog.refresh_member(guild, "42")                          # seeded
        self.game(ts(2026, 9, 1), players=[("Fauz", {"g": 3})], club=754785)   # old history, saved quietly
        new_ts = ts(2026, 10, 5)
        self.game(new_ts, players=[("Fauz", {"r": 9.4})])
        await cog.after_matches(guild, [SimpleNamespace(ts=new_ts, players=[SimpleNamespace(name="Fauz")])])
        self.assertEqual(len(sent), 1)
        self.assertIn("Star Man", sent[0])
        self.assertNotIn("Hat-trick", sent[0])
        self.assertIn("hat_trick", B.stored("10", "42"))               # still earned, just not announced


class AwardsTests(Base):
    def october(self):
        # Ali: 4 games avg 8.0, Bob: 4 games avg 7.0 + lots of goals, Cat: 1 game 9.5 (not enough games)
        for i in range(4):
            self.game(ts(2026, 10, 3 + i), "W" if i < 3 else "L",
                      [("Ali", {"r": 8.0, "a": 1}), ("Bob", {"r": 7.0, "g": 2})] + ([("Cat", {"r": 9.5})] if i == 0 else []))
        for i in range(3):   # September: Ali averaged 6.5 -> most improved
            self.game(ts(2026, 9, 3 + i), "L", [("Ali", {"r": 6.5}), ("Bob", {"r": 7.2})])

    def test_compute(self):
        self.october()
        a = A.compute(CLUB_ID, 2026, 10, SYD)
        self.assertEqual(a["record"]["games"], 4)
        self.assertEqual(a["potm"]["name"], "Ali")                 # Cat rated higher but played 1 game
        self.assertEqual((a["scorer"]["name"], a["scorer"]["value"]), ("Bob", 8))
        self.assertEqual(a["assister"]["name"], "Ali")
        self.assertEqual(a["improved"]["name"], "Ali")
        self.assertAlmostEqual(a["improved"]["value"], 1.5)
        self.assertIsNone(A.compute(CLUB_ID, 2026, 8, SYD))      # no games
        png = A.render_card("MADBOYS FC", a)
        self.assertTrue(png.startswith(b"\x89PNG"))

    async def test_posted_once_with_role_and_badge(self):
        from cogs.awards import AwardsCog, ROLE_NAME
        self.october()
        set_link("10", "7", "Ali", "self")
        sent = []
        channel = SimpleNamespace(send=AsyncMock(side_effect=lambda *a, **k: sent.append(k) or SimpleNamespace(id=99)))
        winner = FakeMember(7, None)
        old = FakeMember(8, None)
        role = SimpleNamespace(name=ROLE_NAME, members=[old])
        role.__ge__ = lambda self, other: False
        winner.add_roles, old.remove_roles = AsyncMock(), AsyncMock()

        class Role(SimpleNamespace):
            def __ge__(self, other):
                return False
        role = Role(name=ROLE_NAME, members=[old])
        guild = SimpleNamespace(id=10, roles=[role], get_member=lambda uid: winner if uid == 7 else None,
                                me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True), top_role=5))
        tracker = SimpleNamespace(channel_id_for=lambda gid: 20)
        badges = SimpleNamespace(refresh_member=AsyncMock())
        cog = object.__new__(AwardsCog)
        cog.bot = SimpleNamespace(get_cog=lambda n: {"MatchdayCog": tracker, "BadgesCog": badges}.get(n),
                                  get_channel=lambda cid: channel)
        with patch("cogs.awards._tz", return_value=SYD), patch("cogs.awards.report_health", AsyncMock()):
            self.assertEqual(await cog.post_due(guild, datetime(2026, 11, 1, 9, tzinfo=SYD)), 0)   # before 10am
            self.assertGreaterEqual(await cog.post_due(guild, datetime(2026, 11, 1, 10, tzinfo=SYD)), 1)
            self.assertEqual(await cog.post_due(guild, datetime(2026, 11, 2, 10, tzinfo=SYD)), 0)  # only once
            self.assertEqual(await cog.post_due(guild, datetime(2026, 11, 20, 10, tzinfo=SYD)), 0)
        self.assertIn("Player of the Month", sent[0]["embed"].description)
        winner.add_roles.assert_awaited_once()
        old.remove_roles.assert_awaited_once()                    # last month's winner loses the role
        badges.refresh_member.assert_awaited_once_with(guild, "7")
        self.assertIn("potm", B.evaluate("10", "Ali"))            # 👑 badge from the saved award


class AwardsRoleTests(Base):
    async def test_one_club_failing_still_gives_the_other_club_its_role(self):
        from cogs.awards import AwardsCog, ROLE_NAME
        import discord
        for club in (CLUB_ID, 754785):
            for i in range(4):
                self.game(ts(2026, 10, 3 + i), players=[("Ali" if club == CLUB_ID else "Gra", {"r": 8.0})], club=club)
        with db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO monitored_clubs (club_id,name,enabled) VALUES (754785,'GRAYSBOYS',1)")
        set_link("10", "7", "Ali", "self")
        set_link("10", "9", "Gra", "self")
        calls = {"n": 0}

        async def send(**kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
            return SimpleNamespace(id=calls["n"])
        channel = SimpleNamespace(send=AsyncMock(side_effect=send))
        ali, gra = FakeMember(7, None), FakeMember(9, None)
        for m in (ali, gra):
            m.add_roles, m.remove_roles = AsyncMock(), AsyncMock()

        class Role(SimpleNamespace):
            def __ge__(self, other):
                return False
        role = Role(name=ROLE_NAME, members=[])
        guild = SimpleNamespace(id=10, roles=[role], get_member={7: ali, 9: gra}.get,
                                me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True), top_role=5))
        cog = object.__new__(AwardsCog)
        cog.bot = SimpleNamespace(get_cog=lambda n: SimpleNamespace(channel_id_for=lambda g: 20) if n == "MatchdayCog" else None,
                                  get_channel=lambda cid: channel)
        with patch("cogs.awards._tz", return_value=SYD), patch("cogs.awards.report_health", AsyncMock()):
            await cog.post_due(guild, datetime(2026, 11, 1, 10, tzinfo=SYD))    # 2nd club's post fails
            first = [m for m in (ali, gra) if m.add_roles.await_count]
            self.assertEqual(len(first), 1)                                     # the posted club's winner got it
            role.members = first
            first[0].roles = [role]                                             # Discord now shows the role
            await cog.post_due(guild, datetime(2026, 11, 1, 11, tzinfo=SYD))    # retry posts the other club
        ali.add_roles.assert_awaited_once()
        gra.add_roles.assert_awaited_once()
        ali.remove_roles.assert_not_awaited()                                   # both clubs' winners keep it
        gra.remove_roles.assert_not_awaited()

    async def test_role_removed_when_no_linked_winner(self):
        from cogs.awards import AwardsCog, ROLE_NAME
        old = FakeMember(8, None)
        old.remove_roles = AsyncMock()

        class Role(SimpleNamespace):
            def __ge__(self, other):
                return False
        guild = SimpleNamespace(id=10, roles=[Role(name=ROLE_NAME, members=[old])], get_member=lambda uid: None,
                                me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True), top_role=5))
        cog = object.__new__(AwardsCog)
        cog.bot = SimpleNamespace()
        with patch("cogs.awards.report_health", AsyncMock()):
            await cog.update_role(guild, [])
        old.remove_roles.assert_awaited_once()


class MenuTests(Base):
    async def test_new_menu_pages_render(self):
        from collections import Counter
        from cogs.hub import SQUAD_PAGES, StatsMenu
        self.game(ts(2026, 10, 3), players=[("Ali", {"r": 8.0})])
        guild = SimpleNamespace(id=10, get_member=lambda _: None, fetch_member=AsyncMock())
        bot = SimpleNamespace(ea=SimpleNamespace(), get_guild=lambda _: guild, get_cog=lambda n: None)
        menu = StatsMenu(bot, SimpleNamespace(id=42), "10", ["Ali"], [])
        for key in SQUAD_PAGES:
            menu.stat = key
            await menu.go("leaderboard")
            self.assertIsNotNone(menu.embed, key)
            ids = Counter(c.custom_id for c in menu.children)
            self.assertFalse([i for i, n in ids.items() if n > 1], key)
            lb = next(c for c in menu.children if getattr(c, "placeholder", "") == "🏆 Leaderboards…")
            self.assertLessEqual(len(lb.options), 25)


if __name__ == "__main__":
    unittest.main()
