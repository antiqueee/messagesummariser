import asyncio
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

import httpx


VK_AUTHORIZE_URL = "https://oauth.vk.com/authorize"
VK_ACCESS_TOKEN_URL = "https://oauth.vk.com/access_token"
VK_API_URL = "https://api.vk.com/method"
VK_API_VERSION = "5.199"


class VkClientManager:
    """Minimal VK OAuth/API manager for multi-account chat monitoring."""

    def __init__(self, app_id: str, app_secret: str, service_token: Optional[str] = None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.service_token = service_token
        self._pending_states: dict[str, int] = {}

    @property
    def oauth_enabled(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def build_redirect_uri(self, request_base_url: str) -> str:
        return f"{request_base_url.rstrip('/')}/api/vk/auth/callback"

    def build_auth_url(self, account_id: int, request_base_url: str) -> str:
        if not self.oauth_enabled:
            raise RuntimeError("VK OAuth не настроен: отсутствуют VK_APP_ID или VK_APP_SECRET")
        state = secrets.token_urlsafe(24)
        self._pending_states[state] = account_id
        params = {
            "client_id": self.app_id,
            "redirect_uri": self.build_redirect_uri(request_base_url),
            "response_type": "code",
            "scope": "messages,offline",
            "v": VK_API_VERSION,
            "state": state,
            "display": "page",
        }
        return f"{VK_AUTHORIZE_URL}?{urlencode(params)}"

    def pop_pending_account_id(self, state: str) -> Optional[int]:
        return self._pending_states.pop(state, None)

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
    ) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                VK_ACCESS_TOKEN_URL,
                params={
                    "client_id": self.app_id,
                    "client_secret": self.app_secret,
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if "error" in payload:
                error_msg = payload.get("error_description") or payload.get("error")
                raise RuntimeError(f"VK token exchange failed: {error_msg}")
            return payload

    async def api_call(self, method: str, access_token: str, **params) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as client:
            last_msg = None
            for attempt in range(5):
                response = await client.get(
                    f"{VK_API_URL}/{method}",
                    params={
                        **params,
                        "access_token": access_token,
                        "v": VK_API_VERSION,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                if "error" not in payload:
                    return payload["response"]

                error = payload["error"]
                msg = error.get("error_msg") or str(error)
                last_msg = msg
                if error.get("error_code") != 6 or attempt == 4:
                    raise RuntimeError(f"VK API {method} failed: {msg}")

                delay = 0.4 * (attempt + 1)
                print(f"[VK] Rate limited on {method}, retrying in {delay:.1f}s", flush=True)
                await asyncio.sleep(delay)

            raise RuntimeError(f"VK API {method} failed: {last_msg}")

    async def get_current_user(self, access_token: str) -> dict:
        users = await self.api_call("users.get", access_token, fields="screen_name")
        if not users:
            raise RuntimeError("VK users.get returned empty response")
        return users[0]

    async def get_dialogs(self, access_token: str, limit: int = 1000) -> list[dict]:
        dialogs: list[dict] = []
        offset = 0

        while len(dialogs) < limit:
            response = await self.api_call(
                "messages.getConversations",
                access_token,
                offset=offset,
                count=min(200, limit - len(dialogs)),
                extended=0,
            )

            items = response.get("items", [])
            if not items:
                break

            for item in items:
                conversation = item.get("conversation") or {}
                peer = conversation.get("peer") or {}
                peer_id = peer.get("id")
                if not peer_id:
                    continue

                title = None
                settings = conversation.get("chat_settings") or {}
                title = settings.get("title")
                if not title:
                    peer_type = peer.get("type", "dialog")
                    local_id = peer.get("local_id") or peer_id
                    title = f"{peer_type}:{local_id}"

                dialogs.append({
                    "chat_id": int(peer_id),
                    "title": title,
                    "type": peer.get("type", "unknown"),
                })

            offset += len(items)
            if len(items) < 200:
                break

        return dialogs

    async def get_messages(
        self,
        access_token: str,
        peer_id: int,
        start_date: datetime,
        end_date: datetime,
        limit: int = 10000,
    ) -> list[dict]:
        start_ts = int(start_date.replace(tzinfo=None).timestamp())
        end_ts = int(end_date.replace(tzinfo=None).timestamp())

        messages: list[dict] = []
        offset = 0
        sender_cache: dict[int, str] = {}

        while len(messages) < limit:
            response = await self.api_call(
                "messages.getHistory",
                access_token,
                peer_id=peer_id,
                offset=offset,
                count=min(200, limit - len(messages)),
                rev=1,
                extended=1,
            )

            items = response.get("items", [])
            if not items:
                break

            self._prime_sender_cache_from_extended_response(response, sender_cache)
            await self._prime_sender_cache(access_token, items, sender_cache)

            for item in items:
                msg_ts = int(item.get("date", 0))
                if msg_ts < start_ts:
                    continue
                if msg_ts > end_ts:
                    continue

                text = (item.get("text") or "").strip()
                if not text:
                    text = self._attachments_placeholder(item)
                    if not text:
                        continue

                sender_id = int(item.get("from_id") or 0)
                messages.append({
                    "message_id": int(item.get("id") or 0),
                    "sender_id": sender_id,
                    "sender_name": sender_cache.get(sender_id) or str(sender_id or "Unknown"),
                    "text": text,
                    "date": datetime.fromtimestamp(msg_ts).isoformat() + "Z",
                    "reply_to": (item.get("reply_message") or {}).get("id"),
                    "topic_id": None,
                })

                if len(messages) >= limit:
                    break

            offset += len(items)
            if len(items) < 200:
                break

        return messages

    def token_expiry_iso(self, expires_in: Optional[int]) -> Optional[str]:
        if not expires_in or expires_in <= 0:
            return None
        return (datetime.now() + timedelta(seconds=expires_in)).isoformat()

    async def _prime_sender_cache(self, access_token: str, items: list[dict], sender_cache: dict[int, str]):
        user_ids = sorted({int(item.get("from_id")) for item in items if int(item.get("from_id") or 0) > 0 and int(item.get("from_id")) not in sender_cache})
        group_ids = sorted({abs(int(item.get("from_id"))) for item in items if int(item.get("from_id") or 0) < 0 and int(item.get("from_id")) not in sender_cache})

        if user_ids:
            for chunk_start in range(0, len(user_ids), 500):
                chunk = user_ids[chunk_start:chunk_start + 500]
                users = await self.api_call("users.get", access_token, user_ids=",".join(map(str, chunk)))
                for user in users:
                    sender_cache[int(user["id"])] = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or str(user["id"])

        if group_ids:
            for chunk_start in range(0, len(group_ids), 500):
                chunk = group_ids[chunk_start:chunk_start + 500]
                groups = await self.api_call("groups.getById", access_token, group_ids=",".join(map(str, chunk)))
                for group in groups:
                    sender_cache[-int(group["id"])] = group.get("name") or f"club{group['id']}"

    def _prime_sender_cache_from_extended_response(self, response: dict, sender_cache: dict[int, str]) -> None:
        for user in response.get("profiles") or []:
            user_id = int(user.get("id") or 0)
            if not user_id:
                continue
            sender_cache[user_id] = " ".join(
                filter(None, [user.get("first_name"), user.get("last_name")])
            ) or str(user_id)

        for group in response.get("groups") or []:
            group_id = int(group.get("id") or 0)
            if not group_id:
                continue
            sender_cache[-group_id] = group.get("name") or f"club{group_id}"

    def _attachments_placeholder(self, item: dict) -> str:
        attachments = item.get("attachments") or []
        if not attachments:
            action = item.get("action") or {}
            if action:
                return f"[Системное событие: {action.get('type') or 'action'}]"
            return ""

        labels = []
        for attachment in attachments:
            atype = attachment.get("type")
            if atype == "photo":
                labels.append("[Фото]")
            elif atype == "video":
                labels.append("[Видео]")
            elif atype == "audio_message":
                labels.append("[Голосовое сообщение]")
            elif atype == "doc":
                labels.append("[Файл]")
            elif atype == "sticker":
                labels.append("[Стикер]")
            else:
                labels.append(f"[{atype or 'Вложение'}]")
        return " ".join(labels)


vk_manager: Optional[VkClientManager] = None


def init_vk_manager() -> Optional[VkClientManager]:
    global vk_manager
    app_id = os.getenv("VK_APP_ID", "").strip()
    app_secret = os.getenv("VK_APP_SECRET", "").strip()
    service_token = os.getenv("VK_SERVICE_TOKEN", "").strip() or None
    vk_manager = VkClientManager(app_id, app_secret, service_token=service_token)
    return vk_manager


def get_vk_manager() -> VkClientManager:
    if vk_manager is None:
        raise RuntimeError("VK manager not initialized")
    return vk_manager
