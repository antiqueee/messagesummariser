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
        self._clients: dict[int, object] = {}  # account_id -> MaxClient
        self._locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, account_id: int) -> asyncio.Lock:
        if account_id not in self._locks:
            self._locks[account_id] = asyncio.Lock()
        return self._locks[account_id]

    def _get_work_dir(self, account_id: int) -> str:
        path = MAX_SESSIONS_DIR / f"account_{account_id}"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    async def start_auth(self, account_id: int, phone: str) -> dict:
        """Start Max authentication (phone-based, sends SMS code)"""
        async with self._get_lock(account_id):
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

                return {'status': 'code_required', 'message': 'Подтвердите вход в приложении Max'}

            except ImportError as ie:
                raise RuntimeError(
                    f"maxapi-python not installed ({ie}). Run: pip install -U maxapi-python"
                )
            except Exception as e:
                import traceback
                traceback.print_exc()
                raise RuntimeError(f"Max auth error: {e}")

    async def connect_client(self, account_id: int, phone: str) -> bool:
        """Connect an already authenticated Max client"""
        async with self._get_lock(account_id):
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
                return True

            except Exception as e:
                print(f"[MaxClient] Failed to connect account {account_id}: {e}", flush=True)
                return False

    async def get_client(self, account_id: int) -> Optional[object]:
        """Get connected Max client for account"""
        return self._clients.get(account_id)

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
            if account_id in self._clients:
                client = self._clients.pop(account_id)
                try:
                    if hasattr(client, 'close'):
                        await client.close()
                    elif hasattr(client, 'disconnect'):
                        await client.disconnect()
                except Exception:
                    pass

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
