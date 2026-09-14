from __future__ import annotations

from datetime import datetime
from typing import Optional

from . import database as db
from .max_client import get_max_manager
from .telegram_client import get_telegram_manager
from .vk_client import VkFloodControlError, get_vk_manager
from .vk_web_client import get_vk_web_manager


class SourceMessageFetchError(RuntimeError):
    """Raised when chat history cannot be fetched from a source provider."""

    def __init__(self, source: str, detail: str):
        self.source = source
        self.detail = detail
        super().__init__(detail)


def normalize_source_name(source: Optional[str]) -> str:
    return source or "telegram"


def get_source_label(source: Optional[str]) -> str:
    normalized = normalize_source_name(source)
    if normalized == "max":
        return "Max"
    if normalized == "vk":
        return "VK"
    return "Telegram"


def exclude_chat_sources(
    chats_by_complex: dict[int, list[dict]],
    excluded_sources: set[str],
) -> dict[int, list[dict]]:
    """Return report chats without selected sources, leaving DB data untouched."""
    normalized_exclusions = {
        normalize_source_name(source) for source in excluded_sources
    }
    if not normalized_exclusions:
        return dict(chats_by_complex)

    filtered: dict[int, list[dict]] = {}
    for complex_id, chats in chats_by_complex.items():
        included_chats = [
            chat
            for chat in chats
            if normalize_source_name(chat.get("source")) not in normalized_exclusions
        ]
        if included_chats:
            filtered[complex_id] = included_chats
    return filtered


async def fetch_chat_messages(
    chat: dict,
    start_date: datetime,
    end_date: datetime,
    topic_ids: Optional[list[int]] = None,
) -> list[dict]:
    """Fetch messages for a chat regardless of its messenger source."""
    source = normalize_source_name(chat.get("source"))

    try:
        if source == "max":
            mm = get_max_manager()
            max_account_id = chat.get("max_account_id")
            if not max_account_id:
                raise SourceMessageFetchError(source, "У чата Max отсутствует max_account_id")

            max_acc = await db.get_max_account(max_account_id)
            if not max_acc:
                raise SourceMessageFetchError(source, "Max аккаунт чата не найден")
            if not max_acc.get("is_authorized"):
                raise SourceMessageFetchError(source, "Max аккаунт чата не авторизован")

            connected = await mm.ensure_connected(max_account_id, max_acc["phone"])
            if not connected:
                raise SourceMessageFetchError(
                    source,
                    "Max аккаунт временно не подключился. Подождите 10-20 секунд и повторите генерацию; если повторяется постоянно, нужна переавторизация аккаунта Max."
                )

            return await mm.get_messages(
                account_id=max_account_id,
                chat_id=chat["telegram_id"],
                start_date=start_date,
                end_date=end_date,
                phone=max_acc["phone"],
            )

        if source == "vk":
            vk = get_vk_manager()
            vk_account_id = chat.get("source_account_id")
            if not vk_account_id:
                raise SourceMessageFetchError(source, "У чата VK отсутствует source_account_id")

            primary_account = await db.get_vk_account(vk_account_id)
            if not primary_account or not primary_account.get("access_token"):
                raise SourceMessageFetchError(source, "VK аккаунт не авторизован")

            peer_id = int(chat.get("source_chat_id") or chat["telegram_id"])
            accounts = [primary_account]
            accounts.extend(
                account
                for account in await db.get_vk_accounts()
                if account.get("id") != vk_account_id
                and account.get("is_authorized")
                and account.get("access_token")
            )

            failures: list[str] = []
            last_exception: Optional[Exception] = None
            for index, account in enumerate(accounts):
                try:
                    messages = await vk.get_messages(
                        access_token=account["access_token"],
                        peer_id=peer_id,
                        start_date=start_date,
                        end_date=end_date,
                    )
                    if index:
                        print(
                            f"[VK] Chat {peer_id}: read through reserve account "
                            f"{account.get('id')} ({account.get('name')})",
                            flush=True,
                        )
                    return messages
                except Exception as exc:
                    last_exception = exc
                    failures.append(f"{account.get('name') or account.get('id')}: {exc}")
                    if index == 0 and isinstance(exc, VkFloodControlError):
                        print(
                            f"[VK] Chat {peer_id}: primary account is flood-blocked, "
                            "trying reserve accounts",
                            flush=True,
                        )

            web = get_vk_web_manager()
            print(f"[VK] Chat {peer_id}: falling back to authenticated browser", flush=True)
            browser_failure: Optional[Exception] = None
            try:
                return await web.get_messages(
                    peer_id=peer_id,
                    start_date=start_date,
                    end_date=end_date,
                )
            except Exception as web_exc:
                browser_failure = web_exc
                failures.append(f"VK через браузер: {web_exc}")

            if len(accounts) == 1 and isinstance(last_exception, VkFloodControlError):
                raise SourceMessageFetchError(
                    source,
                    "VK заблокировал API сообщений текущего аккаунта (Flood control), "
                    "а чтение страницы через браузер тоже не сработало: "
                    f"{browser_failure}",
                )
            raise SourceMessageFetchError(
                source,
                "Ни один подключённый VK аккаунт не смог прочитать чат: "
                + "; ".join(failures),
            )

        tm = get_telegram_manager()
        return await tm.get_messages(
            account_id=chat["account_id"],
            chat_telegram_id=chat["telegram_id"],
            start_date=start_date,
            end_date=end_date,
            topic_ids=topic_ids,
        )
    except SourceMessageFetchError:
        raise
    except Exception as e:
        raise SourceMessageFetchError(source, str(e)) from e
