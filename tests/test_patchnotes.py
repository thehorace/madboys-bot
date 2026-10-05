import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import db
from cogs import patchnotes as P, usage
from cogs.hub import StatsMenu


def html(props):
    return '<html><script type="application/json" id="__NEXT_DATA__">' + json.dumps({"props": {"pageProps": props}}) + '</script></html>'


def article(slug="title-update-v1", year=2020, title="EA SPORTS FC 27 | Title Update v1"):
    return {"slug": slug, "title": title, "publishingDate": f"{year}-01-01T12:00:00Z",
            "linkedTo": [{"type": "Game", "slug": "fc-27"}],
            "body": "**Gameplay**\n\n- Test gameplay improvement.\n\n**Clubs**\n\n- Test Clubs fix. @everyone\n\nWant to provide us feedback?"}


class ParserTests(unittest.TestCase):
    def test_filters_news_old_games_bad_slugs_and_future_notes(self):
        items = [article(), article("promo", title="New kits available"),
                 article("future", 2099), article("../../bad"), article("old", title="EA SPORTS FC 26 | Title Update")]
        result = P.update_articles(html({"initialNewsData": {"items": items}}))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], P.SOURCE + "/title-update-v1")

    def test_blocked_page_and_wrong_article_fail_safely(self):
        with self.assertRaises(ValueError):
            P.update_articles("<html>Access denied</html>")
        with self.assertRaises(ValueError):
            P.article_details(html({"articleDetailsFallback": article()}), "different")

    def test_embed_keeps_details_link_and_escapes_mentions(self):
        data = {**article(), "url": P.SOURCE + "/title-update-v1", "published_ts": 1}
        embed = P.patch_embed(data)
        self.assertIn("Test Clubs fix", embed.description)
        self.assertIn(data["url"], embed.description)
        self.assertNotIn("@everyone", embed.description)
        self.assertNotIn("Want to provide", embed.description)

    def test_blank_and_carried_over_player_targets_are_labeled(self):
        self.assertEqual(usage.lookup_detail({"action": "Stats: club", "detail": "killashogun"}), "Club overview")
        self.assertEqual(usage.lookup_detail({"action": "Stats: lastgame", "detail": ""}), "Latest match")
        rows = [{"action": "Stats: club", "detail": "", "category": "lookup"}]
        self.assertIn("Club overview", usage.build_lookups(rows, "7d").fields[0].value)

    def test_menu_whole_screens_do_not_inherit_player_target(self):
        menu = object.__new__(StatsMenu)
        menu.player, menu.screen_failed = "killashogun", False
        for page, label in (("club", "Club overview"), ("lastgame", "Latest match"), ("form", "Last 10 matches")):
            menu.page = page
            interaction = SimpleNamespace(extras={})
            menu.record_result(interaction)
            self.assertEqual(interaction.extras["usage_lookup"][1], label)


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "test.db"))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init_all()
        P.init_patchnotes()
        self.cog = object.__new__(P.PatchNotesCog)
        self.cog.bot = SimpleNamespace(get_cog=lambda name: None)
        self.cog._lock = asyncio.Lock()
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.id, self.channel.name = 20, "general"
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=100))
        self.guild = SimpleNamespace(id=10, text_channels=[self.channel], get_channel=lambda id: self.channel)
        self.items = [article("title-update-old", 2019), article()]
        self.cog.fetch = AsyncMock(side_effect=self.fetch)

    async def fetch(self, url):
        if url == P.SOURCE:
            return html({"initialNewsData": {"items": self.items}})
        return html({"articleDetailsFallback": next(a for a in self.items if url.endswith(a["slug"]))})

    async def test_first_check_posts_latest_only_then_deduplicates_across_restart(self):
        await self.cog.check(self.guild)
        self.channel.send.assert_awaited_once()
        self.assertFalse(self.channel.send.call_args.kwargs["allowed_mentions"].everyone)
        self.assertIn("title-update-v1", self.channel.send.call_args.kwargs["embed"].url)
        await self.cog.check(self.guild)
        self.channel.send.assert_awaited_once()
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM patchnotes_seen").fetchone()[0], 2)
        self.items.append(article("title-update-v2", 2021, "EA SPORTS FC 27 | Title Update v2"))
        await self.cog.check(self.guild)
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_failed_send_is_not_marked_seen_and_can_retry(self):
        self.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Denied"), "denied")
        with self.assertRaises(discord.Forbidden):
            await self.cog.check(self.guild)
        self.assertIsNone(db.get_setting("10", "patchnotes:initialized"))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM patchnotes_seen").fetchone()[0], 0)
        self.channel.send.side_effect = None
        await self.cog.check(self.guild)
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_disabled_monitor_does_not_fetch_or_send(self):
        db.set_setting("10", "patchnotes:enabled", "0")
        await self.cog.check(self.guild)
        self.cog.fetch.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_preview_does_not_mark_seen_or_post_to_channel(self):
        latest = await self.cog.latest()
        self.assertTrue(latest["body"])
        self.channel.send.assert_not_awaited()
        self.assertIsNone(db.get_setting("10", "patchnotes:initialized"))

    async def test_private_controls_reject_other_admins(self):
        for command in (P.PatchNotesCog.settings, P.PatchNotesCog.check_command, P.PatchNotesCog.preview):
            interaction = SimpleNamespace(user=SimpleNamespace(id=2, name="owner"), guild=self.guild,
                                          response=SimpleNamespace(send_message=AsyncMock()))
            await command.callback(self.cog, interaction)
            interaction.response.send_message.assert_awaited_once()
        self.cog.fetch.assert_not_awaited()

    async def test_admin_patch_controls_render_and_toggle_privately(self):
        from cogs.admin import AdminView
        user = SimpleNamespace(id=42, name="fauz", bot=False)
        anchor = SimpleNamespace(edit_original_response=AsyncMock())
        bot = SimpleNamespace(get_cog=lambda name: self.cog if name == "PatchNotesCog" else None)
        panel = AdminView(bot, user, self.guild, anchor)
        interaction = SimpleNamespace(user=user, guild=self.guild, guild_id=10, extras={},
                                      response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))
        await panel.act(interaction, "patchnotes")
        self.assertEqual(panel.page, "patchnotes")
        self.assertTrue(any(getattr(c, "custom_id", "") == "admin:patchchannel" for c in panel.children))
        await panel.act(interaction, "toggle_patchnotes")
        self.assertFalse(P.patch_settings("10")["enabled"])
        anchor.edit_original_response.assert_awaited_once()
        other = SimpleNamespace(user=SimpleNamespace(id=43, name="fauz"), guild=self.guild, guild_id=10, extras={},
                                response=SimpleNamespace(send_message=AsyncMock()))
        await panel.act(other, "toggle_patchnotes")
        self.assertFalse(P.patch_settings("10")["enabled"])


if __name__ == "__main__":
    unittest.main()
