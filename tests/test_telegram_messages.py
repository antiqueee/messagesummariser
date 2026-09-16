import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app.telegram_client import TelegramClientManager


MOSCOW = ZoneInfo("Europe/Moscow")
UTC = timezone.utc


def telegram_message(mid: int, when: datetime, text: str):
    return SimpleNamespace(
        id=mid,
        date=when,
        text=text,
        sender_id=mid,
        sender=None,
        reply_to=None,
        reply_to_msg_id=None,
    )


class FakeTelegramClient:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    async def iter_messages(self, chat_id, **kwargs):
        self.calls.append((chat_id, kwargs))
        batch = self.batches.pop(0)
        for message in batch:
            yield message


class TelegramMessageWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_naive_moscow_report_period_is_compared_in_utc(self):
        client = FakeTelegramClient([[
            telegram_message(3, datetime(2026, 9, 16, 8, 41, tzinfo=UTC), "Позднее"),
            telegram_message(2, datetime(2026, 9, 16, 5, 30, tzinfo=UTC), "Утреннее"),
            telegram_message(1, datetime(2026, 9, 16, 4, 59, tzinfo=UTC), "До периода"),
        ]])
        manager = TelegramClientManager(1, "hash", use_proxy=False)
        manager.get_client = AsyncMock(return_value=client)

        result = await manager._get_messages_unlocked(
            account_id=1,
            chat_telegram_id=-1001,
            start_date=datetime(2026, 9, 16, 8, 0),
            end_date=datetime(2026, 9, 16, 11, 48),
        )

        self.assertEqual([item["message_id"] for item in result], [2, 3])
        offset_date = client.calls[0][1]["offset_date"]
        self.assertEqual(offset_date, datetime(2026, 9, 16, 8, 48, 1, tzinfo=UTC))

    async def test_live_period_reconciles_newest_tail_and_deduplicates(self):
        end_local = datetime.now(MOSCOW).replace(tzinfo=None) - timedelta(minutes=1)
        end_utc = end_local.replace(tzinfo=MOSCOW).astimezone(UTC)
        older = telegram_message(20, end_utc - timedelta(minutes=5), "Первое сообщение")
        recovered = telegram_message(21, end_utc - timedelta(minutes=1), "Свежий хвост")
        client = FakeTelegramClient([
            [older],
            [recovered, older],
        ])
        manager = TelegramClientManager(1, "hash", use_proxy=False)
        manager.get_client = AsyncMock(return_value=client)

        result = await manager._get_messages_unlocked(
            account_id=1,
            chat_telegram_id=-1001,
            start_date=end_local - timedelta(hours=1),
            end_date=end_local,
        )

        self.assertEqual([item["message_id"] for item in result], [20, 21])
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("offset_date", client.calls[1][1])


if __name__ == "__main__":
    unittest.main()
