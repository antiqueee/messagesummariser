import aiosqlite
from datetime import datetime
from pathlib import Path
from typing import Optional

DATABASE_PATH = Path(__file__).parent.parent / "data" / "app.db"


async def init_db():
    """Initialize database with required tables"""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Telegram accounts table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                is_authorized INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        # Residential complexes table (ЖК)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS complexes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sort_order INTEGER DEFAULT 999,
                created_at TEXT NOT NULL
            )
        """)

        # Add sort_order column if it doesn't exist (for existing DBs)
        try:
            await db.execute("ALTER TABLE complexes ADD COLUMN sort_order INTEGER DEFAULT 999")
        except:
            pass  # Column already exists

        # Chats table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                complex_id INTEGER,
                original_title TEXT NOT NULL,
                custom_name TEXT,
                is_monitored INTEGER DEFAULT 0,
                content_filter TEXT,
                selected_topics TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(id),
                FOREIGN KEY (complex_id) REFERENCES complexes(id),
                UNIQUE(telegram_id, account_id)
            )
        """)

        # Add content_filter and selected_topics columns if they don't exist (for existing DBs)
        try:
            await db.execute("ALTER TABLE chats ADD COLUMN content_filter TEXT")
        except:
            pass
        try:
            await db.execute("ALTER TABLE chats ADD COLUMN selected_topics TEXT")
        except:
            pass

        # Max messenger accounts table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS max_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                is_authorized INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        # Add source column to chats (telegram or max)
        try:
            await db.execute("ALTER TABLE chats ADD COLUMN source TEXT DEFAULT 'telegram'")
        except:
            pass

        # Add max_account_id column to chats
        try:
            await db.execute("ALTER TABLE chats ADD COLUMN max_account_id INTEGER")
        except:
            pass

        # Settings table (for storing editable reglament etc)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        await db.commit()


# Account operations
async def create_account(phone: str, name: str) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO accounts (phone, name, is_authorized, created_at) VALUES (?, ?, 0, ?)",
            (phone, name, datetime.now().isoformat())
        )
        await db.commit()
        return cursor.lastrowid


async def get_accounts() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM accounts ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_account(account_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_account_authorized(account_id: int, is_authorized: bool):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE accounts SET is_authorized = ? WHERE id = ?",
            (1 if is_authorized else 0, account_id)
        )
        await db.commit()


async def delete_account(account_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM chats WHERE account_id = ?", (account_id,))
        await db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        await db.commit()


# Max account operations
async def create_max_account(phone: str, name: str) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO max_accounts (phone, name, is_authorized, created_at) VALUES (?, ?, 0, ?)",
            (phone, name, datetime.now().isoformat())
        )
        await db.commit()
        return cursor.lastrowid


async def get_max_accounts() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM max_accounts ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_max_account(account_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM max_accounts WHERE id = ?", (account_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_max_account_authorized(account_id: int, is_authorized: bool):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE max_accounts SET is_authorized = ? WHERE id = ?",
            (1 if is_authorized else 0, account_id)
        )
        await db.commit()


async def delete_max_account(account_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM chats WHERE max_account_id = ? AND source = 'max'", (account_id,))
        await db.execute("DELETE FROM max_accounts WHERE id = ?", (account_id,))
        await db.commit()


# Complex operations
async def create_complex(name: str) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO complexes (name, created_at) VALUES (?, ?)",
            (name, datetime.now().isoformat())
        )
        await db.commit()
        return cursor.lastrowid


