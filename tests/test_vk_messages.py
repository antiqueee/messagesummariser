import unittest
from datetime import datetime

from app.vk_client import VkClientManager


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
