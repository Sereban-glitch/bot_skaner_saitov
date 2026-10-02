import asyncio
import unittest

import bridge


class FakeSender:
    def __init__(self):
        self.calls = []

    async def __call__(self, chat, text, **kwargs):
        self.calls.append((chat, text, kwargs))
        return object()


class TelegramMessageLimitTests(unittest.TestCase):
    def test_split_preserves_text_and_utf16_limit(self):
        text = ("рядок з емодзі 🚜 і даними\n" * 500) + "кінець"
        chunks = bridge.split_telegram_text(text)

        self.assertGreater(len(chunks), 1)
        self.assertEqual(text, "".join(chunks))
        self.assertTrue(
            all(bridge.telegram_text_units(chunk) <= bridge.TELEGRAM_TEXT_LIMIT for chunk in chunks)
        )

    def test_short_message_preserves_entities(self):
        sender = FakeSender()
        entities = [object()]

        asyncio.run(
            bridge.send_telegram_text(sender, "@target", "коротко", formatting_entities=entities)
        )

        self.assertEqual(1, len(sender.calls))
        self.assertIs(entities, sender.calls[0][2]["formatting_entities"])

    def test_long_message_is_split_without_stale_entities(self):
        sender = FakeSender()
        text = "A" * 9000

        asyncio.run(
            bridge.send_telegram_text(sender, "@target", text, formatting_entities=[object()])
        )

        self.assertEqual(3, len(sender.calls))
        self.assertEqual(text, "".join(call[1] for call in sender.calls))
        self.assertTrue(all(not call[2] for call in sender.calls))

    def test_utf16_surrogate_pairs_are_counted(self):
        text = "🚜" * 3000
        chunks = bridge.split_telegram_text(text)

        self.assertEqual(text, "".join(chunks))
        self.assertTrue(all(bridge.telegram_text_units(chunk) <= 4096 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
