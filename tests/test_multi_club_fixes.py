"""Regression tests for multi-club bugs found in review (panel binding, quiet sync, /position, /motm close)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import clubs
import db
from cogs.clubs import toggle_club
from cogs.rotation import log_positions
import tests.test_multi_club as TMC


class MultiClubFixTests(TMC.MultiClubTests):
    async def test_pinned_panel_follows_the_clicker_not_the_poster(self):
        from cogs.hub import PanelView
        with clubs.club_scope(self.secondary):        # manager posting /panel follows GRAYSBOYS
            panel = PanelView(self.bot)
        self.assertIsNone(panel.club_id)
        db.set_setting("10", "club:42", str(self.primary["club_id"]))   # clicker picked MADBOYS
        seen = []

        async def stats(interaction, _button=None):
            seen.append(clubs.club_id())
        button = next(c for c in panel.children if getattr(c, "custom_id", "") == "madboys:panel:stats")
        original = button.callback
        panel.stats = stats
        interaction = SimpleNamespace(guild_id=10, user=SimpleNamespace(id=42), extras={})
        with patch("interaction_tracking.record"), patch("cogs.hub.StatsMenu.open", AsyncMock(
                side_effect=lambda bot, inter, page="home": seen.append(clubs.club_id()))):
            await original(interaction)
        self.assertEqual(seen, [self.primary["club_id"]])

    async def test_partial_first_check_keeps_older_games_quiet(self):
        self.store(self.primary, "baseline", 100)
        third = self.secondary   # GRAYSBOYS has no history yet: its first check is a quiet sync
        league = [self.raw(third, "l1", 150)]

        class Partial(list):
            complete = False
        calls = {"n": 0}

        def fetch(cid, **kw):
            if cid != third["club_id"]:
                return []
            calls["n"] += 1
            if calls["n"] == 1:
                return Partial(league)                      # playoff request failed
            return league + [self.raw(third, "p1", 120), self.raw(third, "p2", 130)]
        self.tracker.ea.get_recent_matches_multi.side_effect = fetch
        await self.tracker.poll_once()
        await self.tracker.poll_once()                      # older playoff games arrive now
        self.channel.send.assert_not_awaited()             # ...and are saved without announcements
        self.assertIsNone(db.get_setting("global", f"club:quiet:{third['club_id']}"))
        self.tracker.ea.get_recent_matches_multi.side_effect = \
            lambda cid, **kw: [self.raw(third, "new", 400)] if cid == third["club_id"] else []
        await self.tracker.poll_once()
        self.channel.send.assert_awaited()                  # a genuinely new game is announced again

    async def test_reenabled_club_does_not_announce_games_missed_while_off(self):
        self.seed()
        toggle_club(self.secondary["club_id"])              # off
        toggle_club(self.secondary["club_id"])              # back on
        self.tracker.ea.get_recent_matches_multi.side_effect = \
            lambda cid, **kw: [self.raw(clubs.club_for(cid), f"missed{i}", 200 + i) for i in range(5)] \
            if cid == self.secondary["club_id"] else []
        await self.tracker.poll_once()
        self.channel.send.assert_not_awaited()

    async def test_position_fixes_your_last_game_in_whichever_club(self):
        from cogs.positions import PositionsCog
        old = self.store(self.primary, "m_old", 100)
        new = self.store(self.secondary, "g_new", 500)
        self.store(self.primary, "m_other", 600)            # other squad played MADBOYS later
        log_positions("10", self.primary["name"], [("42", "DEF")], match_id=old.match_id)
        log_positions("10", self.secondary["name"], [("42", "DEF")], match_id=new.match_id)
        db.set_setting("10", "club:42", str(self.primary["club_id"]))
        cog = object.__new__(PositionsCog)
        cog.check_notes = AsyncMock()
        interaction = SimpleNamespace(guild_id=10, user=SimpleNamespace(id=42), guild=self.guild,
                                      response=SimpleNamespace(send_message=AsyncMock()))
        await PositionsCog.position.callback(cog, interaction, SimpleNamespace(value="CB"))
        with db.connect() as conn:
            rows = dict(conn.execute("SELECT club, position FROM rotation_log WHERE discord_id='42'").fetchall())
        self.assertEqual(rows, {self.primary["name"]: "DEF", self.secondary["name"]: "CB"})
        self.assertIn(self.secondary["name"], interaction.response.send_message.await_args.args[0])

    async def test_motm_close_finds_the_other_clubs_vote(self):
        from cogs.motm import MotmCog
        with db.connect() as conn:
            conn.execute("INSERT INTO motm_polls (match_id,guild_id,channel_id,title,candidates,closes_at,club_id) "
                         "VALUES ('754785:g','10','20','GRAYSBOYS','[]',9999999999,?)", (self.secondary["club_id"],))
        db.set_setting("10", "club:42", str(self.primary["club_id"]))
        cog = object.__new__(MotmCog)
        cog.close_poll = AsyncMock()
        interaction = SimpleNamespace(guild_id=10, user=SimpleNamespace(id=42, roles=[SimpleNamespace(name="Manager")]),
                                      extras={}, response=SimpleNamespace(send_message=AsyncMock()))
        await MotmCog.motm_close.callback(cog, interaction)
        cog.close_poll.assert_awaited_once()


for _name in dir(TMC.MultiClubTests):   # inherited tests already run in test_multi_club
    if _name.startswith("test_") and _name not in MultiClubFixTests.__dict__:
        setattr(MultiClubFixTests, _name, None)
