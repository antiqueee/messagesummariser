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
        self._phones: dict[int, str] = {}

    def _get_lock(self, account_id: int) -> asyncio.Lock:
        if account_id not in self._locks:
            self._locks[account_id] = asyncio.Lock()
        return self._locks[account_id]

    def _get_work_dir(self, account_id: int) -> str:
        path = MAX_SESSIONS_DIR / f"account_{account_id}"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _is_socket_connected(self, account_id: int) -> bool:
        client = self._clients.get(account_id)
        return bool(client and getattr(client, "is_connected", False))

    def _mark_disconnected(self, account_id: int) -> None:
        if account_id in self._ready:
            self._ready[account_id].clear()

    def _extract_message_datetime(self, msg) -> Optional[datetime]:
        """Best-effort datetime extraction for pymax messages."""
        raw_date = getattr(msg, 'date', None)
        if isinstance(raw_date, datetime):
            return raw_date.replace(tzinfo=None)
        if isinstance(raw_date, (int, float)):
            timestamp = raw_date / 1000 if raw_date > 10**11 else raw_date
            return datetime.fromtimestamp(timestamp)

        raw_time = getattr(msg, 'time', None)
        if isinstance(raw_time, (int, float)):
            timestamp = raw_time / 1000 if raw_time > 10**11 else raw_time
            return datetime.fromtimestamp(timestamp)

        return None

    def _normalize_message(self, msg) -> Optional[dict]:
        """Convert pymax message into app message format."""
        text = getattr(msg, 'text', None)
        if not text:
            attach_texts = []
            for attach in getattr(msg, 'attaches', []) or []:
                attach_type = str(getattr(attach, 'type', '')).upper()
                if attach_type == 'CONTROL':
                    event = getattr(attach, 'event', None)
                    attach_texts.append(f"[Системное событие: {event or 'control'}]")
                elif attach_type == 'PHOTO':
                    attach_texts.append("[Фото]")
                elif attach_type == 'VIDEO':
                    attach_texts.append("[Видео]")
                elif attach_type == 'FILE':
                    attach_texts.append("[Файл]")
                elif attach_type == 'STICKER':
                    attach_texts.append("[Стикер]")
                elif attach_type == 'AUDIO':
                    attach_texts.append("[Голосовое сообщение]")
                elif attach_type == 'CONTACT':
                    attach_texts.append("[Контакт]")

            if attach_texts:
                text = " ".join(attach_texts)
            else:
                msg_type = str(getattr(msg, 'type', '') or '').upper()
                if msg_type in {'SYSTEM', 'SERVICE'}:
                    text = f"[{msg_type}]"
                else:
                    return None

        msg_date = self._extract_message_datetime(msg)

        sender_id = getattr(msg, 'sender_id', None)
        if sender_id in (None, 0):
            sender_id = getattr(msg, 'from_id', None)
        if sender_id in (None, 0):
            sender_id = getattr(msg, 'sender', 0) or 0

        sender_name = str(sender_id) if sender_id else 'Unknown'
        sender = getattr(msg, 'sender', None)
        if sender and not isinstance(sender, (int, float)):
            sender_name = str(sender)

        return {
            'message_id': getattr(msg, 'id', 0),
            'sender_id': sender_id if isinstance(sender_id, int) else 0,
            'sender_name': sender_name,
            'text': text,
            'date': (msg_date or datetime.now()).isoformat() + 'Z',
            'reply_to': None,
            'topic_id': None,
            '_sort_ts': msg_date.timestamp() if msg_date else 0,
        }

    def _get_dialog_title(self, dialog) -> str:
        dialog_id = int(getattr(dialog, 'id', 0))
        dialog_type = str(getattr(dialog, 'type', 'dialog'))
        if dialog_type == 'dialog':
            return f"Личный диалог {dialog_id}"
        return f"Диалог {dialog_id}"

    async def _fetch_history_with_retry(
            self,
            account_id: int,
            chat_id: int,
            from_time: Optional[int] = None,
            backward: int = 200,
    ):
        """Fetch Max history page and raise on failure instead of masking it as empty data."""
        client = self._clients.get(account_id)
        if not client:
            raise RuntimeError(f"No Max client for account {account_id}")

        socket_errors = ("SocketNotConnectedError", "SocketSendError", "SocketError")

        for attempt in range(3):
            try:
                # On retries use smaller page size to avoid large responses breaking the socket
                fetch_size = backward if attempt == 0 else min(backward, 50)
                return await client.fetch_history(chat_id=chat_id, from_time=from_time, backward=fetch_size)
            except Exception as e:
                err_name = e.__class__.__name__
                if err_name not in socket_errors or attempt == 2:
                    raise
                print(
                    f"[MaxClient] Socket error ({err_name}) attempt {attempt + 1}/3 "
                    f"for account {account_id} chat {chat_id}, reconnecting...",
                    flush=True
                )
                self._mark_disconnected(account_id)
                if not await self._ensure_runtime_connection(account_id, force_restart=True):
                    raise RuntimeError(f"Max account {account_id} failed to reconnect")
                client = self._clients.get(account_id)
                if not client:
                    raise RuntimeError(f"No Max client for account {account_id} after reconnect")
                await asyncio.sleep(2.0)

    async def start_auth(self, account_id: int, phone: str) -> dict:
        """Start Max authentication by launching client in background"""
        async with self._get_lock(account_id):
            self._phones[account_id] = phone

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
                    send_fake_telemetry=False,
                    reconnect=False,
                )

                self._clients[account_id] = client
                self._ready[account_id] = asyncio.Event()

                # Register on_start handler to know when connected
                @client.on_start
                async def on_start():
                    print(f"[MaxClient] Account {account_id} connected and synced!", flush=True)
                    # Small delay to let pymax populate chats/dialogs after sync
                    await asyncio.sleep(1.0)
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
                    finally:
                        # start() exited fully — mark client unavailable until it is started again.
                        self._mark_disconnected(account_id)
                        print(f"[MaxClient] Client {account_id} stopped", flush=True)

                self._tasks[account_id] = asyncio.create_task(run_client())

                # Wait a bit to see if it connects quickly (cached session)
                try:
                    await asyncio.wait_for(self._ready[account_id].wait(), timeout=10.0)
                    return {'status': 'success', 'message': 'Max аккаунт подключен (сессия из кеша)'}
                except asyncio.TimeoutError:
                    # Not connected yet - needs auth
                    return {
                        'status': 'auth_started',
                        'message': 'Авторизация запущена. Код должен прийти по SMS или в приложение Max. Проверьте терминал сервера.'
                    }

            except ImportError as ie:
                raise RuntimeError(
                    f"maxapi-python не установлен ({ie}). Запустите: pip install -U maxapi-python"
                )
            except Exception as e:
                import traceback
                traceback.print_exc()
                raise RuntimeError(f"Ошибка авторизации Max: {e}")

    async def ensure_connected(self, account_id: int, phone: str) -> bool:
        """Ensure client is connected. Auto-start if needed. Returns True if connected."""
        self._phones[account_id] = phone

        # Check if task is still alive AND ready
        task_alive = account_id in self._tasks and not self._tasks[account_id].done()
        is_ready = account_id in self._ready and self._ready[account_id].is_set()
        socket_connected = self._is_socket_connected(account_id)

        if task_alive and is_ready and socket_connected:
            return True

        # Client exists but socket has dropped - force a reconnect
        if task_alive and is_ready and not socket_connected:
            print(f"[MaxClient] Client {account_id} lost socket connection, restarting", flush=True)
            self._mark_disconnected(account_id)

        # Client exists but not ready yet - wait a bit
        if task_alive and not is_ready:
            try:
                if account_id in self._ready:
                    await asyncio.wait_for(self._ready[account_id].wait(), timeout=15.0)
                    return self._is_socket_connected(account_id)
            except asyncio.TimeoutError:
                return False

        # Task dead or no client — reconnect
        print(f"[MaxClient] Auto-starting client for account {account_id}", flush=True)
        result = await self.start_auth(account_id, phone)
        return result.get('status') == 'success'

    async def check_connected(self, account_id: int) -> bool:
        """Check if client is actually connected (task alive + ready)"""
        task_alive = account_id in self._tasks and not self._tasks[account_id].done()
        is_ready = account_id in self._ready and self._ready[account_id].is_set()
        return task_alive and is_ready and self._is_socket_connected(account_id)

    async def get_dialogs(self, account_id: int, include_private: bool = False) -> list[dict]:
        """Get group chats from Max account.
        pymax stores group chats in client.chats (Chat objects with .id, .title)
        and private dialogs in client.dialogs (Dialog objects with .id, no .title).
        By default only returns group chats.
        """
        client = self._clients.get(account_id)
        if not client:
            print(f"[MaxClient] No client for account {account_id}", flush=True)
            return []

        if not await self._ensure_runtime_connection(account_id):
            print(f"[MaxClient] Client {account_id} is not connected", flush=True)
            return []

        client = self._clients.get(account_id)
        if not client:
            return []

        result = []
        seen_ids = set()
        try:
            is_connected = self._is_socket_connected(account_id)
            print(f"[MaxClient] Client connected: {is_connected}", flush=True)

            # Force refresh from Max API so newly joined chats appear in sync.
            fetched_chats = await client.fetch_chats()
            print(f"[MaxClient] Refreshed {len(fetched_chats)} chats from Max API", flush=True)

            # Group chats - have .id and .title (deduplicate by id)
            chats = getattr(client, 'chats', []) or []
            print(f"[MaxClient] Found {len(chats)} group chat entries (may include duplicates)", flush=True)
            for chat in chats:
                chat_id = int(getattr(chat, 'id', 0))
                if chat_id in seen_ids:
                    continue
                seen_ids.add(chat_id)
                title = getattr(chat, 'title', None) or f"Chat {chat_id}"
                chat_type = getattr(chat, 'type', 'group')
                result.append({
                    'chat_id': chat_id,
                    'title': str(title),
                    'type': str(chat_type),
                })

            # Private dialogs - only if requested (deduplicate by id)
            if include_private:
                dialogs = getattr(client, 'dialogs', []) or []
                print(f"[MaxClient] Found {len(dialogs)} private dialog entries", flush=True)
                for dialog in dialogs:
                    dialog_id = int(getattr(dialog, 'id', 0))
                    if dialog_id in seen_ids:
                        continue
                    seen_ids.add(dialog_id)
                    title = f"Диалог {dialog_id}"
                    result.append({
                        'chat_id': dialog_id,
                        'title': title,
                        'type': 'dialog',
                    })
            else:
                dialogs = getattr(client, 'dialogs', []) or []
                print(f"[MaxClient] Skipping {len(dialogs)} private dialogs", flush=True)

            print(f"[MaxClient] Total unique: {len(result)} chats", flush=True)

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
            raise RuntimeError(f"No Max client for account {account_id}")

        if not await self._ensure_runtime_connection(account_id):
            raise RuntimeError(f"Max client {account_id} is not connected")

        client = self._clients.get(account_id)
        if not client:
            raise RuntimeError(f"No Max client for account {account_id}")

        start_naive = start_date.replace(tzinfo=None) if start_date.tzinfo else start_date
        end_naive = end_date.replace(tzinfo=None) if end_date.tzinfo else end_date

        print(f"[MaxClient] Fetching messages chat={chat_id}, period={start_naive} - {end_naive}", flush=True)

        messages = []
        seen_message_ids = set()
        page_size = min(max(limit, 200), 1000)
        from_time = int(end_naive.timestamp() * 1000)
        pages = 0

        try:
            while len(messages) < limit and pages < 50:
                pages += 1
                history = await self._fetch_history_with_retry(
                    account_id=account_id,
                    chat_id=chat_id,
                    from_time=from_time,
                    backward=page_size,
                )
                if not history:
                    break

                oldest_seen_ms = None
                added_this_page = 0

                for msg in history:
                    msg_id = getattr(msg, 'id', 0)
                    if msg_id in seen_message_ids:
                        continue
                    seen_message_ids.add(msg_id)

                    msg_date = self._extract_message_datetime(msg)
                    if msg_date:
                        msg_ts_ms = int(msg_date.timestamp() * 1000)
                        if oldest_seen_ms is None or msg_ts_ms < oldest_seen_ms:
                            oldest_seen_ms = msg_ts_ms
                        if msg_date < start_naive:
                            continue
                        if msg_date > end_naive:
                            continue

                    normalized = self._normalize_message(msg)
                    if not normalized:
                        continue

                    normalized.pop('_sort_ts', None)
                    messages.append(normalized)
                    added_this_page += 1

                    if len(messages) >= limit:
                        break

                if oldest_seen_ms is None:
                    break

                oldest_seen_date = datetime.fromtimestamp(oldest_seen_ms / 1000)
                if oldest_seen_date <= start_naive:
                    break

                if added_this_page == 0 and len(history) < page_size:
                    break

                next_from_time = oldest_seen_ms - 1
                if next_from_time >= from_time:
                    break
                from_time = next_from_time

            print(f"[MaxClient] Found {len(messages)} messages", flush=True)

        except Exception as e:
            print(f"[MaxClient] Error fetching messages: {e}", flush=True)
            import traceback
            traceback.print_exc()
            raise RuntimeError(f"Failed to fetch Max messages for chat {chat_id}: {e}") from e

        messages.sort(key=lambda item: item['date'])
        return messages[:limit]

    async def get_service_messages(self, account_id: int, limit: int = 20) -> list[dict]:
        """Get recent messages from Max private dialogs for code lookup."""
        client = self._clients.get(account_id)
        if not client:
            print(f"[MaxClient] No client for account {account_id}", flush=True)
            return []

        if not await self._ensure_runtime_connection(account_id):
            print(f"[MaxClient] Client {account_id} not connected", flush=True)
            return []

        client = self._clients.get(account_id)
        if not client:
            return []

        dialogs = getattr(client, 'dialogs', []) or []
        if not dialogs:
            return []

        results = []
        seen_ids = set()
        history_depth = max(30, min(limit * 3, 100))

        for dialog in dialogs:
            dialog_id = int(getattr(dialog, 'id', 0))
            if not dialog_id or dialog_id in seen_ids:
                continue
            seen_ids.add(dialog_id)

            try:
                history = await client.fetch_history(chat_id=dialog_id, backward=history_depth)
            except Exception as e:
                print(f"[MaxClient] Error fetching private dialog {dialog_id}: {e}", flush=True)
                continue

            dialog_title = self._get_dialog_title(dialog)
            for msg in history or []:
                normalized = self._normalize_message(msg)
                if not normalized:
                    continue
                normalized['chat_id'] = dialog_id
                normalized['chat_title'] = dialog_title
                results.append(normalized)

        results.sort(key=lambda item: item.get('_sort_ts', 0), reverse=True)
        trimmed = results[:limit]
        for item in trimmed:
            item.pop('_sort_ts', None)
        return trimmed

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

            if account_id in self._phones:
                del self._phones[account_id]

    async def close_all(self):
        """Close all Max client connections"""
        for account_id in list(self._clients.keys()):
            await self.disconnect_account(account_id)

    async def _ensure_runtime_connection(self, account_id: int, force_restart: bool = False) -> bool:
        # Save phone BEFORE disconnect (disconnect_account deletes it)
        phone = self._phones.get(account_id)

        if force_restart and account_id in self._tasks:
            await self.disconnect_account(account_id)
            # Restore phone after disconnect wiped it
            if phone:
                self._phones[account_id] = phone

        if self._is_socket_connected(account_id):
            return True

        if not phone:
            print(f"[MaxClient] Missing phone for account {account_id}, cannot reconnect", flush=True)
            return False

        return await self.ensure_connected(account_id, phone)


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
