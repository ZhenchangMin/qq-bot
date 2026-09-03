import json
import unittest
from unittest.mock import patch

import bot


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


class PromptTests(unittest.TestCase):
    def test_scene_prompts_are_distinct(self):
        private = bot.scene_system_prompt("private")
        group = bot.scene_system_prompt("group")
        self.assertIn(bot.BOT_NAME, private)
        self.assertIn("QQ 私聊", private)
        self.assertIn("QQ 群聊", group)
        self.assertIn("2 至 6 句", group)
        self.assertIn("/smile", group)
        self.assertNotEqual(private, group)

    def test_extract_qq_emoji_codes_excludes_registered_commands(self):
        codes = bot.extract_qq_emoji_codes("你好 /smile /doge /HELP /smile /abc123 /ok!")
        self.assertEqual(codes, ["/smile", "/doge", "/ok"])

    def test_payload_marks_detected_qq_emoji_codes(self):
        payload = bot.build_llm_payload([], "今天不错 /smile", scene="private")
        system = payload["messages"][0]["content"]
        self.assertIn("识别到这些 QQ 表情码", system)
        self.assertIn("/smile", system)

    def test_payload_uses_scene_specific_output_limit(self):
        private = bot.build_llm_payload([], "hello", scene="private")
        group = bot.build_llm_payload([], "hello", scene="group")
        self.assertEqual(private["max_tokens"], bot.LLM_PRIVATE_MAX_TOKENS)
        self.assertEqual(group["max_tokens"], bot.LLM_GROUP_MAX_TOKENS)
        self.assertGreater(private["max_tokens"], group["max_tokens"])

    def test_thinking_switch_is_optional_and_configurable(self):
        payload = bot.build_llm_payload([], "hello", scene="private")
        if bot.LLM_THINKING in {"enabled", "disabled"}:
            self.assertEqual(payload["thinking"], {"type": bot.LLM_THINKING})
        else:
            self.assertNotIn("thinking", payload)

    def test_history_is_trimmed_per_scene(self):
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": str(i)}
            for i in range(30)
        ]
        private = bot.trim_history(history, scene="private")
        group = bot.trim_history(history, scene="group")
        self.assertEqual(len(private), bot.LLM_PRIVATE_MAX_TURNS * 2)
        self.assertEqual(len(group), bot.LLM_GROUP_MAX_TURNS * 2)
        self.assertEqual(group, history[-bot.LLM_GROUP_MAX_TURNS * 2 :])


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.conversation_history.clear()
        bot.processed_message_ids.clear()
        self.ws = FakeWS()

    async def test_private_text_goes_to_private_scene(self):
        seen = {}

        async def fake_ask(key, text, *, scene):
            seen.update(key=key, text=text, scene=scene)
            return "private answer"

        event = {
            "post_type": "message",
            "message_type": "private",
            "self_id": 999,
            "user_id": 123,
            "message": [{"type": "text", "data": {"text": "你好"}}],
        }
        with patch.object(bot, "ask_llm", fake_ask):
            await bot.handle_event(self.ws, event)

        self.assertEqual(seen["scene"], "private")
        self.assertEqual(self.ws.sent[-1]["params"]["message"], "private answer")

    async def test_plain_group_message_is_ignored(self):
        event = {
            "post_type": "message",
            "message_type": "group",
            "self_id": 999,
            "user_id": 123,
            "group_id": 456,
            "message": [{"type": "text", "data": {"text": "你好"}}],
        }
        await bot.handle_event(self.ws, event)
        self.assertEqual(self.ws.sent, [])

    async def test_mentioned_group_text_goes_to_group_scene(self):
        seen = {}

        async def fake_ask(key, text, *, scene):
            seen.update(key=key, text=text, scene=scene)
            return "group answer"

        event = {
            "post_type": "message",
            "message_type": "group",
            "self_id": 999,
            "user_id": 123,
            "group_id": 456,
            "message": [
                {"type": "at", "data": {"qq": "999"}},
                {"type": "text", "data": {"text": " 你好"}},
            ],
        }
        with patch.object(bot, "ask_llm", fake_ask):
            await bot.handle_event(self.ws, event)

        self.assertEqual(seen["scene"], "group")
        self.assertEqual(seen["text"], "你好")
        self.assertEqual(self.ws.sent[-1]["params"]["message"], "group answer")

    async def test_group_ping_requires_mention(self):
        plain = {
            "post_type": "message",
            "message_type": "group",
            "self_id": 999,
            "user_id": 123,
            "group_id": 456,
            "message": [{"type": "text", "data": {"text": "/ping"}}],
        }
        await bot.handle_event(self.ws, plain)
        self.assertEqual(self.ws.sent, [])

        mentioned = dict(plain)
        mentioned["message"] = [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " /ping"}},
        ]
        await bot.handle_event(self.ws, mentioned)
        self.assertEqual(self.ws.sent[-1]["params"]["message"], "pong")

    async def test_unknown_slash_english_token_is_treated_as_emoji_not_command(self):
        seen = {}

        async def fake_ask(key, text, *, scene):
            seen.update(key=key, text=text, scene=scene)
            return "收到 /smile"

        event = {
            "post_type": "message",
            "message_type": "private",
            "self_id": 999,
            "user_id": 123,
            "message": [{"type": "text", "data": {"text": "/smile"}}],
        }
        with patch.object(bot, "ask_llm", fake_ask):
            await bot.handle_event(self.ws, event)

        self.assertEqual(seen["text"], "/smile")
        self.assertEqual(self.ws.sent[-1]["params"]["message"], "收到 /smile")

    async def test_same_message_id_is_processed_only_once(self):
        calls = 0

        async def fake_ask(key, text, *, scene):
            nonlocal calls
            calls += 1
            return f"answer-{calls}"

        event = {
            "post_type": "message",
            "message_type": "private",
            "self_id": 999,
            "user_id": 123,
            "message_id": 777,
            "message": [{"type": "text", "data": {"text": "你好"}}],
        }

        with patch.object(bot, "ask_llm", fake_ask):
            await bot.handle_event(self.ws, event)
            await bot.handle_event(self.ws, dict(event))

        self.assertEqual(calls, 1)
        self.assertEqual(len(self.ws.sent), 1)
        self.assertEqual(self.ws.sent[0]["params"]["message"], "answer-1")


if __name__ == "__main__":
    unittest.main()
