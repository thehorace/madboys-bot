"""Gemini fallback and function-calling behavior without network requests."""
import unittest
from types import SimpleNamespace

from PIL import Image
from unittest.mock import AsyncMock, patch

from cogs import chat


def response(*parts):
    return {"candidates": [{"content": {"role": "model", "parts": list(parts)}}]}


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = chat.ChatCog(SimpleNamespace())
        self.message = SimpleNamespace(
            guild=None, author=SimpleNamespace(id=50, display_name="Fauz"), channel=SimpleNamespace(id=42)
        )
        models = patch.multiple(chat, GEMINI_MODEL="primary", GEMINI_FALLBACK_MODEL="fallback")
        models.start()
        self.addCleanup(models.stop)

    async def test_fallback_restarts_without_primary_signature(self):
        tool_call = {"functionCall": {"name": "get_recent_results", "id": "call_1"}, "thoughtSignature": "signed"}
        self.cog._call_gemini = AsyncMock(side_effect=[
            (200, response(tool_call)),
            (503, None),
            (200, response({"text": "Hi!"})),
        ])

        self.assertEqual(await self.cog._answer(self.message, "Hi"), "Hi!")
        first, followup, fallback = self.cog._call_gemini.await_args_list
        self.assertEqual([call.args[1] for call in (first, followup, fallback)],
                         ["primary", "primary", "fallback"])
        self.assertEqual(followup.args[0]["contents"][1]["parts"], [tool_call])
        result = followup.args[0]["contents"][2]["parts"][0]["functionResponse"]
        self.assertEqual(result["id"], "call_1")
        self.assertEqual(fallback.args[0]["contents"], first.args[0]["contents"])
        self.assertEqual(len(self.cog.history[(42, 50, None)]), 2)

    async def test_permission_failure_does_not_try_another_model(self):
        self.cog._call_gemini = AsyncMock(return_value=(403, None))
        self.assertIn("Bot Status", await self.cog._answer(self.message, "Hi"))
        self.cog._call_gemini.assert_awaited_once()
        self.assertFalse(self.cog.history)


    async def test_comparison_uses_saved_stats_and_replies_with_one_attachment(self):
        shared = dict(games=50, wins=27, draws=8, losses=15, avg_rating=6.51, goals=2, assists=4,
                      pass_accuracy_pct=None, motm=1, positions_played={"midfielder": 16})
        left = dict(shared, player="thehorace")
        right = dict(shared, player="PlayForKeeps", avg_rating=7.29)
        result = {"left": left, "right": right, "days": 7.0}
        self.message.guild = SimpleNamespace(id=10, me=SimpleNamespace())
        self.message.author.id = 50
        self.message.channel.permissions_for = lambda _: SimpleNamespace(attach_files=True, embed_links=True)
        self.message.reply = AsyncMock()
        self.cog._call_gemini = AsyncMock(return_value=(200, response({"functionCall": {
            "name": "compare_players", "args": {"left": "thehorace", "right": "keeps", "days": 7}
        }})))
        with patch.object(chat, "selected_club", return_value={"club_id": 1, "name": "MADBOYS FC"}), \
             patch.object(chat, "get_link", return_value=None), \
             patch.dict(chat.TOOLS, {"compare_players": lambda *args, **kwargs: result}):
            card = await self.cog._answer(self.message, "Compare thehorace and keeps")
        self.assertEqual(card.title, "thehorace vs PlayForKeeps")
        self.assertEqual(card.period, "Past 7 days")
        self.assertIn(("Pass accuracy", "—", "—"), card.rows())
        self.assertIn("6.51", self.cog.history[(42, 50, 1)][-1]["parts"][0]["text"])
        self.cog._call_gemini.assert_awaited_once()

        async def inspect_attachment(*args, file, **kwargs):
            self.assertEqual(file.filename, "player-comparison.png")
            with Image.open(file.fp) as png:
                self.assertEqual(png.format, "PNG")
                self.assertEqual(png.size, (900, 690))
                png.verify()

        self.message.reply.side_effect = inspect_attachment
        await self.cog._send_comparison(self.message, card)
        self.message.reply.assert_awaited_once()

        self.message.reply.reset_mock(side_effect=True)
        self.message.channel.permissions_for = lambda _: SimpleNamespace(attach_files=False, embed_links=True)
        await self.cog._send_comparison(self.message, card)
        self.assertEqual(self.message.reply.await_args.kwargs["embed"].title, card.title)

    def test_compare_players_uses_a_shared_cutoff_and_rejects_missing_data(self):
        with patch.object(chat, "tool_player_stats", side_effect=[
            {"player": "thehorace", "games": 5}, {"player": "PlayForKeeps", "games": 0}
        ]) as lookup:
            result = chat.tool_compare_players(1, "thehorace", "keeps", 7)
        self.assertIn("error", result)
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(lookup.call_args_list[0].kwargs["since"], lookup.call_args_list[1].kwargs["since"])


    async def test_player_twin_question_uses_existing_feature_without_gemini(self):
        self.message.guild = SimpleNamespace(id=10)
        self.message.author.id = 50
        club = {"club_id": 2, "name": "GRAYSBOYS"}
        self.cog._call_gemini = AsyncMock()
        with patch.object(chat, "selected_club", return_value=club), \
             patch.object(chat, "get_link", return_value=None), \
             patch.object(chat, "command_catalog", return_value={"/playertwin": "Real football style twin"}), \
             patch.object(chat, "show_feature", AsyncMock(return_value="Player Twin result")) as view:
            reply = await self.cog._answer(self.message, "okay not bad, what about who graman plays the most like?")
            self.assertEqual(reply.command, "/playertwin")
            self.assertEqual(reply.screen, "Player Twin result")
            view.assert_awaited_once_with(self.cog.bot, "10", club, "player_twin", player="graman", asker_ea=None)
            installed = await self.cog._answer(self.message, "i would like to know if you have the playertwin installed?")
            self.assertIn("Yes.", installed)
        self.cog._call_gemini.assert_not_awaited()

    async def test_gemini_gets_registered_commands_but_cannot_invoke_admin_views(self):
        self.message.guild = SimpleNamespace(id=10)
        self.message.author.id = 50
        self.cog._call_gemini = AsyncMock(side_effect=[
            (200, response({"functionCall": {"name": "show_bot_feature", "args": {"feature": "admin"}}})),
            (200, response({"text": "Open /admin for the private panel."})),
        ])
        with patch.object(chat, "selected_club", return_value={"club_id": 2, "name": "GRAYSBOYS"}), \
             patch.object(chat, "get_link", return_value=None), \
             patch.object(chat, "command_catalog", return_value={"/admin": "Private admin panel"}), \
             patch.object(chat, "show_feature", AsyncMock()) as view:
            reply = await self.cog._answer(self.message, "Open admin")
        self.assertIn("/admin", reply)
        view.assert_not_awaited()
        first, second = self.cog._call_gemini.await_args_list
        self.assertIn("/admin", first.args[0]["systemInstruction"]["parts"][0]["text"])
        self.assertIn("error", second.args[0]["contents"][-1]["parts"][0]["functionResponse"]["response"])

    async def test_twin_uses_cross_club_name_resolution_and_existing_card(self):
        from cogs import chat_features, insights
        import player_twin

        club = {"club_id": 2, "name": "GRAYSBOYS"}
        card = ("twin embed", b"existing card")
        with patch.object(player_twin, "resolve_name", return_value=("Graman1302", [])) as resolve, \
             patch.object(insights, "build_player_twin", AsyncMock(return_value=card)) as build:
            reply = await chat_features.show_feature(self.cog.bot, "10", club, "player_twin", player="graman")
        self.assertEqual(reply, card)
        resolve.assert_called_once_with("graman")
        build.assert_awaited_once_with("Graman1302")


    async def test_history_stays_with_the_asker_and_club(self):
        self.message.guild = SimpleNamespace(id=10)
        self.cog._call_gemini = AsyncMock(return_value=(200, response({"text": "Answer"})))
        madboys = {"club_id": 1, "name": "MADBOYS"}
        graysboys = {"club_id": 2, "name": "GRAYSBOYS"}
        with patch.object(chat, "selected_club", side_effect=[madboys, madboys, graysboys, madboys]), \
             patch.object(chat, "get_link", return_value=None):
            await self.cog._answer(self.message, "My stats")
            self.message.author.id = 51
            await self.cog._answer(self.message, "My goals")
            self.message.author.id = 50
            await self.cog._answer(self.message, "My goals in GRAYSBOYS")
            await self.cog._answer(self.message, "And my goals?")
        requests = [call.args[0]["contents"] for call in self.cog._call_gemini.await_args_list]
        self.assertEqual([len(history) for history in requests], [1, 1, 1, 3])
        self.assertEqual(requests[-1][0]["parts"][0]["text"], "My stats")

    async def test_repeated_lookup_is_reused_on_fallback(self):
        self.message.guild = SimpleNamespace(id=10)
        club = {"club_id": 1, "name": "MADBOYS"}
        call = response({"functionCall": {"name": "get_recent_results", "args": {"limit": 3}}})
        self.cog._call_gemini = AsyncMock(side_effect=[
            (200, call), (503, None), (200, call), (200, response({"text": "Results"})),
        ])
        from unittest.mock import Mock
        lookup = Mock(return_value={"matches": []})
        with patch.object(chat, "selected_club", return_value=club), \
             patch.object(chat, "get_link", return_value=None), \
             patch.dict(chat.TOOLS, {"get_recent_results": lookup}):
            self.assertEqual(await self.cog._answer(self.message, "Last three results"), "Results")
        lookup.assert_called_once_with(1, limit=3)
