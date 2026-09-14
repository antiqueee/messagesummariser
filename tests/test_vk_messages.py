import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from app.vk_client import VkClientManager, VkFloodControlError
from app.source_router import exclude_chat_sources, fetch_chat_messages


class FakeVkClient(VkClientManager):
    def __init__(self, pages):
        super().__init__("", "")
        self.pages = list(pages)
        self.calls = []

    async def api_call(self, method, access_token, **params):
        self.calls.append((method, params))
        return self.pages.pop(0) if self.pages else {"items": []}

    async def _prime_sender_cache(self, access_token, items, sender_cache):
        for item in items:
            sender_cache[int(item.get("from_id") or 0)] = "Автор"


class VkMessageHistoryTests(unittest.IsolatedAsyncioTestCase):
    def test_excluding_vk_keeps_other_chats_and_does_not_mutate_source_data(self):
        chats = {
            1: [
                {"id": 10, "source": "telegram"},
                {"id": 11, "source": "vk"},
                {"id": 12, "source": "max"},
            ],
            2: [{"id": 20, "source": "vk"}],
        }

        filtered = exclude_chat_sources(chats, {"vk"})

        self.assertEqual([chat["id"] for chat in filtered[1]], [10, 12])
        self.assertNotIn(2, filtered)
        self.assertEqual([chat["id"] for chat in chats[1]], [10, 11, 12])
        self.assertIn(2, chats)

    async def test_flood_control_waits_and_retries_without_losing_request(self):
        class FakeResponse:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class FakeHttpClient:
            def __init__(self):
                self.responses = [
                    FakeResponse({"error": {"code": "6", "error_msg": "Flood control"}}),
                    FakeResponse({"error": {"error_code": "6", "error_msg": "Flood control"}}),
                    FakeResponse({"response": {"items": [{"id": 1}]}}),
                ]
                self.calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, *args, **kwargs):
                self.calls += 1
                return self.responses.pop(0)

        fake_client = FakeHttpClient()
        client = VkClientManager("", "")
        client._min_api_interval = 0

        with (
            patch("app.vk_client.httpx.AsyncClient", return_value=fake_client),
            patch("app.vk_client.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        ):
            response = await client.api_call("messages.getHistory", "token", peer_id=1)

        self.assertEqual(response, {"items": [{"id": 1}]})
        self.assertEqual(fake_client.calls, 3)
        self.assertEqual([call.args[0] for call in sleep_mock.await_args_list], [5.0, 10.0])

    async def test_action_flood_control_fails_immediately_for_account_failover(self):
        class FakeResponse:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class FakeHttpClient:
            def __init__(self):
                self.responses = [
                    FakeResponse({"error": {"error_code": 9, "error_msg": "Flood control"}}),
                ]

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, *args, **kwargs):
                return self.responses.pop(0)

        client = VkClientManager("", "")
        client._min_api_interval = 0

        with (
            patch("app.vk_client.httpx.AsyncClient", return_value=FakeHttpClient()),
            patch("app.vk_client.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        ):
            with self.assertRaises(VkFloodControlError):
                await client.api_call("messages.getHistory", "token", peer_id=1)

        sleep_mock.assert_not_awaited()

    async def test_vk_chat_falls_back_to_authorized_reserve_account(self):
        vk = AsyncMock()
        vk.get_messages.side_effect = [
            VkFloodControlError("Flood control"),
            [{"message_id": 7, "text": "Сообщение из резерва"}],
        ]
        primary = {"id": 1, "name": "Основной", "access_token": "one", "is_authorized": 1}
        reserve = {"id": 2, "name": "Резерв", "access_token": "two", "is_authorized": 1}
        chat = {
            "source": "vk",
            "source_account_id": 1,
            "source_chat_id": "2000000007",
            "telegram_id": 2000000007,
        }

        with (
            patch("app.source_router.get_vk_manager", return_value=vk),
            patch("app.source_router.db.get_vk_account", new=AsyncMock(return_value=primary)),
            patch("app.source_router.db.get_vk_accounts", new=AsyncMock(return_value=[primary, reserve])),
        ):
            messages = await fetch_chat_messages(
                chat,
                datetime.fromtimestamp(100),
                datetime.fromtimestamp(200),
            )

        self.assertEqual(messages[0]["message_id"], 7)
        self.assertEqual(
            [call.kwargs["access_token"] for call in vk.get_messages.await_args_list],
            ["one", "two"],
        )

    async def test_vk_chat_falls_back_to_authenticated_browser(self):
        vk = AsyncMock()
        vk.get_messages.side_effect = VkFloodControlError("Flood control")
        web = AsyncMock()
        web.get_messages.return_value = [{"message_id": 11, "text": "Из браузера"}]
        account = {"id": 1, "name": "Основной", "access_token": "one", "is_authorized": 1}
        chat = {
            "source": "vk",
            "source_account_id": 1,
            "source_chat_id": "2000000007",
            "telegram_id": 2000000007,
        }

        with (
            patch("app.source_router.get_vk_manager", return_value=vk),
            patch("app.source_router.get_vk_web_manager", return_value=web),
            patch("app.source_router.db.get_vk_account", new=AsyncMock(return_value=account)),
            patch("app.source_router.db.get_vk_accounts", new=AsyncMock(return_value=[account])),
        ):
            messages = await fetch_chat_messages(
                chat,
                datetime.fromtimestamp(100),
                datetime.fromtimestamp(200),
            )

        self.assertEqual(messages, [{"message_id": 11, "text": "Из браузера"}])
        web.get_messages.assert_awaited_once()

    async def test_reads_newest_first_and_stops_at_period_start(self):
        first_page = [
                {"id": 5, "date": 500, "from_id": 1, "text": "После периода"},
                {"id": 4, "date": 390, "from_id": 1, "text": "Новое"},
                {"id": 3, "date": 250, "from_id": 1, "text": "В периоде"},
        ] + [
            {"id": 10 + index, "date": 240, "from_id": 1, "text": ""}
            for index in range(197)
        ]
        client = FakeVkClient([
            {"items": first_page},
            {"items": [
                {"id": 2, "date": 190, "from_id": 1, "text": "До периода"},
                {"id": 1, "date": 100, "from_id": 1, "text": "Старое"},
            ]},
        ])

        messages = await client.get_messages(
            "token",
            peer_id=2_000_000_007,
            start_date=datetime.fromtimestamp(200),
            end_date=datetime.fromtimestamp(400),
        )

        self.assertEqual([message["message_id"] for message in messages], [3, 4])
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call[1]["rev"] == 0 for call in client.calls))


if __name__ == "__main__":
    unittest.main()
