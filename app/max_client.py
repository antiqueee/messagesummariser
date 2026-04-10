# -*- coding: utf-8 -*-
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

MAX_SESSIONS_DIR = Path(__file__).parent.parent / "sessions" / "max"
MAX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


class MaxClientManager:
    """Manager for Max messenger client sessions using maxapi-python (pymax)"""

    def __init__(self):
        self._clients: dict[int, object] = {}  # account_id -> client
        self._tasks: dict[int, asyncio.Task] = {}  # account_id -> background task
        self._locks: dict[int, asyncio.Lock] = {}
        self._ready: dict[int, asyncio.Event] = {}  # signals when client is connected

    def _get_lock(self, account_id: int) -> asyncio.Lock:
        if account_id not in self._locks:
            self._locks[account_id] = asyncio.Lock()
        return self._locks[account_id]

    def _get_work_dir(self, account_id: int) -> str:
        path = MAX_SESSIONS_DIR / f"account_{account_id}"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    async def start_auth(self, account_id: int, phone: str) -> dict:
        """Start Max authentication by launching client in background"""
        async with self._get_lock(account_id):
            # Stop existing client if any
            if account_id in self._tasks:
                self._tasks[account_id].cancel()
                try:
                    await self._tasks[account_id]
                except (asyncio.CancelledError, Exception):
                    pass

            try:
                from pymax import SocketMaxClient
                from pymax.payloads import UserAgentPayload

                ua = UserAgentPayload(device_type="DESKTOP", app_version="25.12.13")
                client = SocketMaxClient(
                    phone=phone,
                    work_dir=self._get_work_dir(account_id),
                    headers=ua,
                )

                self._clients[account_id] = client
                self._ready[account_id] = asyncio.Event()

                # Register on_start handler to know when connected
                @client.on_start
                async def on_start():
                    print(f"[MaxClient] Account {account_id} connected!", flush=True)
                    self._ready[account_id].set()

                # Start client in background task
                async def run_client():
                    try:
                        print(f"[MaxClient] Starting auth for account {account_id}, phone: {phone}", flush=True)
                        await client.start()
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        print(f"[MaxClient] Client {account_id} error: {e}", flush=True)
                        import traceback
                        traceback.print_exc()

                self._tasks[account_id] = asyncio.create_task(run_client())

                # Wait a bit to see if it connects quickly (cached session)
                try:
                    await asyncio.wait_for(self._ready[account_id].wait(), timeout=5.0)
                    return {'status': 'success', 'message': 'Max аккаунт подключен (сессия из кеша)'}
                except asyncio.TimeoutError:
                    # Not connected yet - needs auth
                    return {
                        'status': 'auth_started',
                        'message': 'Авторизация запущена. Код должен прийти по SMS или в приложение Max. Проверьте терминал сервера.'
                    }

            except ImportError as ie:
                raise RuntimeError(
                    f"maxapi-python not installed ({ie}). Run: pip install -U maxapi-python"
                )
            except Exception as e:
                import traceback
                traceback.print_exc()
                raise RuntimeError(f"Max auth error: {e}")

    async def check_connected(self, account_id: int) -> bool:
        """Check if client is connected"""
        if account_id in self._ready:
            return self._ready[account_id].is_set()
        return False

    async def get_dialogs(self, account_id: int) -> list[dict]:
        """Get all dialogs (chats) from Max account.
        pymax stores group chats in client.chats (Chat objects with .id, .title)
        and private dialogs in client.dialogs (Dialog objects with .id, no .title)
        """
        client = self._clients.get(account_id)
        if not client:
            print(f"[MaxClient] No client for account {account_id}", flush=True)
            return []

        if account_id in self._ready and not self._ready[account_id].is_set():
            print(f"[MaxClient] Client {account_id} not yet connected", flush=True)
            return []

        result = []
        try:
            # Group chats - have .id and .title
            chats = getattr(client, 'chats', []) or []
            print(f"[MaxClient] Found {len(chats)} group chats", flush=True)
            for chat in chats:
                chat_id = getattr(chat, 'id', 0)
                title = getattr(chat, 'title', None) or f"Chat {chat_id}"
                chat_type = getattr(chat, 'type', 'group')
                result.append({
                    'chat_id': int(chat_id),
                    'title': str(title),
                    'type': str(chat_type),
                })

            # Private dialogs - have .id but no .title
            dialogs = getattr(client, 'dialogs', []) or []
            print(f"[MaxClient] Found {len(dialogs)} private dialogs", flush=True)
            for dialog in dialogs:
                dialog_id = getattr(dialog, 'id', 0)
                # Dialogs don't have title, use owner id or participants
                owner = getattr(dialog, 'owner', 0)
                title = f"Диалог {dialog_id}"
                result.append({
                    'chat_id': int(dialog_id),
                    'title': title,
                    'type': 'dialog',
                })

            print(f"[MaxClient] Total: {len(result)} chats/dialogs", flush=True)

        except Exception as e:
            print(f"[MaxClient] Error getting dialogs: {e}", flush=True)
            import traceback
            traceback.print_exc()

        return result

    async def get_messages(
            self,
            account_id: int,
            chat_id: int,
            start_date: datetime,
            end_date: datetime,
            limit: int = 10000,
    ) -> list[dict]:
        """Get messages from a Max chat within a date range"""
        client = self._clients.get(account_id)
        if not client:
            print(f"[MaxClient] No client for account {account_id}", flush=True)
            return []

        # Check if client is ready
        if account_id in self._ready and not self._ready[account_id].is_set():
            print(f"[MaxClient] Client {account_id} not yet connected", flush=True)
            return []

        start_naive = start_date.replace(tzinfo=None) if start_date.tzinfo else start_date
        end_naive = end_date.replace(tzinfo=None) if end_date.tzinfo else end_date

        print(f"[MaxClient] Fetching messages chat={chat_id}, period={start_naive} - {end_naive}", flush=True)

        messages = []
        try:
            history = await client.fetch_history(chat_id=chat_id)

            for msg in history:
                if not hasattr(msg, 'text') or not msg.text:
                    continue

                # Parse message date if available
                msg_date = None
                if hasattr(msg, 'date') and msg.date:
                    if isinstance(msg.date, datetime):
                        msg_date = msg.date.replace(tzinfo=None)
                    elif isinstance(msg.date, (int, float)):
                        msg_date = datetime.fromtimestamp(msg.date)

                # Filter by date range if we have date info
                if msg_date:
                    if msg_date < start_naive or msg_date > end_naive:
                        continue

                sender_name = 'Unknown'
                sender_id = 0
                if hasattr(msg, 'sender'):
                    sender_name = str(msg.sender) if msg.sender else 'Unknown'
                if hasattr(msg, 'sender_id'):
                    sender_id = msg.sender_id or 0
                elif hasattr(msg, 'from_id'):
                    sender_id = msg.from_id or 0

                msg_id = getattr(msg, 'id', 0)
                date_str = msg_date.isoformat() + 'Z' if msg_date else datetime.now().isoformat() + 'Z'

                messages.append({
                    'message_id': msg_id,
                    'sender_id': sender_id,
                    'sender_name': sender_name,
                    'text': msg.text,
                    'date': date_str,
                    'reply_to': None,
                    'topic_id': None
                })

            print(f"[MaxClient] Found {len(messages)} messages", flush=True)

        except Exception as e:
            print(f"[MaxClient] Error fetching messages: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return []

        # Return in chronological order
        return list(reversed(messages))

    async def disconnect_account(self, account_id: int):
        """Disconnect Max client for account"""
        async with self._get_lock(account_id):
            if account_id in self._tasks:
                self._tasks[account_id].cancel()
                try:
                    await self._tasks[account_id]
                except (asyncio.CancelledError, Exception):
                    pass
                del self._tasks[account_id]

            if account_id in self._clients:
                client = self._clients.pop(account_id)
                try:
                    if hasattr(client, 'close'):
                        await client.close()
                    elif hasattr(client, 'disconnect'):
                        await client.disconnect()
                except Exception:
                    pass

            if account_id in self._ready:
                del self._ready[account_id]

    async def close_all(self):
        """Close all Max client connections"""
        for account_id in list(self._clients.keys()):
            await self.disconnect_account(account_id)


# Global instance
_max_manager: Optional[MaxClientManager] = None


def init_max_manager() -> MaxClientManager:
    global _max_manager
    _max_manager = MaxClientManager()
    return _max_manager


def get_max_manager() -> MaxClientManager:
    if _max_manager is None:
        raise RuntimeError("Max manager not initialized")
    return _max_manager
