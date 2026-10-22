"""Speed-ups and quality-of-life fixes from the optimisation review."""
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

import badges as B
import insights as I
from config import CLUB_ID
from cogs.link import set_link
from db import connect, get_setting, set_setting
from tests.test_squad_features import Base, ts


class FastQueryTests(Base):
    async def test_one_players_partners_match_the_full_pair_list(self):
        names = ["Ali", "Bob", "Cat", "Dan"]
        for i in range(12):
            squad = [names[j] for j in range(4) if (i + j) % 4 != 0]
            self.game(ts(2026, 9, 1 + i), "WDL"[i % 3], [(n, {"pos": "defender"}) for n in squad])
        full = {}
        for p in I._pair_rows(CLUB_ID):
            if "ali" in (p["a"].lower(), p["b"].lower()):
                other = p["b"] if p["a"].lower() == "ali" else p["a"]
                full[other] = (p["games"], p["w"], p["d"], p["l"], p["gf"], p["ga"])
        mine = {p["b"]: (p["games"], p["w"], p["d"], p["l"], p["gf"], p["ga"]) for p in I._pair_rows(CLUB_ID, "ALI")}
        self.assertEqual(mine, full)
        self.assertTrue(all(p["a"] == "Ali" for p in I._pair_rows(CLUB_ID, "ali")))

    async def test_motm_table_counts_votes_from_every_poll(self):
        from clubs import club_scope
        from cogs.motm import season_table
        m1 = self.game(ts(2026, 9, 1), players=[("Ali", {})])
        m2 = self.game(ts(2026, 9, 2), players=[("Bob", {})])
        with connect() as conn:
            for mid, win in ((m1, "Ali"), (m2, "Bob")):
                conn.execute("INSERT INTO motm_polls (match_id,guild_id,channel_id,title,candidates,closes_at,closed,winners,club_id)"
                             " VALUES (?,?,?,?,?,?,1,?,?)", (mid, "10", "20", "t", "[]", 0, json.dumps([win]), CLUB_ID))
            for mid, voter, cand in ((m1, "1", "Ali"), (m1, "2", "Ali"), (m1, "3", "Bob"), (m2, "1", "Bob")):
                conn.execute("INSERT INTO motm_votes VALUES (?,?,?,'x')", (mid, voter, cand))
        with club_scope({"club_id": CLUB_ID, "name": "MADBOYS FC"}):
            self.assertEqual(season_table(), [("Ali", 1, 2), ("Bob", 1, 2)])


class SetupNameTests(Base):
    async def test_typo_warning_only_when_there_is_something_to_compare(self):
        from cogs.onboarding import unknown_ea_name
        self.assertFalse(unknown_ea_name("Whoever", []))          # brand-new bot: no data, no nagging
        self.game(ts(2026, 9, 1), players=[("Ali", {})])
        self.assertFalse(unknown_ea_name("ali", []))              # played a tracked game
        self.assertFalse(unknown_ea_name("bob", ["Bob"]))         # on the EA squad list
        self.assertTrue(unknown_ea_name("Alii", ["Bob"]))         # neither: probably a typo


class RotationChannelTests(Base):
    async def test_turning_channel_notes_off_beats_the_env_default(self):
        from cogs.positions import K_MANAGER_CHANNEL, PositionsCog
        cog = PositionsCog.__new__(PositionsCog)
        with patch.dict(os.environ, {"MANAGER_CHANNEL_ID": "555"}):
            self.assertEqual(cog.manager_channel_id("10"), 555)
            set_setting("10", K_MANAGER_CHANNEL, "777")
            self.assertEqual(cog.manager_channel_id("10"), 777)
            set_setting("10", K_MANAGER_CHANNEL, "off")
            self.assertIsNone(cog.manager_channel_id("10"))


