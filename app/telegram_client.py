import os
import asyncio
import traceback
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, AsyncGenerator
from zoneinfo import ZoneInfo
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import User, Chat, Channel, Message, ForumTopic
from telethon.tl.functions.channels import GetForumTopicsRequest
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    SendCodeUnavailableError,
)

from .proxy_manager import get_proxy_manager, ProxyConfig

SESSIONS_DIR = Path(__file__).parent.parent / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

REPORT_TIMEZONE = ZoneInfo(os.getenv("REPORT_TIMEZONE", "Europe/Moscow"))
UTC = timezone.utc
LIVE_TAIL_AGE = timedelta(minutes=30)
LIVE_TAIL_WINDOW = timedelta(minutes=20)
LIVE_TAIL_LIMIT = 1000

# Connection errors that should trigger proxy switch
CONNECTION_ERRORS = (
    ConnectionError,
    TimeoutError,
    OSError,
    asyncio.TimeoutError,
)


class TelegramClientManager:
    """Manager for multiple Telegram client sessions"""

    def __init__(self, api_id: int, api_hash: str, use_proxy: bool = True):
        self.api_id = api_id
        self.api_hash = api_hash
        self.use_proxy = use_proxy
        self._clients: dict[int, TelegramClient] = {}
        self._pending_auth: dict[int, dict] = {}  # account_id -> {client, phone_code_hash}
        self._locks: dict[int, asyncio.Lock] = {}  # Lock per account to prevent concurrent access
        self._operation_locks: dict[int, asyncio.Lock] = {}
        self._proxy_initialized = False

    def _get_session_path(self, account_id: int) -> Path:
        return SESSIONS_DIR / f"account_{account_id}"

    def _get_lock(self, account_id: int) -> asyncio.Lock:
        """Get or create a lock for the given account"""
        if account_id not in self._locks:
            self._locks[account_id] = asyncio.Lock()
        return self._locks[account_id]

    def _get_operation_lock(self, account_id: int) -> asyncio.Lock:
        """Serialize high-level Telegram requests per session/account."""
        if account_id not in self._operation_locks:
            self._operation_locks[account_id] = asyncio.Lock()
        return self._operation_locks[account_id]

    @staticmethod
    def _auth_delivery_payload(result) -> dict:
        """Expose Telegram's actual code delivery route to the UI."""
        def normalize(value) -> str | None:
            if value is None:
                return None
            name = type(value).__name__
            prefix = "SentCodeType"
            if name.startswith(prefix):
                name = name[len(prefix):]
            aliases = {
                "App": "app",
                "Sms": "sms",
                "Call": "call",
                "FlashCall": "flash_call",
                "MissedCall": "missed_call",
                "FragmentSms": "fragment_sms",
                "EmailCode": "email_code",
                "SetUpEmailRequired": "email_setup_required",
                "FirebaseSms": "firebase_sms",
            }
            return aliases.get(name, name.lower())

        delivery_type = normalize(getattr(result, "type", None)) or "unknown"
        next_type = normalize(getattr(result, "next_type", None))
        try:
            timeout = max(0, int(getattr(result, "timeout", 0) or 0))
        except (TypeError, ValueError):
            timeout = 0
        return {
            "delivery_type": delivery_type,
            "next_delivery_type": next_type,
            "resend_available_in": timeout,
        }

    async def _resend_auth_unlocked(self, account_id: int) -> dict:
        auth_data = self._pending_auth.get(account_id)
        if not auth_data:
            return {
                "status": "error",
                "message": "Сначала запросите код авторизации заново",
            }

        client = auth_data["client"]
        if not client.is_connected():
            await asyncio.wait_for(client.connect(), timeout=30)

        # Telethon retains the first phone_code_hash on this exact client.
        # Calling send_code_request again therefore issues ResendCodeRequest
        # instead of repeating a fresh SendCodeRequest with the same hash.
        try:
            result = await client.send_code_request(auth_data["phone"])
        except SendCodeUnavailableError:
            return {
                "status": "error",
                "error_code": "delivery_unavailable",
                "message": (
                    "Telegram пока не разрешает повторную отправку: все доступные "
                    "способы доставки для этого номера уже использованы. Не запрашивайте "
                    "код несколько раз подряд; попробуйте снова позже."
                ),
            }
        auth_data["phone_code_hash"] = result.phone_code_hash
        delivery = self._auth_delivery_payload(result)
        auth_data["delivery"] = delivery
        print(
            f"[Auth] Code resent for account {account_id}: "
            f"delivery={delivery['delivery_type']}, "
            f"next={delivery['next_delivery_type']}, "
            f"timeout={delivery['resend_available_in']}",
            flush=True,
        )
        return {
            "status": "code_required",
            "phone_code_hash": result.phone_code_hash,
            **delivery,
        }

    def _is_transient_fetch_error(self, error: Exception) -> bool:
        """Detect Telethon/network failures that require a fresh connection."""
        if isinstance(error, CONNECTION_ERRORS):
            return True

        text = str(error).lower()
        error_name = type(error).__name__.lower()
        markers = (
            "cannot send requests while disconnected",
            "closed",
            "connection",
            "disconnected",
            "broken pipe",
            "wrong session id",
            "proxy closed",
            "tcptransport",
            "transport",
            "timeout",
        )
        return any(marker in text for marker in markers) or any(marker in error_name for marker in markers)

    async def _ensure_proxy_initialized(self):
        """Initialize proxy manager and find best proxy on first use"""
        if not self.use_proxy or self._proxy_initialized:
            return

        pm = get_proxy_manager()
        proxy = await pm.get_best_proxy()
        if proxy:
            print(f"[TelegramClient] Using proxy: {proxy}", flush=True)
        else:
            raise ConnectionError("Нет рабочего Telegram прокси. Проверьте файл прокси.")
        self._proxy_initialized = True

    def _create_client(self, session_path: str, proxy: Optional[ProxyConfig] = None) -> TelegramClient:
        """Create a TelegramClient with optional proxy configuration"""
        kwargs = {
            'session': session_path,
            'api_id': self.api_id,
            'api_hash': self.api_hash,
        }

        if proxy:
            pm = get_proxy_manager()
            proxy_args = pm.get_telethon_proxy_args(proxy)
            kwargs.update(proxy_args)

        return TelegramClient(**kwargs)

    async def _safe_disconnect(self, client: Optional[TelegramClient]):
        """Best-effort disconnect that preserves task cancellation."""
        if not client:
            return
        try:
            await client.disconnect()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _reset_client_after_fetch_failure(self, account_id: int, client: Optional[TelegramClient]):
        """Drop a stale client and rotate proxy after message fetch failures."""
        if self._clients.get(account_id) is client:
            self._clients.pop(account_id, None)
        await self._safe_disconnect(client)
        await asyncio.sleep(0.3)
        if self.use_proxy:
            pm = get_proxy_manager()
            await pm.get_next_proxy()

    async def get_client(self, account_id: int, max_retries: int = 3) -> Optional[TelegramClient]:
        """Get or create a client for the given account with automatic proxy failover"""
        # Quick check without lock
        if account_id in self._clients:
            client = self._clients[account_id]
            if client.is_connected():
                return client
            self._clients.pop(account_id, None)
            await self._safe_disconnect(client)

        # Use lock to prevent concurrent session access (SQLite locking issues)
        async with self._get_lock(account_id):
            # Double-check after acquiring lock
            if account_id in self._clients:
                client = self._clients[account_id]
                if client.is_connected():
                    return client
                self._clients.pop(account_id, None)
                await self._safe_disconnect(client)

            session_path = self._get_session_path(account_id)
            if not session_path.with_suffix('.session').exists():
                return None

            # Initialize proxy on first connection attempt
            if self.use_proxy:
                await self._ensure_proxy_initialized()

            pm = get_proxy_manager() if self.use_proxy else None
            last_error = None

            for attempt in range(max_retries):
                proxy = pm.current_proxy if pm else None
                client = None

                try:
                    client = self._create_client(str(session_path), proxy)
                    await asyncio.wait_for(client.connect(), timeout=30)

                    if await client.is_user_authorized():
                        self._clients[account_id] = client
                        return client
                    return None

                except CONNECTION_ERRORS as e:
                    last_error = e
                    print(f"[TelegramClient] Connection failed (attempt {attempt + 1}/{max_retries}): {e}", flush=True)

                    await self._safe_disconnect(client)

                    # Try next proxy
                    if pm:
                        next_proxy = await pm.get_next_proxy()
                        if next_proxy:
                            print(f"[TelegramClient] Switching to proxy: {next_proxy}", flush=True)
                        else:
                            print("[TelegramClient] No more proxies available", flush=True)
                            break
                except asyncio.CancelledError:
                    print(f"[TelegramClient] Connection cancelled for account {account_id}", flush=True)
                    await self._safe_disconnect(client)
                    raise

            if last_error:
                print(f"[TelegramClient] All connection attempts failed: {last_error}", flush=True)
            return None

    async def start_auth(self, account_id: int, phone: str, max_retries: int = 3) -> dict:
        """Start authentication process for a new account with proxy support"""
        async with self._get_lock(account_id):
            session_path = self._get_session_path(account_id)
            print(f"[Auth] Starting auth for account {account_id}, phone: {phone}")

            if account_id in self._pending_auth:
                # Re-opening the login dialog must not consume Telegram's next
                # delivery attempt. Resending is an explicit separate action.
                auth_data = self._pending_auth[account_id]
                return {
                    "status": "code_required",
                    "phone_code_hash": auth_data["phone_code_hash"],
                    "request_pending": True,
                    **auth_data.get("delivery", {
                        "delivery_type": "app",
                        "next_delivery_type": None,
                        "resend_available_in": 0,
                    }),
                }

            # Initialize proxy on first connection attempt
            if self.use_proxy:
                await self._ensure_proxy_initialized()

            pm = get_proxy_manager() if self.use_proxy else None
            last_error = None

            for attempt in range(max_retries):
                proxy = pm.current_proxy if pm else None
                client = None

                try:
                    client = self._create_client(str(session_path), proxy)
                    await asyncio.wait_for(client.connect(), timeout=30)
                    print(f"[Auth] Connected to Telegram" + (f" via {proxy}" if proxy else ""))

                    result = await client.send_code_request(phone)
                    print(f"[Auth] Code sent! Type: {result.type}, phone_code_hash: {result.phone_code_hash[:10]}...")
                    delivery = self._auth_delivery_payload(result)

                    self._pending_auth[account_id] = {
                        'client': client,
                        'phone': phone,
                        'phone_code_hash': result.phone_code_hash,
                        'delivery': delivery,
                        'requested_at': time.monotonic(),
                    }
                    print(
                        f"[Auth] Delivery for account {account_id}: "
                        f"current={delivery['delivery_type']}, "
                        f"next={delivery['next_delivery_type']}, "
                        f"timeout={delivery['resend_available_in']}",
                        flush=True,
                    )
                    return {
                        'status': 'code_required',
                        'phone_code_hash': result.phone_code_hash,
                        **delivery,
                    }

                except CONNECTION_ERRORS as e:
                    last_error = e
                    print(f"[Auth] Connection failed (attempt {attempt + 1}/{max_retries}): {e}", flush=True)

                    await self._safe_disconnect(client)

                    # Even when regular message collection is configured for a
                    # direct connection, authorization may be selectively
                    # blocked by Telegram/the ISP. Fall back to a healthy proxy
                    # only for the remaining login attempts.
                    if pm is None:
                        pm = get_proxy_manager()
                        next_proxy = await pm.get_best_proxy()
                        if next_proxy:
                            print(f"[Auth] Direct connection failed; trying {next_proxy}", flush=True)
                            continue

                    # Try next proxy.
                    if pm:
                        next_proxy = await pm.get_next_proxy()
                        if next_proxy:
                            print(f"[Auth] Switching to proxy: {next_proxy}", flush=True)
                        else:
                            print("[Auth] No more proxies available", flush=True)
                            break
                except asyncio.CancelledError:
                    print(f"[Auth] Authentication cancelled for account {account_id}", flush=True)
                    await self._safe_disconnect(client)
                    raise

                except SendCodeUnavailableError:
                    await self._safe_disconnect(client)
                    return {
                        "status": "error",
                        "error_code": "delivery_unavailable",
                        "message": (
                            "Telegram временно не разрешает отправить новый код: для этого "
                            "номера уже использованы доступные попытки доставки. Подождите "
                            "и запросите код позже только один раз."
                        ),
                    }

                except Exception as e:
                    print(f"[Auth] ERROR sending code: {e}")
                    traceback.print_exc()
                    raise

            # All attempts failed
            raise ConnectionError(f"Failed to connect after {max_retries} attempts: {last_error}")

    async def resend_auth(self, account_id: int) -> dict:
        """Request Telegram's next allowed delivery route on the pending session."""
        async with self._get_lock(account_id):
            return await self._resend_auth_unlocked(account_id)

    async def restart_auth(self, account_id: int, phone: str) -> dict:
        """Discard a stale code request and make one fresh SendCodeRequest."""
        async with self._get_lock(account_id):
            auth_data = self._pending_auth.get(account_id)
            if auth_data:
                elapsed = time.monotonic() - float(auth_data.get("requested_at") or 0)
                if auth_data.get("requested_at") and elapsed < 60:
                    wait_seconds = max(1, int(60 - elapsed))
                    return {
                        "status": "error",
                        "error_code": "retry_too_soon",
                        "retry_after": wait_seconds,
                        "message": f"Подождите ещё {wait_seconds} сек. перед новым запросом.",
                    }
                self._pending_auth.pop(account_id, None)
                await self._safe_disconnect(auth_data.get("client"))

        # start_auth takes the same per-account lock, so call it only after the
        # cleanup lock has been released.
        return await self.start_auth(account_id, phone)

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
        async with self._get_operation_lock(account_id):
            return await self._get_messages_unlocked(
                account_id=account_id,
                chat_telegram_id=chat_telegram_id,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
                topic_ids=topic_ids,
                timeout_seconds=timeout_seconds,
            )

    async def _get_messages_unlocked(
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

        # Dates from the browser are local wall-clock values. Telegram returns
        # timezone-aware UTC datetimes. Converting both sides to aware UTC is
        # essential: merely stripping tzinfo shifts Moscow reports by 3 hours.
        start_utc = self._report_datetime_to_utc(start_date)
        end_utc = self._report_datetime_to_utc(end_date)

        topic_filter = set(topic_ids) if topic_ids else None
        print(
            f"[get_messages] account_id={account_id}, "
            f"chat={chat_telegram_id!r} ({type(chat_telegram_id).__name__}), "
            f"period={start_date} - {end_date}, "
            f"utc={start_utc.isoformat()} - {end_utc.isoformat()}, "
            f"topics={topic_filter}, "
            f"limit={limit}, timeout={timeout_seconds}",
            flush=True
        )

        max_fetch_attempts = 3 if self.use_proxy else 1
        per_attempt_timeout = max(5, min(timeout_seconds, max(10, timeout_seconds // max_fetch_attempts)))
        last_error = None

        for attempt in range(max_fetch_attempts):
            if attempt > 0:
                client = await self.get_client(account_id)
                if not client:
                    print(f"[get_messages] No client for account {account_id} after retry", flush=True)
                    return []

            messages_by_id: dict[int, dict] = {}

            def include_message(message) -> bool:
                msg_date = self._telegram_datetime_to_utc(message.date)

                if msg_date < start_utc or msg_date > end_utc:
                    return False

                # Skip non-text messages
                if not message.text:
                    return False

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
                        msg_topic_id = 1

                    if msg_topic_id not in topic_filter:
                        return False

                sender_name = 'Unknown'
                sender_id = message.sender_id or 0
                sender_username = None

                if message.sender:
                    if isinstance(message.sender, User):
                        sender_username = message.sender.username
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

                messages_by_id[message.id] = {
                    'message_id': message.id,
                    'sender_id': sender_id,
                    'sender_name': sender_name,
                    'sender_username': sender_username,
                    'text': message.text,
                    'date': msg_date.isoformat().replace('+00:00', 'Z'),
                    'reply_to': message.reply_to_msg_id,
                    'topic_id': msg_topic_id
                }
                return True

            async def fetch_messages():
                async for message in client.iter_messages(
                        chat_telegram_id,
                        # offset_date is exclusive, so add one second to retain
                        # messages stamped exactly at the selected boundary.
                        offset_date=end_utc + timedelta(seconds=1),
                        reverse=False,
                        limit=limit
                ):
                    msg_date = self._telegram_datetime_to_utc(message.date)

                    if msg_date < start_utc:
                        break
                    include_message(message)

                # For a live period, issue a second newest-first request without
                # offset_date. This reconciles Telegram's freshest page and
                # prevents messages arriving near the report boundary from being
                # absent in the snapshot passed to AI.
                now_utc = datetime.now(UTC)
                if -timedelta(minutes=2) <= now_utc - end_utc <= LIVE_TAIL_AGE:
                    tail_start = max(start_utc, end_utc - LIVE_TAIL_WINDOW)
                    before_tail = len(messages_by_id)
                    async for message in client.iter_messages(
                            chat_telegram_id,
                            reverse=False,
                            limit=LIVE_TAIL_LIMIT,
                    ):
                        msg_date = self._telegram_datetime_to_utc(message.date)
                        if msg_date < tail_start:
                            break
                        include_message(message)
                    recovered = len(messages_by_id) - before_tail
                    if recovered:
                        print(
                            f"[get_messages] Recovered {recovered} messages from live tail",
                            flush=True,
                        )

            try:
                await asyncio.wait_for(fetch_messages(), timeout=per_attempt_timeout)
                messages = sorted(messages_by_id.values(), key=lambda item: item['date'])
                print(f"[get_messages] Found {len(messages)} messages", flush=True)
                return messages

            except asyncio.TimeoutError as e:
                last_error = e
                print(
                    f"[get_messages] Timeout after {per_attempt_timeout}s "
                    f"(attempt {attempt + 1}/{max_fetch_attempts}), got {len(messages_by_id)} messages so far",
                    flush=True
                )
                if messages_by_id:
                    return sorted(messages_by_id.values(), key=lambda item: item['date'])
                if attempt < max_fetch_attempts - 1:
                    await self._reset_client_after_fetch_failure(account_id, client)
                    continue

            except CONNECTION_ERRORS as e:
                last_error = e
                print(
                    f"[get_messages] Connection error (attempt {attempt + 1}/{max_fetch_attempts}): {e}",
                    flush=True
                )
                if messages_by_id:
                    return sorted(messages_by_id.values(), key=lambda item: item['date'])
                if attempt < max_fetch_attempts - 1:
                    await self._reset_client_after_fetch_failure(account_id, client)
                    continue

            except Exception as e:
                if self._is_transient_fetch_error(e):
                    last_error = e
                    print(
                        f"[get_messages] Transient Telegram error "
                        f"(attempt {attempt + 1}/{max_fetch_attempts}): {e}",
                        flush=True
                    )
                    if messages_by_id:
                        return sorted(messages_by_id.values(), key=lambda item: item['date'])
                    if attempt < max_fetch_attempts - 1:
                        await self._reset_client_after_fetch_failure(account_id, client)
                        continue

                print(
                    f"[get_messages] Error fetching messages: {e}\n"
                    f"[get_messages] Context: account_id={account_id}, "
                    f"chat={chat_telegram_id!r} ({type(chat_telegram_id).__name__}), "
                    f"start={start_utc!r}, end={end_utc!r}, "
                    f"topic_ids={topic_ids!r}, fetched_so_far={len(messages_by_id)}",
                    flush=True
                )
                traceback.print_exc()
                return []

        if last_error:
            print(f"[get_messages] Giving up after {max_fetch_attempts} attempts: {last_error}", flush=True)
        return []

    @staticmethod
    def _report_datetime_to_utc(value: datetime) -> datetime:
        """Interpret naive UI values in the configured report timezone."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=REPORT_TIMEZONE)
        return value.astimezone(UTC)

    @staticmethod
    def _telegram_datetime_to_utc(value: datetime) -> datetime:
        """Normalize Telethon timestamps, whose naive form is always UTC."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

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


def init_telegram_manager(api_id: int, api_hash: str, use_proxy: bool = True):
    global telegram_manager
    telegram_manager = TelegramClientManager(api_id, api_hash, use_proxy)
    return telegram_manager


def get_telegram_manager() -> TelegramClientManager:
    if telegram_manager is None:
        raise RuntimeError("Telegram manager not initialized")
    return telegram_manager
