import unittest
from types import SimpleNamespace

from app.max_client import MaxClientManager


class FakeMaxClient:
    def __init__(self):
        self.requested_ids = []

    async def get_users(self, user_ids):
        self.requested_ids.extend(user_ids)
        return [
            SimpleNamespace(
                id=74481502,
                names=[SimpleNamespace(
                    type="ONEME",
                    name="Иван Петров",
                    first_name=None,
                    last_name=None,
                )],
            )
        ]


class MaxSenderNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_sender_is_replaced_with_profile_name(self):
        manager = MaxClientManager()
        client = FakeMaxClient()
        messages = [{
            "sender_id": 74481502,
            "sender_name": "74481502",
            "text": "Сообщение",
        }]

        await manager._resolve_message_sender_names(client, messages)

        self.assertEqual(client.requested_ids, [74481502])
        self.assertEqual(messages[0]["sender_name"], "Иван Петров")

    async def test_existing_sender_name_is_not_looked_up(self):
        manager = MaxClientManager()
        client = FakeMaxClient()
        messages = [{
            "sender_id": 74481502,
            "sender_name": "Уже известное имя",
            "text": "Сообщение",
        }]

        await manager._resolve_message_sender_names(client, messages)

        self.assertEqual(client.requested_ids, [])
        self.assertEqual(messages[0]["sender_name"], "Уже известное имя")


if __name__ == "__main__":
    unittest.main()
