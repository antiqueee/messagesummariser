import unittest
from unittest.mock import AsyncMock

from telethon.errors import SendCodeUnavailableError
from app.telegram_client import TelegramClientManager


class SentCodeTypeApp:
    pass


class SentCodeTypeSms:
    pass


class FakeSentCode:
    def __init__(self, delivery, next_delivery=None, timeout=0, phone_code_hash="hash-2"):
        self.type = delivery
        self.next_type = next_delivery
        self.timeout = timeout
        self.phone_code_hash = phone_code_hash


class FakeAuthClient:
    def __init__(self, response):
        self.response = response
        self.requested_phones = []

    def is_connected(self):
        return True

    async def send_code_request(self, phone):
        self.requested_phones.append(phone)
        return self.response


class TelegramAuthTests(unittest.IsolatedAsyncioTestCase):
    def test_delivery_payload_exposes_current_next_route_and_timeout(self):
        result = FakeSentCode(SentCodeTypeApp(), SentCodeTypeSms(), timeout=42)

        payload = TelegramClientManager._auth_delivery_payload(result)

        self.assertEqual(payload["delivery_type"], "app")
        self.assertEqual(payload["next_delivery_type"], "sms")
        self.assertEqual(payload["resend_available_in"], 42)

    async def test_resend_reuses_pending_client_and_updates_hash(self):
        manager = TelegramClientManager(1, "hash", use_proxy=False)
        response = FakeSentCode(SentCodeTypeSms(), timeout=30)
        client = FakeAuthClient(response)
        manager._pending_auth[7] = {
            "client": client,
            "phone": "+70000000000",
            "phone_code_hash": "hash-1",
        }

        result = await manager.resend_auth(7)

        self.assertEqual(client.requested_phones, ["+70000000000"])
        self.assertEqual(result["status"], "code_required")
        self.assertEqual(result["delivery_type"], "sms")
        self.assertEqual(manager._pending_auth[7]["phone_code_hash"], "hash-2")

    async def test_resend_without_pending_auth_explains_recovery(self):
        manager = TelegramClientManager(1, "hash", use_proxy=False)

        result = await manager.resend_auth(7)

        self.assertEqual(result["status"], "error")
        self.assertIn("запросите код", result["message"])

    async def test_start_with_pending_auth_does_not_resend(self):
        manager = TelegramClientManager(1, "hash", use_proxy=False)
        client = FakeAuthClient(FakeSentCode(SentCodeTypeSms()))
        manager._pending_auth[7] = {
            "client": client,
            "phone": "+70000000000",
            "phone_code_hash": "hash-1",
            "delivery": {
                "delivery_type": "app",
                "next_delivery_type": None,
                "resend_available_in": 0,
            },
        }

        result = await manager.start_auth(7, "+70000000000")

        self.assertEqual(client.requested_phones, [])
        self.assertTrue(result["request_pending"])
        self.assertEqual(result["delivery_type"], "app")

    async def test_unavailable_resend_returns_russian_explanation(self):
        manager = TelegramClientManager(1, "hash", use_proxy=False)
        client = FakeAuthClient(None)
        client.send_code_request = AsyncMock(
            side_effect=SendCodeUnavailableError(request=None)
        )
        manager._pending_auth[7] = {
            "client": client,
            "phone": "+70000000000",
            "phone_code_hash": "hash-1",
        }

        result = await manager.resend_auth(7)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "delivery_unavailable")
        self.assertIn("Telegram", result["message"])


if __name__ == "__main__":
    unittest.main()
