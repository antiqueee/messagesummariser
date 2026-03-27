import os
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, AsyncGenerator
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import User, Chat, Channel, Message, ForumTopic
from telethon.tl.functions.channels import GetForumTopicsRequest
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

SESSIONS_DIR = Path(__file__).parent.parent / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


class TelegramClientManager:
    """Manager for multiple Telegram client sessions"""

    def __init__(self, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self._clients: dict[int, TelegramClient] = {}
        self._pending_auth: dict[int, dict] = {}  # account_id -> {client, phone_code_hash}
        self._locks: dict[int, asyncio.Lock] = {}  # Lock per account to prevent concurrent access

    def _get_session_path(self, account_id: int) -> Path:
        return SESSIONS_DIR / f"account_{account_id}"

    def _get_lock(self, account_id: int) -> asyncio.Lock:
        """Get or create a lock for the given account"""
        if account_id not in self._locks:
            self._locks[account_id] = asyncio.Lock()
        return self._locks[account_id]

    async def get_client(self, account_id: int) -> Optional[TelegramClient]:
        """Get or create a client for the given account"""
        # Quick check without lock
        if account_id in self._clients:
            client = self._clients[account_id]
            if client.is_connected():
                return client

        # Use lock to prevent concurrent session access (SQLite locking issues)
        async with self._get_lock(account_id):
            # Double-check after acquiring lock
            if account_id in self._clients:
                client = self._clients[account_id]
                if client.is_connected():
                    return client

            session_path = self._get_session_path(account_id)
            if not session_path.with_suffix('.session').exists():
                return None

            client = TelegramClient(
                str(session_path),
                self.api_id,
                self.api_hash
            )
            await client.connect()

            if await client.is_user_authorized():
                self._clients[account_id] = client
                return client

            return None

    async def start_auth(self, account_id: int, phone: str) -> dict:
        """Start authentication process for a new account"""
        async with self._get_lock(account_id):
            session_path = self._get_session_path(account_id)
            print(f"[Auth] Starting auth for account {account_id}, phone: {phone}")

            client = TelegramClient(
                str(session_path),
                self.api_id,
                self.api_hash
            )
            await client.connect()
            print(f"[Auth] Connected to Telegram")

            try:
                result = await client.send_code_request(phone)
                print(f"[Auth] Code sent! Type: {result.type}, phone_code_hash: {result.phone_code_hash[:10]}...")
            except Exception as e:
                print(f"[Auth] ERROR sending code: {e}")
                import traceback
                traceback.print_exc()
                raise

            self._pending_auth[account_id] = {
                'client': client,
                'phone': phone,
                'phone_code_hash': result.phone_code_hash
            }

            return {'status': 'code_required', 'phone_code_hash': result.phone_code_hash}

    async def complete_auth(self, account_id: int, code: str,
                            password: Optional[str] = None) -> dict:
        """Complete authentication with the received code"""
        async with self._get_lock(account_id):
            if account_id not in self._pending_auth:
                return {'status': 'error', 'message': 'No pending authentication found'}

            auth_data = self._pending_auth[account_id]
            client = auth_data['client']
            phone = auth_data['phone']
            phone_code_hash = auth_data['phone_code_hash']

            try:
                await client.sign_in(
                    phone=phone,
                    code=code,
                    phone_code_hash=phone_code_hash
                )
            except SessionPasswordNeededError:
                if password:
                    await client.sign_in(password=password)
                else:
                    return {'status': 'password_required'}
            except PhoneCodeInvalidError:
                return {'status': 'error', 'message': 'Invalid code'}
            except Exception as e:
                return {'status': 'error', 'message': str(e)}

            if await client.is_user_authorized():
                self._clients[account_id] = client
                del self._pending_auth[account_id]
                return {'status': 'success'}

            return {'status': 'error', 'message': 'Authorization failed'}

    async def disconnect_account(self, account_id: int):
        """Disconnect and remove client session"""
        async with self._get_lock(account_id):
            if account_id in self._clients:
                await self._clients[account_id].disconnect()
                del self._clients[account_id]

            if account_id in self._pending_auth:
                await self._pending_auth[account_id]['client'].disconnect()
                del self._pending_auth[account_id]

            # Remove session files
            session_path = self._get_session_path(account_id)
            for suffix in ['.session', '.session-journal']:
                path = session_path.with_suffix(suffix)
                if path.exists():
                    path.unlink()

    async def get_dialogs(self, account_id: int) -> list[dict]:
        """Get all dialogs (chats) for an account"""
        client = await self.get_client(account_id)
        if not client:
            return []

        dialogs = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity

            # Determine chat type
            if isinstance(entity, User):
                chat_type = 'user'
            elif isinstance(entity, Chat):
                chat_type = 'group'
            elif isinstance(entity, Channel):
                chat_type = 'supergroup' if entity.megagroup else 'channel'
            else:
                chat_type = 'unknown'

            # We're interested in groups and supergroups (chats)
            if chat_type in ['group', 'supergroup']:
                dialogs.append({
                    'telegram_id': dialog.id,
                    'title': dialog.title or dialog.name or 'Unknown',
                    'type': chat_type,
                    'unread_count': dialog.unread_count,
                    'participants_count': getattr(entity, 'participants_count', None)
                })

        return dialogs

    async def get_forum_topics(self, account_id: int, chat_telegram_id: int) -> list[dict]:
        """Get forum topics for a chat (if it's a forum)"""
        client = await self.get_client(account_id)
        if not client:
            return []

        try:
            # Get entity to check if it's a forum
            entity = await client.get_entity(chat_telegram_id)
            if not getattr(entity, 'forum', False):
                return []  # Not a forum chat

            # Get topics
            result = await client(GetForumTopicsRequest(
                channel=chat_telegram_id,
                offset_date=None,
                offset_id=0,
                offset_topic=0,
                limit=100
            ))

            topics = []
            for topic in result.topics:
                if isinstance(topic, ForumTopic):
                    topics.append({
                        'id': topic.id,
                        'title': topic.title,
                        'icon_emoji': getattr(topic, 'icon_emoji_id', None),
                    })

            return topics

        except Exception as e:
            print(f"[get_forum_topics] Error: {e}")
            return []

    async def is_forum_chat(self, account_id: int, chat_telegram_id: int) -> bool:
        """Check if a chat is a forum (has topics)"""
        client = await self.get_client(account_id)
        if not client:
            return False

        try:
            entity = await client.get_entity(chat_telegram_id)
            return getattr(entity, 'forum', False)
        except Exception:
            return False

    async def get_messages(
            self,
            account_id: int,
            chat_telegram_id: int,
            start_date: datetime,
            end_date: datetime,
            limit: int = 10000,
            topic_ids: list[int] = None,
            timeout_seconds: int = 60
    ) -> list[dict]:
        """Get messages from a chat within a date range, optionally filtered by topic IDs"""
        client = await self.get_client(account_id)
        if not client:
            print(f"[get_messages] No client for account {account_id}")
            return []

        # Strip timezone info for consistent comparison
        start_naive = start_date.replace(tzinfo=None) if start_date.tzinfo else start_date
        end_naive = end_date.replace(tzinfo=None) if end_date.tzinfo else end_date

        topic_filter = set(topic_ids) if topic_ids else None
        print(f"[get_messages] chat={chat_telegram_id}, period={start_naive} - {end_naive}, topics={topic_filter}", flush=True)

        messages = []

        async def fetch_messages():
            nonlocal messages
            async for message in client.iter_messages(
                    chat_telegram_id,
                    offset_date=end_naive,
                    reverse=False,
                    limit=limit
            ):
                msg_date = message.date.replace(tzinfo=None)

                if msg_date < start_naive:
                    break

                if msg_date > end_naive:
                    continue

                # Skip non-text messages
                if not message.text:
                    continue

                # Filter by topic if specified
                if topic_filter is not None:
                    # Get topic ID from message (reply_to contains topic info in forums)
                    msg_topic_id = None
                    if hasattr(message, 'reply_to') and message.reply_to:
                        # In forums, reply_to_top_id is the topic ID
                        msg_topic_id = getattr(message.reply_to, 'reply_to_top_id', None)
                        if msg_topic_id is None:
                            msg_topic_id = getattr(message.reply_to, 'reply_to_msg_id', None)

                    # Messages in General topic have no reply_to, but topic ID is 1
                    if msg_topic_id is None:
                        msg_topic_id = 1  # General topic

                    if msg_topic_id not in topic_filter:
                        continue

                sender_name = 'Unknown'
                sender_id = 0

                if message.sender:
                    sender_id = message.sender_id
                    if isinstance(message.sender, User):
                        sender_name = ' '.join(filter(None, [
                            message.sender.first_name,
                            message.sender.last_name
                        ])) or message.sender.username or 'User'
                    else:
                        sender_name = getattr(message.sender, 'title', 'Unknown')

                # Get topic ID for reference
                msg_topic_id = None
                if hasattr(message, 'reply_to') and message.reply_to:
                    msg_topic_id = getattr(message.reply_to, 'reply_to_top_id', None)

                messages.append({
                    'message_id': message.id,
                    'sender_id': sender_id,
                    'sender_name': sender_name,
                    'text': message.text,
                    'date': msg_date.isoformat() + 'Z',
                    'reply_to': message.reply_to_msg_id,
                    'topic_id': msg_topic_id
                })

        try:
            await asyncio.wait_for(fetch_messages(), timeout=timeout_seconds)
            print(f"[get_messages] Found {len(messages)} messages", flush=True)

        except asyncio.TimeoutError:
            print(f"[get_messages] Timeout after {timeout_seconds}s, got {len(messages)} messages so far", flush=True)

        except Exception as e:
            print(f"[get_messages] Error fetching messages: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return []

        # Return in chronological order
        return list(reversed(messages))

    async def close_all(self):
        """Close all client connections"""
        for client in self._clients.values():
            await client.disconnect()
        self._clients.clear()

        for auth_data in self._pending_auth.values():
            await auth_data['client'].disconnect()
        self._pending_auth.clear()


# Global instance (will be initialized in main.py)
telegram_manager: Optional[TelegramClientManager] = None


def init_telegram_manager(api_id: int, api_hash: str):
    global telegram_manager
    telegram_manager = TelegramClientManager(api_id, api_hash)
    return telegram_manager


def get_telegram_manager() -> TelegramClientManager:
    if telegram_manager is None:
        raise RuntimeError("Telegram manager not initialized")
    return telegram_manager
