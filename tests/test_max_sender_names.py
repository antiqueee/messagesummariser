import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
    def test_user_agent_uses_supported_max_version(self):
        with patch.dict("os.environ", {}, clear=True):
            user_agent = MaxClientManager._build_user_agent()

        self.assertEqual(user_agent.app_version, "26.25.0")
        self.assertEqual(user_agent.build_number, 6790)

    def test_user_profile_contains_identity_avatar_and_deep_link(self):
        user = SimpleNamespace(
            id=74481502,
            names=[SimpleNamespace(
                type="ONEME",
                name="Иван Петров",
                first_name=None,
                last_name=None,
            )],
            username=None,
            link="ivan_petrov",
            base_url="https://i.oneme.ru/avatar-small.jpg",
            base_raw_url="https://i.oneme.ru/avatar-full.jpg",
        )

        profile = MaxClientManager._max_user_to_profile(user, 74481502)

        self.assertEqual(profile["display_name"], "Иван Петров")
        self.assertEqual(profile["username"], "ivan_petrov")
        self.assertEqual(profile["avatar_url"], "https://i.oneme.ru/avatar-small.jpg")
        self.assertEqual(profile["photos"], [{
            "thumbnail_url": "https://i.oneme.ru/avatar-small.jpg",
            "full_url": "https://i.oneme.ru/avatar-full.jpg",
            "label": "Текущая фотография профиля",
        }])
        self.assertEqual(profile["profile_link"], "max://user/74481502")
        self.assertEqual(profile["public_link"], "https://max.ru/ivan_petrov")

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
