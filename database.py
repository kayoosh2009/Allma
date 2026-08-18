# database.py
# Асинхронная работа с SQLite: пользователи, история переписки, статистика.
#
# Таблицы:
#   users    - карточка пользователя
#   messages - вся история переписки (для контекста и /clean)
#   stats    - агрегированная статистика по токенам и сообщениям

import datetime
import os

import aiosqlite

DB_PATH = os.environ.get("DB_PATH", "bot.db")

CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    created_at  TEXT,
    is_banned   INTEGER DEFAULT 0
)
"""

CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    role        TEXT,       -- 'user' или 'assistant'
    content     TEXT,
    created_at  TEXT,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
)
"""

CREATE_STATS = """
CREATE TABLE IF NOT EXISTS stats (
    user_id             INTEGER PRIMARY KEY,
    prompt_tokens       INTEGER DEFAULT 0,
    completion_tokens   INTEGER DEFAULT 0,
    messages_count      INTEGER DEFAULT 0,
    last_active         TEXT,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
)
"""


async def init_db() -> None:
    """Создаёт таблицы, если их ещё нет. Вызывается один раз при старте."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_USERS)
        await db.execute(CREATE_MESSAGES)
        await db.execute(CREATE_STATS)
        await db.commit()


async def ensure_user(user_id: int, username: str | None, first_name: str | None) -> None:
    """Регистрирует пользователя, если он ещё не встречался."""
    now = datetime.datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, username, first_name, now),
        )
        await db.execute(
            "INSERT OR IGNORE INTO stats (user_id, last_active) VALUES (?, ?)",
            (user_id, now),
        )
        await db.commit()


async def add_message(user_id: int, role: str, content: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, datetime.datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_history(user_id: int, limit: int = 20) -> list[dict]:
    """Возвращает последние `limit` сообщений в хронологическом порядке."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, content FROM messages WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        rows = list(rows)
        rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]


async def clear_history(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        await db.commit()


async def update_stats(user_id: int, prompt_tokens: int, completion_tokens: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE stats
            SET prompt_tokens     = prompt_tokens + ?,
                completion_tokens = completion_tokens + ?,
                messages_count    = messages_count + 1,
                last_active       = ?
            WHERE user_id = ?
            """,
            (prompt_tokens, completion_tokens, datetime.datetime.utcnow().isoformat(), user_id),
        )
        await db.commit()


async def get_stats(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM stats WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None