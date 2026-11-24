"""Gemini fallback and function-calling behavior without network requests."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs import chat


def response(*parts):
    return {"candidates": [{"content": {"role": "model", "parts": list(parts)}}]}


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = chat.ChatCog(SimpleNamespace())
        self.message = SimpleNamespace(
            guild=None, author=SimpleNamespace(display_name="Fauz"), channel=SimpleNamespace(id=42)
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
        self.assertEqual(len(self.cog.history[42]), 2)

    async def test_permission_failure_does_not_try_another_model(self):
        self.cog._call_gemini = AsyncMock(return_value=(403, None))
        self.assertIn("Bot Status", await self.cog._answer(self.message, "Hi"))
        self.cog._call_gemini.assert_awaited_once()
        self.assertEqual(len(self.cog.history[42]), 0)
