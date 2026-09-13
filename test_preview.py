import asyncio
import unittest
from dataclasses import replace

import bridge
import preview


class FakeMessage:
    def __init__(self, message_id, sender_id, text):
        self.id = message_id
        self.sender_id = sender_id
        self.raw_text = text
        self.message = text
        self.action = None


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []
        self.started = False
        self.disconnected = False

    async def start(self):
        self.started = True

    async def disconnect(self):
        self.disconnected = True

    async def get_entity(self, entity):
        return entity

    async def iter_messages(self, _entity, limit):
        for message in self.messages[:limit]:
            yield message

    async def send_message(self, chat, text):
        self.sent.append((chat, text))


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.settings = bridge.Settings(
            api_id=123,
            api_hash="hash",
            session_name="session",
            source_chat="-1001309601189",
            target_channel="@production_group",
            source_author_id="201821011",
            append_source_link=True,
            source_link_label="@radiokowtun",
            notify_chat="",
            notify_group_chats=(),
            notify_rss_chats=(),
            album_wait_seconds=1.5,
            rss_feed_url="http://example.test/feed/",
            rss_target_channel="@production_rss",
            rss_poll_seconds=300,
            rss_state_path=".rss-state.json",
            rss_recent_limit=30,
        )

    def test_preview_sends_at_most_one_source_and_one_rss_item_to_test_chat(self):
        client = FakeClient(
            [
                FakeMessage(10, 999, "Message from the wrong author"),
                FakeMessage(11, 201821011, "First matching source message"),
                FakeMessage(12, 201821011, "Second matching source message"),
            ]
        )
        rss_items = [
            {
                "title": "First RSS item",
                "excerpt": "First RSS description",
                "link": "http://domdara.org/first",
            },
            {
                "title": "Second RSS item",
                "excerpt": "Second RSS description",
                "link": "http://domdara.org/second",
            },
        ]

        asyncio.run(
            preview.run_preview(
                self.settings,
                "@sereban_tech",
                client=client,
                rss_items=rss_items,
            )
        )

        self.assertTrue(client.started)
        self.assertTrue(client.disconnected)
        self.assertEqual(2, len(client.sent))
        self.assertEqual(["@sereban_tech", "@sereban_tech"], [chat for chat, _ in client.sent])
        self.assertIn("First matching source message", client.sent[0][1])
        self.assertNotIn("Second matching source message", client.sent[0][1])
        self.assertIn("First RSS item", client.sent[1][1])
        self.assertNotIn("Second RSS item", client.sent[1][1])
        self.assertNotIn("@production_group", "".join(text for _, text in client.sent))
        self.assertNotIn("@production_rss", "".join(text for _, text in client.sent))

    def test_preview_disconnects_when_source_lookup_fails(self):
        class FailingClient(FakeClient):
            async def get_entity(self, entity):
                raise RuntimeError("source unavailable")

        client = FailingClient([])

        with self.assertRaisesRegex(RuntimeError, "source unavailable"):
            asyncio.run(
                preview.run_preview(
                    replace(self.settings, rss_feed_url=""),
                    "@sereban_tech",
                    client=client,
                    rss_items=[],
                )
            )

        self.assertTrue(client.disconnected)


if __name__ == "__main__":
    unittest.main()
