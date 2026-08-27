#!/usr/bin/env python3
import asyncio
import os
import sys
from collections import defaultdict
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import DATABASE_PATH
from app.telegram_client import init_telegram_manager


def parse_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def load_chats() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                ch.id,
                ch.telegram_id,
                ch.account_id,
                COALESCE(ch.custom_name, ch.original_title) AS chat_name,
                COALESCE(c.name, 'Без ЖК') AS complex_name
            FROM chats ch
            LEFT JOIN complexes c ON c.id = ch.complex_id
            WHERE ch.is_monitored = 1
              AND COALESCE(ch.source, 'telegram') = 'telegram'
            ORDER BY c.sort_order, c.name, chat_name
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def main():
    load_dotenv(ROOT / ".env")
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("TELEGRAM_API_ID / TELEGRAM_API_HASH are not configured")

    use_proxy = parse_bool_env("TELEGRAM_USE_PROXY", True)
    manager = init_telegram_manager(int(api_id), api_hash, use_proxy=use_proxy)
    chats = await load_chats()
    bots: dict[int, dict] = {}
    chat_errors: list[str] = []

    print(f"[BotScan] Scanning {len(chats)} monitored Telegram chats, use_proxy={use_proxy}", flush=True)

    try:
        for index, chat in enumerate(chats, start=1):
            label = f"{chat['complex_name']} / {chat['chat_name']}"
            print(f"[BotScan] {index}/{len(chats)} {label}", flush=True)

            client = await manager.get_client(int(chat["account_id"]))
            if not client:
                chat_errors.append(f"{label}: account {chat['account_id']} is not connected")
                continue

            try:
                entity = await client.get_entity(int(chat["telegram_id"]))
                async for user in client.iter_participants(entity):
                    if not getattr(user, "bot", False):
                        continue

                    bot_id = int(user.id)
                    if bot_id not in bots:
                        name = " ".join(filter(None, [user.first_name, user.last_name])).strip()
                        bots[bot_id] = {
                            "id": bot_id,
                            "username": user.username or "",
                            "name": name or user.username or str(bot_id),
                            "chats": [],
                        }
                    bots[bot_id]["chats"].append(label)
            except Exception as e:
                chat_errors.append(f"{label}: {type(e).__name__}: {e}")

        print("\n=== TELEGRAM BOTS ===")
        if not bots:
            print("Не найдено")
        else:
            for bot in sorted(bots.values(), key=lambda item: (item["username"].lower(), item["name"].lower())):
                username = f"@{bot['username']}" if bot["username"] else "без username"
                print(f"{bot['name']} | {username} | id={bot['id']} | chats={len(bot['chats'])}")
                for chat_name in bot["chats"][:12]:
                    print(f"  - {chat_name}")
                if len(bot["chats"]) > 12:
                    print(f"  ... еще {len(bot['chats']) - 12}")

        if chat_errors:
            print("\n=== CHATS WITH ERRORS ===")
            for error in chat_errors:
                print(f"- {error}")
    finally:
        await manager.close_all()


if __name__ == "__main__":
    asyncio.run(main())
