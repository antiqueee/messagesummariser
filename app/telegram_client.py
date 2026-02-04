import os
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, AsyncGenerator
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import User, Chat, Channel, Message
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

    def _get_session_path(self, account_id: int) -> Path:
        return SESSIONS_DIR / f"account_{account_id}"

    async def get_client(self, account_id: int) -> Optional[TelegramClient]:
        """Get or create a client for the given account"""
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
        session_path = self._get_session_path(account_id)

        client = TelegramClient(
            str(session_path),
            self.api_id,
            self.api_hash
        )
        await client.connect()

        result = await client.send_code_request(phone)

        self._pending_auth[account_id] = {
            'client': client,
            'phone': phone,
            'phone_code_hash': result.phone_code_hash
        }

        return {'status': 'code_required', 'phone_code_hash': result.phone_code_hash}

    async def complete_auth(self, account_id: int, code: str,
                            password: Optional[str] = None) -> dict:
        """Complete authentication with the received code"""
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

    async def get_messages(
            self,
            account_id: int,
            chat_telegram_id: int,
            start_date: datetime,
            end_date: datetime,
            limit: int = 10000
    ) -> list[dict]:
        """Get messages from a chat within a date range"""
        client = await self.get_client(account_id)
        if not client:
            return []

        messages = []

        try:
            async for message in client.iter_messages(
                    chat_telegram_id,
                    offset_date=end_date,
                    reverse=False,
                    limit=limit
            ):
                if message.date.replace(tzinfo=None) < start_date:
                    break

                if message.date.replace(tzinfo=None) > end_date:
                    continue

                # Skip non-text messages
                if not message.text:
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

                messages.append({
                    'message_id': message.id,
                    'sender_id': sender_id,
                    'sender_name': sender_name,
                    'text': message.text,
                    'date': message.date.replace(tzinfo=None).isoformat(),
                    'reply_to': message.reply_to_msg_id
                })

        except Exception as e:
            print(f"Error fetching messages: {e}")
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
