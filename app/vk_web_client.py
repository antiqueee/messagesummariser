import asyncio
import re
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
import httpx


VK_WEB_PROFILE_DIR = Path(__file__).parent.parent / "sessions" / "vk_web_profile"


class VkWebClientManager:
    """Persistent local Chrome session used when VK's messages API is blocked."""

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._web_api_headers: dict[str, str] = {}
        self._web_api_auth_params: dict[str, str] = {}

    async def open_login(self) -> dict:
        async with self._lock:
            page = await self._ensure_page()
            await page.goto("https://vk.com/im", wait_until="domcontentloaded", timeout=60_000)
            await page.bring_to_front()
            return await self.status()

    async def status(self) -> dict:
        page = self._page
        if not page or page.is_closed():
            return {"running": False, "authorized": False, "message": "Браузер VK не запущен"}

        cookies = await self._context.cookies("https://vk.com") if self._context else []
        cookie_names = {cookie.get("name") for cookie in cookies}
        authorized = bool(cookie_names.intersection({"remixsid", "remixsid6"}))
        url = page.url
        if "login" in url or "join" in url:
            authorized = False
        return {
            "running": True,
            "authorized": authorized,
            "url": url,
            "message": "VK через браузер подключён" if authorized else "Войдите в VK в открытом окне",
        }

    async def open_chat(self, peer_id: int) -> dict:
        async with self._lock:
            page = await self._ensure_page()
            network_urls: list[str] = []
            network_requests: list[dict] = []

            def remember_response(response):
                url = response.url
                if any(marker in url.lower() for marker in ("message", "history", "convo", "im")):
                    network_urls.append(url[:1000])

            def remember_request(request):
                if "web.api.vk.ru/method/" not in request.url:
                    return
                headers = dict(request.headers)
                post_data = request.post_data or ""
                try:
                    from urllib.parse import parse_qsl
                    params = dict(parse_qsl(post_data, keep_blank_values=True))
                except Exception:
                    params = {}
                self._web_api_headers = headers
                for key in ("access_token", "web_token", "client_secret", "oauth"):
                    if params.get(key):
                        self._web_api_auth_params[key] = params[key]
                network_requests.append({
                    "url": request.url[:500],
                    "method": request.method,
                    "headerKeys": sorted(headers),
                    "postDataKeys": sorted(params),
                })

            page.on("response", remember_response)
            page.on("request", remember_request)
            await page.goto(
                f"https://vk.ru/im/convo/{peer_id}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await page.wait_for_timeout(3_000)
            page.remove_listener("response", remember_response)
            page.remove_listener("request", remember_request)
            result = await self.inspect_page()
            result["networkUrls"] = network_urls[:200]
            result["networkRequests"] = network_requests[:100]
            return result

    async def test_web_history(self, peer_id: int) -> dict:
        """Diagnostic: call the same cookie-authenticated API used by vk.ru."""
        async with self._lock:
            await self._ensure_page()
            access_token = self._web_api_auth_params.get("access_token")
            if not access_token:
                raise RuntimeError("Веб-токен ещё не получен: сначала откройте любой VK чат")
            headers = {
                key: value
                for key, value in self._web_api_headers.items()
                if key.lower() in {"user-agent", "referer", "accept"}
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://web.api.vk.ru/method/messages.getHistory",
                    params={"v": "5.285", "client_id": "6287487"},
                    data={
                        "access_token": access_token,
                        "peer_id": str(peer_id),
                        "count": "20",
                        "offset": "0",
                        "extended": "1",
                    },
                    headers=headers,
                )
            return {"status": response.status_code, "text": response.text[:20_000]}

    async def inspect_page(self) -> dict:
        """Non-sensitive DOM diagnostics for adapting the reader to VK's current UI."""
        page = self._page
        if not page or page.is_closed():
            raise RuntimeError("Браузер VK не запущен")
        return await page.evaluate(
            """
            () => ({
                url: location.href,
                title: document.title,
                textSample: (document.body?.innerText || '').slice(0, 1000),
                dataTestIds: [...new Set([...document.querySelectorAll('[data-testid]')]
                    .map(el => el.getAttribute('data-testid')).filter(Boolean))].slice(0, 100),
                messageLikeClasses: [...new Set([...document.querySelectorAll('[class*="message" i]')]
                    .flatMap(el => [...el.classList]).filter(name => /message/i.test(name)))].slice(0, 100),
                messageSamples: [...document.querySelectorAll('.ConvoHistory__messageBlock')].slice(-5).map(el => ({
                    text: (el.innerText || '').slice(0, 1000),
                    attributes: Object.fromEntries([...el.attributes].map(attr => [attr.name, attr.value])),
                    html: el.outerHTML.slice(0, 6000),
                })),
                dateElements: [...document.querySelectorAll('[class*="date" i]')]
                    .filter(el => (el.textContent || '').trim())
                    .slice(-100).map(el => ({
                        text: (el.textContent || '').trim().slice(0, 200),
                        className: typeof el.className === 'string' ? el.className : '',
                        title: el.getAttribute('title'),
                        dateTime: el.getAttribute('datetime'),
                        ariaLabel: el.getAttribute('aria-label'),
                    })),
                scrollables: [...document.querySelectorAll('*')]
                    .filter(el => el.scrollHeight > el.clientHeight + 100 && el.querySelector('.ConvoHistory__messageBlock'))
                    .map(el => ({
                        className: typeof el.className === 'string' ? el.className : '',
                        clientHeight: el.clientHeight,
                        scrollHeight: el.scrollHeight,
                        scrollTop: el.scrollTop,
                    })).slice(0, 20),
                historyText: (document.querySelector('.ConvoHistory')?.innerText || '').slice(-6000),
                historyButtons: [...document.querySelectorAll('button')]
                    .filter(el => el.closest('.ConvoHistory') || /scroll|bottom|down|unread/i.test(el.className || ''))
                    .map(el => ({
                        text: (el.textContent || '').trim().slice(0, 100),
                        className: typeof el.className === 'string' ? el.className : '',
                        ariaLabel: el.getAttribute('aria-label'),
                        title: el.getAttribute('title'),
                        testId: el.getAttribute('data-testid'),
                    })).slice(0, 100),
            })
            """
        )

    async def get_messages(
        self,
        peer_id: int,
        start_date: datetime,
        end_date: datetime,
        limit: int = 10_000,
    ) -> list[dict]:
        """Read history through the authenticated vk.ru browser session."""
        async with self._lock:
            page = await self._ensure_page()
            status = await self.status()
            if not status["authorized"]:
                raise RuntimeError("Сначала подключите VK через браузер во вкладке «Аккаунты»")

            api_exc: Optional[Exception] = None
            for attempt in range(2):
                try:
                    return await self._get_messages_via_web_api(
                        page=page,
                        peer_id=peer_id,
                        start_date=start_date,
                        end_date=end_date,
                        limit=limit,
                    )
                except Exception as exc:
                    api_exc = exc
                    if attempt == 0:
                        print(
                            f"[VK Web] Chat {peer_id}: Web API failed ({exc}); "
                            "refreshing browser token",
                            flush=True,
                        )
                        self._web_api_auth_params.clear()
                        self._web_api_headers.clear()

            print(
                f"[VK Web] Chat {peer_id}: refreshed Web API failed ({api_exc}); "
                "reading visible browser history",
                flush=True,
            )
            try:
                return await self._get_messages_from_page(
                    page=page,
                    peer_id=peer_id,
                    start_date=start_date,
                    end_date=end_date,
                    limit=limit,
                )
            except Exception as page_exc:
                raise RuntimeError(
                    f"web API: {api_exc}; страница VK: {page_exc}"
                ) from page_exc

    async def _get_messages_via_web_api(
        self,
        page: Page,
        peer_id: int,
        start_date: datetime,
        end_date: datetime,
        limit: int,
    ) -> list[dict]:
        """Use the short-lived token emitted by the authenticated web client."""
        await self._ensure_web_api_credentials(page, peer_id)
        token = self._web_api_auth_params.get("access_token")
        if not token:
            raise RuntimeError("VK не передал браузерный ключ сообщений")

        start_ts = int(start_date.timestamp())
        end_ts = int(end_date.timestamp())
        sender_cache: dict[int, str] = {}
        messages: list[dict] = []
        offset = 0
        reached_start = False
        headers = {
            key: value
            for key, value in self._web_api_headers.items()
            if key.lower() in {"user-agent", "referer", "accept"}
        }

        async with httpx.AsyncClient(timeout=45.0) as client:
            while len(messages) < limit:
                response = await client.post(
                    "https://web.api.vk.ru/method/messages.getHistory",
                    params={"v": "5.285", "client_id": "6287487"},
                    data={
                        "access_token": token,
                        "peer_id": str(peer_id),
                        "count": str(min(200, limit - len(messages))),
                        "offset": str(offset),
                        "extended": "1",
                    },
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("error"):
                    error = payload["error"]
                    raise RuntimeError(
                        f"VK Web API: {error.get('error_msg') or error}"
                    )
                history = payload.get("response") or {}
                items = history.get("items") or []
                if not items:
                    break

                for profile in history.get("profiles") or []:
                    sender_id = int(profile.get("id") or 0)
                    if sender_id:
                        sender_cache[sender_id] = " ".join(
                            filter(None, [profile.get("first_name"), profile.get("last_name")])
                        ) or str(sender_id)
                for group in history.get("groups") or []:
                    group_id = int(group.get("id") or 0)
                    if group_id:
                        sender_cache[-group_id] = group.get("name") or f"club{group_id}"

                for item in items:
                    msg_ts = int(item.get("date") or 0)
                    if msg_ts < start_ts:
                        reached_start = True
                        break
                    if msg_ts > end_ts:
                        continue
                    text = (item.get("text") or "").strip()
                    if not text:
                        text = self._attachment_placeholder(item)
                    if not text:
                        continue
                    sender_id = int(item.get("from_id") or 0)
                    messages.append({
                        "message_id": int(item.get("id") or item.get("conversation_message_id") or 0),
                        "sender_id": sender_id,
                        "sender_name": sender_cache.get(sender_id) or str(sender_id or "Unknown"),
                        "text": text,
                        "date": datetime.fromtimestamp(msg_ts).isoformat() + "Z",
                        "reply_to": (item.get("reply_message") or {}).get("id"),
                        "topic_id": None,
                    })

                offset += len(items)
                if reached_start or len(items) < 200:
                    break
                await asyncio.sleep(0.35)

        messages.sort(key=lambda item: item["date"])
        print(
            f"[VK Web API] Chat {peer_id}: fetched {len(messages)} messages "
            f"from {start_date.isoformat()} to {end_date.isoformat()}",
            flush=True,
        )
        return messages

    async def _get_messages_from_page(
        self,
        page: Page,
        peer_id: int,
        start_date: datetime,
        end_date: datetime,
        limit: int,
    ) -> list[dict]:
        await page.goto(
            f"https://vk.ru/im/convo/{peer_id}",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        try:
            await page.wait_for_selector(
                ".ConvoHistory__messageBlock",
                timeout=20_000,
            )
        except Exception as exc:
            page_state = await page.evaluate(
                """() => ({
                    title: document.title,
                    text: (document.body?.innerText || '').trim().slice(0, 500),
                })"""
            )
            raise RuntimeError(
                f"история чата не появилась ({page_state.get('title') or 'без заголовка'}: "
                f"{page_state.get('text') or 'пустая страница'})"
            ) from exc

        await self._move_history_to_latest(page, end_date)
        collected: dict[str, dict] = {}
        oldest_seen: Optional[datetime] = None
        stalled_rounds = 0
        raw_message_count = 0

        for _ in range(120):
            stacks = await self._extract_visible_date_stacks(page)
            raw_message_count = max(
                raw_message_count,
                sum(len(stack.get("messages") or []) for stack in stacks),
            )
            parsed_dates = self._collect_dom_messages(
                stacks=stacks,
                start_date=start_date,
                end_date=end_date,
                collected=collected,
            )
            current_oldest = min(parsed_dates) if parsed_dates else None
            if current_oldest and (oldest_seen is None or current_oldest < oldest_seen):
                oldest_seen = current_oldest
                stalled_rounds = 0
            else:
                stalled_rounds += 1
            if current_oldest and current_oldest <= start_date:
                break
            if stalled_rounds >= 5:
                break

            scroll_target = await page.evaluate(
                """() => {
                    const message = document.querySelector('.ConvoHistory__messageBlock');
                    let node = document.querySelector('.ConvoHistory__scrollbar');
                    if (!node) {
                        node = message?.parentElement || document.querySelector('.ConvoHistory');
                        while (node && node !== document.body) {
                            const style = getComputedStyle(node);
                            if (node.scrollHeight > node.clientHeight + 20
                                && ['auto', 'scroll'].includes(style.overflowY)) break;
                            node = node.parentElement;
                        }
                    }
                    if (!node || node === document.body) {
                        node = [...document.querySelectorAll('*')].find(candidate =>
                            candidate.scrollHeight > candidate.clientHeight + 100
                            && candidate.querySelector('.ConvoHistory__messageBlock'));
                    }
                    if (!node) return null;
                    const rect = node.getBoundingClientRect();
                    const before = node.scrollTop;
                    node.scrollTop = Math.max(0, before - Math.max(node.clientHeight * 0.85, 600));
                    node.dispatchEvent(new Event('scroll', {bubbles: true}));
                    return {
                        x: rect.left + rect.width / 2,
                        y: rect.top + Math.min(rect.height / 2, 300),
                    };
                }"""
            )
            if not scroll_target:
                raise RuntimeError("не найден контейнер прокрутки истории")
            await page.mouse.move(scroll_target["x"], scroll_target["y"])
            await page.mouse.wheel(0, -1_500)
            await page.wait_for_timeout(700)

        if raw_message_count and not oldest_seen:
            raise RuntimeError("сообщения видны, но VK изменил разметку даты или времени")

        messages = sorted(collected.values(), key=lambda item: item["date"])
        print(
            f"[VK Browser DOM] Chat {peer_id}: fetched {len(messages)} messages "
            f"from {start_date.isoformat()} to {end_date.isoformat()}",
            flush=True,
        )
        return messages[:limit]

    async def _move_history_to_latest(self, page: Page, end_date: datetime) -> None:
        hop_button = page.locator(".HopNavigationButton:visible")
        if await hop_button.count():
            await hop_button.last.click()
            await page.wait_for_timeout(800)

        latest_seen: Optional[datetime] = None
        stalled_rounds = 0
        for _ in range(120):
            stacks = await self._extract_visible_date_stacks(page)
            visible_dates = self._dom_dates_from_stacks(stacks, end_date)
            current_latest = max(visible_dates) if visible_dates else None
            if current_latest and current_latest >= end_date:
                return
            if current_latest and (latest_seen is None or current_latest > latest_seen):
                latest_seen = current_latest
                stalled_rounds = 0
            else:
                stalled_rounds += 1
            if stalled_rounds >= 8:
                return

            scroll_target = await page.evaluate(
                """() => {
                    const message = document.querySelector('.ConvoHistory__messageBlock');
                    let node = document.querySelector('.ConvoHistory__scrollbar');
                    if (!node) {
                        node = message?.parentElement || document.querySelector('.ConvoHistory');
                        while (node && node !== document.body) {
                            const style = getComputedStyle(node);
                            if (node.scrollHeight > node.clientHeight + 20
                                && ['auto', 'scroll'].includes(style.overflowY)) break;
                            node = node.parentElement;
                        }
                    }
                    if (!node || node === document.body) return null;
                    const rect = node.getBoundingClientRect();
                    node.scrollTop = node.scrollHeight;
                    node.dispatchEvent(new Event('scroll', {bubbles: true}));
                    return {
                        x: rect.left + rect.width / 2,
                        y: rect.top + Math.min(rect.height / 2, 300),
                    };
                }"""
            )
            if not scroll_target:
                raise RuntimeError("не найден контейнер прокрутки истории")
            await page.mouse.move(scroll_target["x"], scroll_target["y"])
            await page.mouse.wheel(0, 10_000)
            await page.wait_for_timeout(500)

    def _dom_dates_from_stacks(
        self,
        stacks: list[dict],
        reference: datetime,
    ) -> list[datetime]:
        dates = []
        for stack in stacks:
            date_label = stack.get("dateLabel") or ""
            for message in stack.get("messages") or []:
                message_date = self._parse_message_date(
                    date_label,
                    message.get("time") or message.get("dateTime") or "",
                    reference,
                )
                if message_date:
                    dates.append(message_date)
        return dates

    def _collect_dom_messages(
        self,
        stacks: list[dict],
        start_date: datetime,
        end_date: datetime,
        collected: dict[str, dict],
    ) -> list[datetime]:
        parsed_dates: list[datetime] = []
        for stack in stacks:
            date_label = stack.get("dateLabel") or ""
            for message in stack.get("messages") or []:
                message_date = self._parse_message_date(
                    date_label,
                    message.get("time") or message.get("dateTime") or "",
                    end_date,
                )
                if not message_date:
                    continue
                parsed_dates.append(message_date)
                if message_date < start_date or message_date > end_date:
                    continue

                text = (message.get("text") or "").strip()
                if not text:
                    labels = list(dict.fromkeys(message.get("attachmentLabels") or []))
                    text = " ".join(f"[{label}]" for label in labels)
                if not text:
                    continue

                sender_name = (message.get("senderName") or "Unknown").strip()
                profile_path = message.get("profilePath") or ""
                sender_id = self._sender_id_from_profile(profile_path, sender_name)
                raw_message_id = str(message.get("messageId") or "")
                id_match = re.search(r"(\d+)(?!.*\d)", raw_message_id)
                identity = "|".join(
                    [raw_message_id, message_date.isoformat(), profile_path, sender_name, text]
                )
                message_id = (
                    int(id_match.group(1))
                    if id_match
                    else zlib.crc32(identity.encode("utf-8"))
                )
                collected[identity] = {
                    "message_id": message_id,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "text": text,
                    "date": message_date.isoformat() + "Z",
                    "reply_to": None,
                    "topic_id": None,
                }
        return parsed_dates

    def _sender_id_from_profile(self, profile_path: str, sender_name: str) -> int:
        match = re.search(r"/(?:id)(\d+)", profile_path)
        if match:
            return int(match.group(1))
        match = re.search(r"/(?:club|public|event)(\d+)", profile_path)
        if match:
            return -int(match.group(1))
        return zlib.crc32((profile_path or sender_name).encode("utf-8"))

    async def _ensure_web_api_credentials(self, page: Page, peer_id: int) -> None:
        if self._web_api_auth_params.get("access_token"):
            return

        def remember_request(request):
            if "web.api.vk.ru/method/messages.getHistory" not in request.url:
                return
            from urllib.parse import parse_qsl
            params = dict(parse_qsl(request.post_data or "", keep_blank_values=True))
            if params.get("access_token"):
                self._web_api_auth_params["access_token"] = params["access_token"]
                self._web_api_headers = dict(request.headers)

        page.on("request", remember_request)
        try:
            await page.goto(
                f"https://vk.ru/im/convo/{peer_id}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            for _ in range(30):
                if self._web_api_auth_params.get("access_token"):
                    break
                await page.wait_for_timeout(250)
        finally:
            page.remove_listener("request", remember_request)

    def _attachment_placeholder(self, item: dict) -> str:
        labels = []
        names = {
            "photo": "Фотография", "video": "Видео", "audio": "Аудио",
            "doc": "Документ", "sticker": "Стикер", "wall": "Запись",
            "audio_message": "Голосовое сообщение", "graffiti": "Граффити",
            "link": "Ссылка", "poll": "Опрос", "market": "Товар",
        }
        for attachment in item.get("attachments") or []:
            attachment_type = attachment.get("type") or "вложение"
            labels.append(names.get(attachment_type, attachment_type))
        if labels:
            return " ".join(f"[{label}]" for label in labels)
        action = item.get("action") or {}
        if action:
            return f"[Системное событие: {action.get('type') or 'action'}]"
        return ""

    async def _extract_visible_date_stacks(self, page: Page) -> list[dict]:
        return await page.evaluate(
            """
            () => [...document.querySelectorAll('.ConvoHistory__dateStack')].map(stack => ({
                dateLabel: (stack.querySelector('.DateSeparator')?.getAttribute('aria-label')
                    || stack.querySelector('.DateSeparator')?.textContent || '').trim(),
                messages: [...stack.querySelectorAll('.ConvoHistory__messageBlock')].map(block => ({
                    messageId: block.getAttribute('data-message-id')
                        || block.getAttribute('data-cmid')
                        || block.querySelector('[data-message-id]')?.getAttribute('data-message-id')
                        || block.querySelector('[data-cmid]')?.getAttribute('data-cmid')
                        || block.querySelector('a[href*="cmid="]')?.getAttribute('href')
                        || block.id || '',
                    senderName: (block.querySelector('.ConvoMessageHeader__authorLink .PeerTitle__title')?.textContent
                        || block.querySelector('.ConvoMessageWithoutBubble__avatar img')?.getAttribute('alt') || '').trim(),
                    profilePath: block.querySelector('.ConvoMessageHeader__authorLink')?.getAttribute('href')
                        || block.querySelector('.ConvoMessageWithoutBubble__avatar')?.getAttribute('href') || '',
                    text: (block.querySelector('.ConvoMessageWithoutBubble__text .MessageText')?.innerText || '').trim(),
                    time: (block.querySelector('.ConvoMessageInfoWithoutBubbles__date')?.textContent || '').trim(),
                    dateTime: block.querySelector('time')?.getAttribute('datetime')
                        || block.querySelector('[datetime]')?.getAttribute('datetime')
                        || block.querySelector('.ConvoMessageInfoWithoutBubbles__date')?.getAttribute('aria-label')
                        || '',
                    attachmentLabels: [...block.querySelectorAll(
                        '.ConvoMessageWithoutBubble__attachments [aria-label], .ConvoMessageWithoutBubble__mediaAttachments [aria-label]'
                    )].map(el => el.getAttribute('aria-label')).filter(Boolean).slice(0, 20),
                })),
            }))
            """
        )

    def _parse_message_date(
        self,
        date_label: str,
        time_label: str,
        reference: datetime,
    ) -> Optional[datetime]:
        months = {
            "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
            "мая": 5, "июня": 6, "июля": 7, "августа": 8,
            "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
        }
        label = re.sub(r"\s+", " ", date_label.strip().lower())
        if label == "сегодня":
            day = reference.date()
        elif label == "вчера":
            day = (reference - timedelta(days=1)).date()
        else:
            match = re.search(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", label)
            if not match or match.group(2) not in months:
                return None
            year = int(match.group(3) or reference.year)
            day = datetime(year, months[match.group(2)], int(match.group(1))).date()
            if not match.group(3) and day > (reference + timedelta(days=2)).date():
                day = day.replace(year=year - 1)

        time_match = re.search(r"(\d{1,2}):(\d{2})", time_label)
        if not time_match:
            return None
        return datetime.combine(
            day,
            datetime.min.time().replace(
                hour=int(time_match.group(1)), minute=int(time_match.group(2))
            ),
        )

    async def close(self) -> None:
        async with self._lock:
            await self._dispose_browser()

    async def _dispose_browser(self) -> None:
        context = self._context
        playwright = self._playwright
        self._context = None
        self._playwright = None
        self._page = None
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if playwright:
            try:
                await playwright.stop()
            except Exception:
                pass

    async def _ensure_page(self) -> Page:
        if self._page and not self._page.is_closed():
            return self._page

        if self._context:
            try:
                open_pages = [page for page in self._context.pages if not page.is_closed()]
                self._page = open_pages[0] if open_pages else await self._context.new_page()
                return self._page
            except Exception:
                await self._dispose_browser()

        VK_WEB_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(VK_WEB_PROFILE_DIR),
                channel="chrome",
                headless=False,
                viewport={"width": 1400, "height": 950},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            await self._dispose_browser()
            detail = str(exc)
            if "ProcessSingleton" in detail or "profile directory" in detail:
                raise RuntimeError(
                    "профиль браузера VK уже занят другим процессом Chrome; "
                    "закройте лишнее окно VK и повторите попытку"
                ) from exc
            raise
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        return self._page


_manager = VkWebClientManager()


def get_vk_web_manager() -> VkWebClientManager:
    return _manager
