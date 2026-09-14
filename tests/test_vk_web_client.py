import unittest
from datetime import datetime
from unittest.mock import AsyncMock

from app.vk_web_client import VkWebClientManager


class VkWebDateParsingTests(unittest.TestCase):
    def setUp(self):
        self.client = VkWebClientManager()
        self.reference = datetime(2026, 9, 11, 12, 0)

    def test_parses_today(self):
        self.assertEqual(
            self.client._parse_message_date("Сегодня", "09:15", self.reference),
            datetime(2026, 9, 11, 9, 15),
        )

    def test_parses_yesterday(self):
        self.assertEqual(
            self.client._parse_message_date("Вчера", "23:59", self.reference),
            datetime(2026, 9, 10, 23, 59),
        )

    def test_parses_russian_month(self):
        self.assertEqual(
            self.client._parse_message_date("7 сентября", "14:32", self.reference),
            datetime(2026, 9, 7, 14, 32),
        )

    def test_infers_previous_year(self):
        self.assertEqual(
            self.client._parse_message_date("27 ноября", "10:00", datetime(2026, 1, 3)),
            datetime(2025, 11, 27, 10, 0),
        )

    def test_collects_dom_messages_in_requested_period(self):
        collected = {}
        parsed_dates = self.client._collect_dom_messages(
            stacks=[{
                "dateLabel": "11 сентября",
                "messages": [
                    {
                        "messageId": "message-41",
                        "senderName": "Иван Иванов",
                        "profilePath": "/id123",
                        "text": "До периода",
                        "time": "08:59",
                        "attachmentLabels": [],
                    },
                    {
                        "messageId": "message-42",
                        "senderName": "Иван Иванов",
                        "profilePath": "/id123",
                        "text": "В периоде",
                        "time": "09:15",
                        "attachmentLabels": [],
                    },
                ],
            }],
            start_date=datetime(2026, 9, 11, 9, 0),
            end_date=datetime(2026, 9, 12, 12, 0),
            collected=collected,
        )

        self.assertEqual(parsed_dates, [
            datetime(2026, 9, 11, 8, 59),
            datetime(2026, 9, 11, 9, 15),
        ])
        self.assertEqual(list(collected.values())[0]["message_id"], 42)
        self.assertEqual(list(collected.values())[0]["sender_id"], 123)
        self.assertEqual(list(collected.values())[0]["text"], "В периоде")

    def test_collects_attachment_without_text(self):
        collected = {}
        self.client._collect_dom_messages(
            stacks=[{
                "dateLabel": "Сегодня",
                "messages": [{
                    "messageId": "77",
                    "senderName": "Мария",
                    "profilePath": "",
                    "text": "",
                    "time": "10:30",
                    "attachmentLabels": ["Фотография", "Фотография"],
                }],
            }],
            start_date=datetime(2026, 9, 11, 9, 0),
            end_date=self.reference,
            collected=collected,
        )

        self.assertEqual(list(collected.values())[0]["text"], "[Фотография]")


class VkWebFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_reopens_page_in_existing_browser_context(self):
        class ClosedPage:
            def is_closed(self):
                return True

        class OpenPage:
            def is_closed(self):
                return False

        client = VkWebClientManager()
        replacement_page = OpenPage()
        context = type("FakeContext", (), {})()
        context.pages = []
        context.new_page = AsyncMock(return_value=replacement_page)
        client._page = ClosedPage()
        client._context = context

        page = await client._ensure_page()

        self.assertIs(page, replacement_page)
        context.new_page.assert_awaited_once()

    async def test_refreshes_browser_token_after_first_web_api_failure(self):
        client = VkWebClientManager()
        page = object()
        expected = [{"message_id": 42, "text": "Сообщение"}]
        client._web_api_auth_params = {"access_token": "stale"}
        client._web_api_headers = {"referer": "https://vk.ru/im"}
        client._ensure_page = AsyncMock(return_value=page)
        client.status = AsyncMock(return_value={"authorized": True})
        client._get_messages_via_web_api = AsyncMock(
            side_effect=[RuntimeError("expired token"), expected]
        )
        client._get_messages_from_page = AsyncMock()

        result = await client.get_messages(
            peer_id=2_000_000_006,
            start_date=datetime(2026, 9, 11, 9, 0),
            end_date=datetime(2026, 9, 12, 12, 4),
        )

        self.assertEqual(result, expected)
        self.assertEqual(client._get_messages_via_web_api.await_count, 2)
        self.assertEqual(client._web_api_auth_params, {})
        self.assertEqual(client._web_api_headers, {})
        client._get_messages_from_page.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
