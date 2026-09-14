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


class FakeHistoryClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def fetch_history(self, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


class FakeEngine:
    def __init__(self):
        self.disposed = False

    def dispose(self):
        self.disposed = True


class FakeAuthDatabase:
    def __init__(self):
        self.engine = FakeEngine()
        self.saved = None

    def update_auth_token(self, device_id, token):
        self.saved = (device_id, token)


class FakeAuthClient:
    def __init__(self, response, password_response=None):
        self.response = response
        self.password_response = password_response
        self._database = FakeAuthDatabase()
        self._device_id = "device-1"
        self._token = None
        self.closed = False
        self.cleaned = False

    async def _send_code(self, code, temp_token):
        return self.response

    async def _check_password(self, password, track_id):
        return self.password_response

    async def close(self):
        self.closed = True

    async def _cleanup_client(self):
        self.cleaned = True


class MaxSenderNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_empty_history_page_is_retried(self):
        manager = MaxClientManager()
        client = FakeHistoryClient([[], [SimpleNamespace(id=42)]])
        manager._clients[9] = client

        with patch("app.max_client.asyncio.sleep", return_value=None):
            history = await manager._fetch_history_with_retry(
                account_id=9,
                chat_id=-123,
                from_time=1_000,
                backward=200,
                retry_empty=True,
            )

        self.assertEqual(client.calls, 2)
        self.assertEqual(history[0].id, 42)

    def test_audio_attachment_allows_missing_transcription_status(self):
        MaxClientManager()
        from pymax.types import AudioAttach

        attachment = AudioAttach.from_dict({
            "_type": "AUDIO",
            "duration": 1200,
            "audioId": 42,
            "url": "https://example.test/audio",
            "wave": "",
            "token": "token",
        })

        self.assertEqual(attachment.audio_id, 42)
        self.assertEqual(attachment.transcription_status, "")

    def test_video_attachment_allows_missing_duration(self):
        manager = MaxClientManager()
        from pymax.types import Message, VideoAttach

        attachment = VideoAttach.from_dict({
            "_type": "VIDEO",
            "videoId": 42,
            "thumbnail": "https://example.test/preview.jpg",
            "token": "token",
        })

        self.assertEqual(attachment.video_id, 42)
        self.assertEqual(attachment.duration, 0)

        message = Message.from_dict({
            "chatId": -71003461216938,
            "message": {
                "id": 100,
                "time": 1_788_800_000_000,
                "text": "",
                "type": "USER",
                "sender": 74481502,
                "attaches": [{
                    "_type": "VIDEO",
                    "videoId": 42,
                    "thumbnail": "https://example.test/preview.jpg",
                    "token": "token",
                }],
            },
        })
        normalized = manager._normalize_message(message)

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["text"], "[Видео]")
        self.assertEqual(normalized["message_id"], 100)

    def test_other_media_attachments_allow_missing_optional_metadata(self):
        MaxClientManager()
        from pymax.types import FileAttach, PhotoAttach, StickerAttach

        photo = PhotoAttach.from_dict({"_type": "PHOTO", "photoId": 1})
        file = FileAttach.from_dict({"_type": "FILE", "fileId": 2})
        sticker = StickerAttach.from_dict({"_type": "STICKER", "stickerId": 3})

        self.assertEqual(photo.photo_id, 1)
        self.assertEqual(file.file_id, 2)
        self.assertEqual(sticker.sticker_id, 3)

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

    async def test_auth_code_saves_token_and_disposes_temporary_client(self):
        manager = MaxClientManager()
        client = FakeAuthClient({"tokenAttrs": {"LOGIN": {"token": "saved-token"}}})
        manager._clients[7] = client
        manager._phones[7] = "+70000000000"
        manager._auth_tokens[7] = "temporary-token"

        result = await manager.complete_auth_code(7, "123456")

        self.assertEqual(result["status"], "success")
        self.assertEqual(client._database.saved, ("device-1", "saved-token"))
        self.assertTrue(client.closed)
        self.assertTrue(client.cleaned)
        self.assertTrue(client._database.engine.disposed)
        self.assertNotIn(7, manager._clients)

    async def test_auth_code_returns_password_challenge_without_blocking(self):
        manager = MaxClientManager()
        client = FakeAuthClient(
            {"passwordChallenge": {"trackId": "track-1", "hint": "подсказка"}},
            {"LOGIN": {"token": "2fa-token"}},
        )
        manager._clients[8] = client
        manager._phones[8] = "+70000000001"
        manager._auth_tokens[8] = "temporary-token"

        challenge = await manager.complete_auth_code(8, "123456")
        self.assertEqual(challenge["status"], "password_required")
        self.assertIn(8, manager._clients)

        result = await manager.complete_auth_code(8, "123456", "secret")
        self.assertEqual(result["status"], "success")
        self.assertEqual(client._database.saved, ("device-1", "2fa-token"))


if __name__ == "__main__":
    unittest.main()
