# -*- coding: utf-8 -*-
import asyncio
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

MAX_SESSIONS_DIR = Path(__file__).parent.parent / "sessions" / "max"
MAX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_MAX_APP_VERSION = "26.25.0"
DEFAULT_MAX_BUILD_NUMBER = 6790


class MaxClientManager:
    """Manager for Max messenger client sessions using maxapi-python (pymax)"""

    def __init__(self):
        self._clients: dict[int, object] = {}  # account_id -> client
        self._tasks: dict[int, asyncio.Task] = {}  # account_id -> background task
        self._locks: dict[int, asyncio.Lock] = {}
        self._ready: dict[int, asyncio.Event] = {}  # signals when client is connected
        self._phones: dict[int, str] = {}
        self._auth_tokens: dict[int, str] = {}
        self._password_challenges: dict[int, dict] = {}
        self._patch_pymax_socket_unpacker()
        self._patch_pymax_contact_attach_parser()

    def _patch_pymax_socket_unpacker(self) -> None:
        """Make pymax tolerate larger compressed sync packets from Max."""
        try:
            import lz4.block
            import msgpack
            from pymax.mixins.socket import SocketMixin
        except ImportError:
            return

        if getattr(SocketMixin, "_messagesummariser_lz4_patch", False):
            return

        def _unpack_packet(client, data: bytes):
            ver = int.from_bytes(data[0:1], "big")
            cmd = int.from_bytes(data[1:3], "big")
            seq = int.from_bytes(data[3:4], "big")
            opcode = int.from_bytes(data[4:6], "big")
            packed_len = int.from_bytes(data[6:10], "big", signed=False)
            comp_flag = packed_len >> 24
            payload_length = packed_len & 0xFFFFFF
            payload_bytes = data[10: 10 + payload_length]

            payload = None
            if payload_bytes:
                if comp_flag != 0:
                    compressed_data = bytes(payload_bytes)
                    decompressed = None
                    for size in (99_999, 250_000, 500_000, 1_000_000, 2_000_000, 5_000_000):
                        try:
                            decompressed = lz4.block.decompress(compressed_data, uncompressed_size=size)
                            break
                        except lz4.block.LZ4BlockError:
                            continue

                    if decompressed is None:
                        try:
                            decompressed = lz4.block.decompress(compressed_data)
                        except Exception:
                            return None

                    payload_bytes = decompressed

                payload = msgpack.unpackb(payload_bytes, raw=False, strict_map_key=False)

            return {
                "ver": ver,
                "cmd": cmd,
                "seq": seq,
                "opcode": opcode,
                "payload": payload,
            }

        SocketMixin._unpack_packet = _unpack_packet
        SocketMixin._messagesummariser_lz4_patch = True

    def _patch_pymax_contact_attach_parser(self) -> None:
        """Allow Max CONTACT attachments with missing optional fields."""
        try:
            from pymax.types import ContactAttach
        except ImportError:
            return

        if getattr(ContactAttach, "_messagesummariser_optional_fields_patch", False):
            return

        def from_dict(cls, data):
            first_name = data.get("firstName") or ""
            last_name = data.get("lastName") or ""
            name = data.get("name") or " ".join(filter(None, [first_name, last_name])) or str(data.get("contactId", ""))
            return cls(
                contact_id=data.get("contactId", 0),
                first_name=first_name,
                last_name=last_name,
                name=name,
                photo_url=data.get("photoUrl") or "",
            )

        ContactAttach.from_dict = classmethod(from_dict)
        ContactAttach._messagesummariser_optional_fields_patch = True

    def _get_lock(self, account_id: int) -> asyncio.Lock:
        if account_id not in self._locks:
            self._locks[account_id] = asyncio.Lock()
        return self._locks[account_id]

    def _get_work_dir(self, account_id: int) -> str:
        path = MAX_SESSIONS_DIR / f"account_{account_id}"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    @staticmethod
    def _build_user_agent():
        """Build a currently supported Max fingerprint for pymax 1.x."""
        from pymax.payloads import UserAgentPayload

        app_version = os.getenv("MAX_APP_VERSION", DEFAULT_MAX_APP_VERSION).strip()
        raw_build_number = os.getenv("MAX_BUILD_NUMBER", str(DEFAULT_MAX_BUILD_NUMBER)).strip()
        try:
            build_number = int(raw_build_number)
        except ValueError:
            print(
                f"[MaxClient] Invalid MAX_BUILD_NUMBER={raw_build_number!r}; "
                f"using {DEFAULT_MAX_BUILD_NUMBER}",
                flush=True,
            )
            build_number = DEFAULT_MAX_BUILD_NUMBER

        return UserAgentPayload(
            device_type="DESKTOP",
            app_version=app_version or DEFAULT_MAX_APP_VERSION,
            build_number=build_number,
        )

    def _is_socket_connected(self, account_id: int) -> bool:
        client = self._clients.get(account_id)
        return bool(client and getattr(client, "is_connected", False))

    def _mark_disconnected(self, account_id: int) -> None:
        if account_id in self._ready:
            self._ready[account_id].clear()

    def _is_transient_socket_error(self, error: Exception) -> bool:
        err_name = error.__class__.__name__
        err_text = str(error).lower()
        transient_names = {
            "SocketNotConnectedError",
            "SocketSendError",
            "SocketError",
            "TimeoutError",
            "ConnectionError",
            "OSError",
        }
        return (
            err_name in transient_names
            or "not connected" in err_text
            or "connection" in err_text
            or "socket" in err_text
            or "timed out" in err_text
        )

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

    @staticmethod
    def _get_max_user_display_name(user) -> Optional[str]:
        """Extract the most useful display name from a pymax User/Contact."""
        names = getattr(user, 'names', None) or []
        ordered_names = sorted(
            names,
            key=lambda item: str(getattr(item, 'type', '') or '').upper() != 'ONEME',
        )
        for item in ordered_names:
            display_name = str(getattr(item, 'name', '') or '').strip()
            if display_name:
                return display_name
            full_name = ' '.join(filter(None, [
                str(getattr(item, 'first_name', '') or '').strip(),
                str(getattr(item, 'last_name', '') or '').strip(),
            ])).strip()
            if full_name:
                return full_name
        return None

    @staticmethod
    def _get_max_user_username(user) -> Optional[str]:
        """Extract a username from the fields exposed by different pymax versions."""
        candidates = [getattr(user, 'username', None), getattr(user, 'link', None)]
        for item in getattr(user, 'names', None) or []:
            name_type = str(getattr(item, 'type', '') or '').upper()
            if name_type in {'USERNAME', 'NICKNAME', 'LINK'}:
                candidates.append(getattr(item, 'name', None))

        for raw_value in candidates:
            value = str(raw_value or '').strip()
            if not value:
                continue
            value = value.removeprefix('@')
            if 'max.ru/' in value:
                value = value.split('max.ru/', 1)[1].split('?', 1)[0].strip('/')
            if re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', value) and not value.isdigit():
                return value
        return None

    @classmethod
    def _max_user_to_profile(cls, user, user_id: int) -> dict:
        username = cls._get_max_user_username(user)
        thumbnail_url = getattr(user, 'base_url', None) or getattr(user, 'photo_url', None)
        full_url = (
            getattr(user, 'base_raw_url', None)
            or getattr(user, 'full_avatar_url', None)
            or thumbnail_url
        )
        photos = []
        if thumbnail_url or full_url:
            photos.append({
                'thumbnail_url': str(thumbnail_url or full_url),
                'full_url': str(full_url or thumbnail_url),
                'label': 'Текущая фотография профиля',
            })
        return {
            'user_id': int(getattr(user, 'id', None) or user_id),
            'display_name': cls._get_max_user_display_name(user) or f'Пользователь MAX {user_id}',
            'username': username,
            'avatar_url': str(thumbnail_url or full_url) if (thumbnail_url or full_url) else None,
            'photos': photos,
            'profile_link': f'max://user/{user_id}',
            'public_link': f'https://max.ru/{username}' if username else None,
        }

    async def get_user_profile(self, account_id: int, user_id: int) -> Optional[dict]:
        """Resolve one Max user through an already connected account."""
        client = self._clients.get(account_id)
        if not client or not self._is_socket_connected(account_id):
            return None
        user = await client.get_user(user_id)
        if not user:
            return None
        return self._max_user_to_profile(user, user_id)

    async def _resolve_message_sender_names(self, client, messages: list[dict]) -> None:
        """Replace numeric Max sender labels with names fetched from user profiles."""
        unresolved_ids = sorted({
            int(message['sender_id'])
            for message in messages
            if isinstance(message.get('sender_id'), int)
            and message['sender_id'] > 0
            and message.get('sender_name') in {
                str(message['sender_id']),
                'Unknown',
            }
        })
        if not unresolved_ids:
            return

        resolved: dict[int, str] = {}
        for offset in range(0, len(unresolved_ids), 100):
            chunk = unresolved_ids[offset:offset + 100]
            try:
                users = await client.get_users(chunk)
            except Exception as e:
                print(
                    f"[MaxClient] Could not resolve {len(chunk)} sender profiles: {e}",
                    flush=True,
                )
                continue
            for user in users or []:
                user_id = getattr(user, 'id', None)
                display_name = self._get_max_user_display_name(user)
                if isinstance(user_id, int) and display_name:
                    resolved[user_id] = display_name

        for message in messages:
            display_name = resolved.get(message.get('sender_id'))
            if display_name:
                message['sender_name'] = display_name

        print(
            f"[MaxClient] Resolved sender names: {len(resolved)}/{len(unresolved_ids)}",
            flush=True,
        )

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
                if (
                    err_name not in socket_errors
                    and not self._is_transient_socket_error(e)
                ) or attempt == 2:
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

    async def start_auth(self, account_id: int, phone: str, force_code: bool = False) -> dict:
        """Start Max authentication by launching client in background"""
        async with self._get_lock(account_id):
            # A code-request client has no background task, so checking only
            # _tasks leaks its socket and SQLite engine on every repeated click.
            if account_id in self._tasks or account_id in self._clients:
                await self._disconnect_account_unlocked(account_id)
            self._phones[account_id] = phone

            if force_code:
                self._auth_tokens.pop(account_id, None)
                self._password_challenges.pop(account_id, None)
                session_db = Path(self._get_work_dir(account_id)) / "session.db"
                if session_db.exists():
                    session_db.unlink()

            try:
                from pymax import SocketMaxClient
                ua = self._build_user_agent()
                client = SocketMaxClient(
                    phone=phone,
                    work_dir=self._get_work_dir(account_id),
                    headers=ua,
                    send_fake_telemetry=False,
                    reconnect=False,
                )

                self._clients[account_id] = client
                self._ready[account_id] = asyncio.Event()

                # No cached session yet: use a UI-driven SMS code flow instead of
                # pymax's stdin prompt.
                if getattr(client, "_token", None) is None:
                    print(f"[MaxClient] Requesting SMS code for account {account_id}", flush=True)
                    await client.connect(client.user_agent)
                    temp_token = await client.request_code(phone)
                    self._auth_tokens[account_id] = temp_token
                    return {
                        'status': 'code_required',
                        'message': 'Код отправлен по SMS или в приложение Max. Введите его в форме.'
                    }

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

    async def complete_auth_code(
            self,
            account_id: int,
            code: str,
            password: Optional[str] = None,
    ) -> dict:
        """Complete Max SMS-code auth and start the saved session."""
        code = (code or "").strip()
        if len(code) != 6 or not code.isdigit():
            raise RuntimeError("Код Max должен состоять из 6 цифр")

        async with self._get_lock(account_id):
            phone = self._phones.get(account_id)
            client = self._clients.get(account_id)
            temp_token = self._auth_tokens.get(account_id)
            if not client or not temp_token or not phone:
                raise RuntimeError("Сначала нажмите 'Авторизовать', чтобы запросить код Max")

            try:
                challenge = self._password_challenges.get(account_id)
                if challenge:
                    if not password:
                        return {
                            "status": "password_required",
                            "message": "Введите пароль двухфакторной аутентификации Max.",
                            "password_hint": challenge.get("hint"),
                        }
                    token_attrs = await asyncio.wait_for(
                        client._check_password(password, challenge["trackId"]),
                        timeout=30.0,
                    )
                    token = (token_attrs or {}).get("LOGIN", {}).get("token")
                    if not token:
                        raise RuntimeError("Неверный пароль двухфакторной аутентификации Max")
                else:
                    response = await asyncio.wait_for(
                        client._send_code(code, temp_token),
                        timeout=30.0,
                    )
                    login_attrs = response.get("tokenAttrs", {}).get("LOGIN", {})
                    challenge = response.get("passwordChallenge")
                    token = login_attrs.get("token")
                    if challenge and not token:
                        self._password_challenges[account_id] = challenge
                        hint = challenge.get("hint")
                        message = "Код принят. Введите пароль двухфакторной аутентификации Max."
                        if hint:
                            message += f" Подсказка: {hint}"
                        return {
                            "status": "password_required",
                            "message": message,
                            "password_hint": hint,
                        }
                    if not token:
                        raise RuntimeError("Max не вернул токен авторизации")

                client._token = token
                client._database.update_auth_token(client._device_id, token)
                self._auth_tokens.pop(account_id, None)
                self._password_challenges.pop(account_id, None)
            except Exception as e:
                raise RuntimeError(f"Не удалось подтвердить код Max: {e}") from e

        await self.disconnect_account(account_id)
        self._phones[account_id] = phone
        return {"status": "success", "message": "Max аккаунт авторизован, сессия сохранена."}

    async def resend_auth_code(self, account_id: int) -> dict:
        """Ask Max to send another auth code for the current UI auth flow."""
        async with self._get_lock(account_id):
            phone = self._phones.get(account_id)
            client = self._clients.get(account_id)
            if not client or not phone:
                raise RuntimeError("Сначала нажмите 'Авторизовать', чтобы запросить код Max")

            try:
                temp_token = await asyncio.wait_for(client.resend_code(phone), timeout=20.0)
                self._auth_tokens[account_id] = temp_token
            except Exception as e:
                raise RuntimeError(f"Не удалось повторно отправить код Max: {e}") from e

            return {
                "status": "code_required",
                "message": "Новый код Max запрошен. Проверьте SMS и приложение Max."
            }

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
            return await self._ensure_runtime_connection(account_id, force_restart=True)

        # Client exists but not ready yet - wait a bit
        if task_alive and not is_ready:
            try:
                if account_id in self._ready:
                    await asyncio.wait_for(self._ready[account_id].wait(), timeout=20.0)
                    if self._is_socket_connected(account_id):
                        return True
            except asyncio.TimeoutError:
                pass

            print(f"[MaxClient] Client {account_id} did not become ready, restarting", flush=True)
            return await self._ensure_runtime_connection(account_id, force_restart=True)

        # Task dead or no client — reconnect
        print(f"[MaxClient] Auto-starting client for account {account_id}", flush=True)
        result = await self.start_auth(account_id, phone)
        if result.get('status') == 'success':
            return True
        if result.get('status') == 'auth_started' and account_id in self._ready:
            try:
                await asyncio.wait_for(self._ready[account_id].wait(), timeout=20.0)
                return self._is_socket_connected(account_id)
            except asyncio.TimeoutError:
                print(f"[MaxClient] Client {account_id} auth start timed out, restarting once", flush=True)
                result = await self.start_auth(account_id, phone)
                if result.get('status') == 'success':
                    return True
                if result.get('status') == 'auth_started' and account_id in self._ready:
                    try:
                        await asyncio.wait_for(self._ready[account_id].wait(), timeout=30.0)
                        return self._is_socket_connected(account_id)
                    except asyncio.TimeoutError:
                        return False
        return False

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
            phone: Optional[str] = None,
    ) -> list[dict]:
        """Get messages from a Max chat within a date range"""
        if phone:
            self._phones[account_id] = phone

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

        await self._resolve_message_sender_names(client, messages)
        messages.sort(key=lambda item: item['date'])
        return messages[:limit]

    def _split_outgoing_text(self, text: str, max_chars: int = 3500) -> list[str]:
        """Split long report text into Max-friendly chunks without reading chat history."""
        clean = (text or "").strip()
        if not clean:
            return []
        chunks = []
        current = ""
        for paragraph in clean.split("\n\n"):
            part = paragraph.strip()
            if not part:
                continue
            candidate = f"{current}\n\n{part}".strip() if current else part
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            while len(part) > max_chars:
                chunks.append(part[:max_chars].rstrip())
                part = part[max_chars:].lstrip()
            current = part
        if current:
            chunks.append(current)
        return chunks

    async def send_text(
            self,
            account_id: int,
            chat_id: int,
            text: str,
            notify: bool = True,
    ) -> list[int]:
        """Send text to a Max chat. This does not fetch dialogs or message history."""
        if not text.strip():
            raise RuntimeError("Message text is empty")

        client = self._clients.get(account_id)
        if not client:
            raise RuntimeError(f"No Max client for account {account_id}")

        if not await self._ensure_runtime_connection(account_id):
            raise RuntimeError(f"Max client {account_id} is not connected")

        client = self._clients.get(account_id)
        if not client:
            raise RuntimeError(f"No Max client for account {account_id}")

        message_ids = []
        for chunk in self._split_outgoing_text(text):
            last_error = None
            for attempt in range(3):
                if attempt > 0:
                    await asyncio.sleep(1.5)
                    if not await self._ensure_runtime_connection(account_id, force_restart=True):
                        last_error = RuntimeError(f"Max client {account_id} failed to reconnect")
                        continue
                    client = self._clients.get(account_id)
                    if not client:
                        last_error = RuntimeError(f"No Max client for account {account_id} after reconnect")
                        continue

                try:
                    msg = await client.send_message(text=chunk, chat_id=int(chat_id), notify=notify)
                    msg_id = getattr(msg, "id", None) if msg else None
                    if msg_id is not None:
                        message_ids.append(int(msg_id))
                    break
                except Exception as e:
                    last_error = e
                    if not self._is_transient_socket_error(e) or attempt == 2:
                        raise RuntimeError(f"Max send failed: {e}") from e
                    print(
                        f"[MaxClient] Send failed for account {account_id}, "
                        f"attempt {attempt + 1}/3: {e}. Reconnecting...",
                        flush=True,
                    )
                    self._mark_disconnected(account_id)
            else:
                raise RuntimeError(f"Max send failed: {last_error}")
            await asyncio.sleep(0.4)
        return message_ids

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

    async def _disconnect_account_unlocked(self, account_id: int) -> None:
        """Disconnect one client. Caller must serialize access when needed."""
        task = self._tasks.pop(account_id, None)
        if task:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        client = self._clients.pop(account_id, None)
        if client:
            try:
                if hasattr(client, 'close'):
                    await asyncio.wait_for(client.close(), timeout=3.0)
                elif hasattr(client, 'disconnect'):
                    await asyncio.wait_for(client.disconnect(), timeout=3.0)
                if hasattr(client, '_cleanup_client'):
                    await asyncio.wait_for(client._cleanup_client(), timeout=5.0)
            except Exception:
                pass

            database = getattr(client, '_database', None)
            engine = getattr(database, 'engine', None)
            if engine:
                try:
                    engine.dispose()
                except Exception:
                    pass

        self._ready.pop(account_id, None)
        self._phones.pop(account_id, None)
        self._auth_tokens.pop(account_id, None)
        self._password_challenges.pop(account_id, None)

    async def disconnect_account(self, account_id: int):
        """Disconnect Max client for account."""
        async with self._get_lock(account_id):
            await self._disconnect_account_unlocked(account_id)

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
