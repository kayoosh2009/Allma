import time
import aiosqlite

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER,
    role TEXT NOT NULL,           -- 'user' или 'assistant'
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, created_at);

CREATE TABLE IF NOT EXISTS reacted_posts (
    message_id INTEGER PRIMARY KEY,
    reacted_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mood (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    value INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.executescript(_SCHEMA)
        cur = await db.execute("SELECT COUNT(*) FROM mood")
        (count,) = await cur.fetchone()
        if count == 0:
            await db.execute(
                "INSERT INTO mood (id, value, updated_at) VALUES (1, 50, ?)",
                (time.time(),),
            )
        await db.commit()


async def add_message(chat_id: int, role: str, content: str, user_id: int | None = None) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, user_id, role, content, time.time()),
        )
        await db.commit()


async def get_history(chat_id: int, limit: int = 20) -> list[dict]:
    """Возвращает последние `limit` сообщений диалога в хронологическом порядке."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY created_at DESC LIMIT ?",
            (chat_id, limit),
        )
        rows = await cur.fetchall()
    rows.reverse()
    return [{"role": role, "content": content} for role, content in rows]


async def was_reacted(message_id: int) -> bool:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM reacted_posts WHERE message_id = ?", (message_id,)
        )
        return await cur.fetchone() is not None


async def mark_reacted(message_id: int) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO reacted_posts (message_id, reacted_at) VALUES (?, ?)",
            (message_id, time.time()),
        )
        await db.commit()


async def get_mood() -> int:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT value FROM mood WHERE id = 1")
        (value,) = await cur.fetchone()
        return value


async def set_mood(value: int) -> None:
    value = max(0, min(100, value))
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE mood SET value = ?, updated_at = ? WHERE id = 1",
            (value, time.time()),
        )
        await db.commit()
