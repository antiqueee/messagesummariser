from __future__ import annotations

from datetime import datetime
from typing import Optional

from . import database as db
from .max_client import get_max_manager
from .telegram_client import get_telegram_manager
from .vk_client import get_vk_manager


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

            vk_acc = await db.get_vk_account(vk_account_id)
            if not vk_acc or not vk_acc.get("access_token"):
                raise SourceMessageFetchError(source, "VK аккаунт не авторизован")

            peer_id = int(chat.get("source_chat_id") or chat["telegram_id"])
            return await vk.get_messages(
                access_token=vk_acc["access_token"],
                peer_id=peer_id,
                start_date=start_date,
                end_date=end_date,
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