class TimeoutTests(Base):
    async def test_expired_menu_greys_out_its_buttons(self):
        from interaction_tracking import TrackedView
        view = TrackedView(timeout=5)
        button = discord.ui.Button(label="x")
        button.callback = AsyncMock()
        view.add_item(button)
        inter = SimpleNamespace(message=SimpleNamespace(id=9), extras={}, guild_id=10, user=SimpleNamespace(id=1),
                                response=SimpleNamespace(type=discord.InteractionResponseType.deferred_message_update),
                                edit_original_response=AsyncMock())
        with patch("interaction_tracking.record"), patch("clubs.selected_club", return_value={"club_id": CLUB_ID, "name": "MADBOYS FC"}):
            await button.callback(inter)
        await view.on_timeout()
        self.assertTrue(button.disabled)
        inter.edit_original_response.assert_awaited_once_with(view=view)

    async def test_untouched_menu_timeout_does_nothing(self):
        from interaction_tracking import TrackedView
        await TrackedView(timeout=5).on_timeout()   # no click yet: nothing to edit, no error


class MenuGateTests(Base):
    async def test_unlinked_player_can_open_last_game_without_setup(self):
        import cogs.hub as H
        from cogs.hub import StatsMenu
        inter = SimpleNamespace(guild_id=10, user=SimpleNamespace(id=42), extras={},
                                response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        with patch("cogs.onboarding.open_setup", AsyncMock()) as setup, \
                patch.object(H.S, "roster_names", AsyncMock(return_value=[])), \
                patch.object(StatsMenu, "go", AsyncMock()), patch.object(StatsMenu, "record_result", Mock()):
            await StatsMenu.open(SimpleNamespace(ea=None), inter, "lastgame")
            setup.assert_not_awaited()
            await StatsMenu.open(SimpleNamespace(ea=None), inter, "me")
            setup.assert_awaited_once()

    async def test_switch_club_only_shown_with_two_clubs(self):
        from cogs.hub import StatsMenu
        guild = SimpleNamespace(id=10, get_member=lambda _: None, fetch_member=AsyncMock())
        bot = SimpleNamespace(ea=SimpleNamespace(), get_guild=lambda _: guild, get_cog=lambda n: None)
        menu = StatsMenu(bot, SimpleNamespace(id=42), "10", [], [])
        labels = lambda: [getattr(c, "label", "") or "" for c in menu.children]
        with patch("cogs.hub.monitored_clubs", return_value=[{"club_id": CLUB_ID, "name": "MADBOYS FC"}]):
            await menu.go("home")
            self.assertFalse([l for l in labels() if l.startswith("Switch")])
        with patch("cogs.hub.monitored_clubs", return_value=[{"club_id": CLUB_ID, "name": "MADBOYS FC"},
                                                             {"club_id": 2, "name": "MADBOYS B"}]):
            await menu.go("home")
            self.assertIn("Switch to MADBOYS B", labels())


class RelinkBadgeTests(Base):
    async def test_badge_page_rechecks_after_relinking_to_another_name(self):
        from cogs.badges import BadgesCog
        cog = BadgesCog.__new__(BadgesCog)
        cog.refresh_member = AsyncMock()
        set_link("10", "5", "NewName", "self")
        B.set_user("10", "5", seeded=1, ea_name="OldName")
        user = SimpleNamespace(id=5, bot=False, display_name="x")
        inter = SimpleNamespace(guild_id=10, user=user, guild=SimpleNamespace(id=10), extras={},
                                response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        with patch("cogs.badges.badges_embed", return_value=discord.Embed()):
            await cog.open_badges(inter)
        cog.refresh_member.assert_awaited_once()
        cog.refresh_member.reset_mock()
        B.set_user("10", "5", ea_name="NewName")
        with patch("cogs.badges.badges_embed", return_value=discord.Embed()):
            await cog.open_badges(inter)
        cog.refresh_member.assert_not_awaited()

    async def test_linking_schedules_a_background_refresh(self):
        from cogs.badges import BadgesCog, badges_relinked
        cog = BadgesCog.__new__(BadgesCog)
        cog._background = set()
        cog.refresh_member = AsyncMock()
        inter = SimpleNamespace(client=SimpleNamespace(get_cog=lambda n: cog), guild=SimpleNamespace(id=10))
        badges_relinked(inter, "5")
        for t in list(cog._background):
            await t
        cog.refresh_member.assert_awaited_once_with(inter.guild, "5")
        badges_relinked(SimpleNamespace(), "5")   # odd interaction objects never break linking