async def get_complexes() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM complexes ORDER BY sort_order, name")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_complex(complex_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM complexes WHERE id = ?", (complex_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_complex(complex_id: int, name: str, sort_order: int = None):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        if sort_order is not None:
            await db.execute("UPDATE complexes SET name = ?, sort_order = ? WHERE id = ?", (name, sort_order, complex_id))
        else:
            await db.execute("UPDATE complexes SET name = ? WHERE id = ?", (name, complex_id))
        await db.commit()


async def delete_complex(complex_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE chats SET complex_id = NULL WHERE complex_id = ?", (complex_id,))
        await db.execute("DELETE FROM complexes WHERE id = ?", (complex_id,))
        await db.commit()


# Chat operations
async def upsert_chat(telegram_id: int, account_id: int, original_title: str) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Check if exists
        cursor = await db.execute(
            "SELECT id FROM chats WHERE telegram_id = ? AND account_id = ?",
            (telegram_id, account_id)
        )
        existing = await cursor.fetchone()

        if existing:
            # Update title if changed
            await db.execute(
                "UPDATE chats SET original_title = ? WHERE id = ?",
                (original_title, existing[0])
            )
            await db.commit()
            return existing[0]
        else:
            cursor = await db.execute(
                """INSERT INTO chats (telegram_id, account_id, original_title, created_at)
                   VALUES (?, ?, ?, ?)""",
                (telegram_id, account_id, original_title, datetime.now().isoformat())
            )
            await db.commit()
            return cursor.lastrowid


async def get_chats(account_id: Optional[int] = None, complex_id: Optional[int] = None,
                    monitored_only: bool = False) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row

        query = """
            SELECT c.*, cx.name as complex_name,
                   COALESCE(a.name, ma.name) as account_name
            FROM chats c
            LEFT JOIN complexes cx ON c.complex_id = cx.id
            LEFT JOIN accounts a ON c.account_id = a.id AND (c.source IS NULL OR c.source = 'telegram')
            LEFT JOIN max_accounts ma ON c.max_account_id = ma.id AND c.source = 'max'
            WHERE 1=1
        """
        params = []

        if account_id is not None:
            query += " AND c.account_id = ?"
            params.append(account_id)

        if complex_id is not None:
            query += " AND c.complex_id = ?"
            params.append(complex_id)

        if monitored_only:
            query += " AND c.is_monitored = 1"

        query += " ORDER BY c.original_title"

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_chat(chat_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT c.*, cx.name as complex_name,
                   COALESCE(a.name, ma.name) as account_name
            FROM chats c
            LEFT JOIN complexes cx ON c.complex_id = cx.id
            LEFT JOIN accounts a ON c.account_id = a.id AND (c.source IS NULL OR c.source = 'telegram')
            LEFT JOIN max_accounts ma ON c.max_account_id = ma.id AND c.source = 'max'
            WHERE c.id = ?
        """, (chat_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_chat(chat_id: int, custom_name: Optional[str] = None,
                      complex_id: Optional[int] = None, is_monitored: Optional[bool] = None,
                      content_filter: Optional[str] = None, selected_topics: Optional[str] = None):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        updates = []
        params = []

        if custom_name is not None:
            updates.append("custom_name = ?")
            params.append(custom_name if custom_name else None)

        if complex_id is not None:
            updates.append("complex_id = ?")
            params.append(complex_id if complex_id > 0 else None)

        if is_monitored is not None:
            updates.append("is_monitored = ?")
            params.append(1 if is_monitored else 0)

        if content_filter is not None:
            updates.append("content_filter = ?")
            params.append(content_filter if content_filter else None)

        if selected_topics is not None:
            updates.append("selected_topics = ?")
            params.append(selected_topics if selected_topics else None)

        if updates:
            params.append(chat_id)
            query = f"UPDATE chats SET {', '.join(updates)} WHERE id = ?"
            await db.execute(query, params)
            await db.commit()


async def get_monitored_chats_by_complex() -> dict[int, list[dict]]:
    """Get all monitored chats grouped by complex_id, ordered by complex sort_order"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT c.*, cx.name as complex_name, cx.sort_order as complex_sort_order,
                   COALESCE(a.name, ma.name) as account_name,
                   COALESCE(a.phone, ma.phone) as account_phone
            FROM chats c
            JOIN complexes cx ON c.complex_id = cx.id
            LEFT JOIN accounts a ON c.account_id = a.id AND c.source = 'telegram'
            LEFT JOIN max_accounts ma ON c.max_account_id = ma.id AND c.source = 'max'
            WHERE c.is_monitored = 1 AND c.complex_id IS NOT NULL
            ORDER BY cx.sort_order, cx.name, c.original_title
        """)
        rows = await cursor.fetchall()

        result = {}
        for row in rows:
            row_dict = dict(row)
            cid = row_dict['complex_id']
            if cid not in result:
                result[cid] = []
            result[cid].append(row_dict)

        return result


async def upsert_max_chat(max_chat_id: int, max_account_id: int, original_title: str) -> int:
    """Create or update a chat from Max messenger"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM chats WHERE telegram_id = ? AND max_account_id = ? AND source = 'max'",
            (max_chat_id, max_account_id)
        )
        existing = await cursor.fetchone()

        if existing:
            await db.execute(
                "UPDATE chats SET original_title = ? WHERE id = ?",
                (original_title, existing[0])
            )
            await db.commit()
            return existing[0]
        else:
            cursor = await db.execute(
                """INSERT INTO chats (telegram_id, account_id, max_account_id, original_title, source, created_at)
                   VALUES (?, 0, ?, ?, 'max', ?)""",
                (max_chat_id, max_account_id, original_title, datetime.now().isoformat())
            )
            await db.commit()
            return cursor.lastrowid


# Settings operations
async def get_setting(key: str) -> Optional[str]:
    """Get a setting value by key"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_setting(key: str, value: str):
    """Set a setting value (insert or update)"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """, (key, value, datetime.now().isoformat()))
        await db.commit()
