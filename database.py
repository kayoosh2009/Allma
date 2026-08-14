from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiosqlite


def get_db_path() -> Path:
    """
    Возвращает путь к базе данных.
    Читается из переменной окружения DB_PATH.
    """
    return Path(os.getenv("DB_PATH", "alma.db"))


async def init_db() -> None:
    """
    Создает таблицы, если их еще нет.
    """
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_history_chat_id
            ON chat_history(chat_id, id)
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS gifs (
                file_id TEXT PRIMARY KEY,
                file_unique_id TEXT,
                tag TEXT,
                caption TEXT,
                chat_id INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        await db.commit()


async def save_user(user) -> None:
    """
    Сохраняет или обновляет пользователя.
    """
    if not user:
        return

    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user.id,
                user.username,
                user.first_name,
            ),
        )
        await db.commit()


async def add_message(
    chat_id: int,
    user_id: Optional[int],
    role: str,
    content: str,
) -> None:
    """
    Добавляет сообщение в историю чата.
    """
    content = (content or "")[:4000]

    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            INSERT INTO chat_history (chat_id, user_id, role, content)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, user_id, role, content),
        )
        await db.commit()


async def get_history(chat_id: int, limit: int = 12) -> List[Dict[str, str]]:
    """
    Возвращает последние сообщения из истории чата.
    """
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT role, content
            FROM chat_history
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )

        rows = await cursor.fetchall()

        return [
            {
                "role": row["role"],
                "content": row["content"],
            }
            for row in reversed(rows)
        ]


async def clear_history(chat_id: int) -> None:
    """
    Очищает историю конкретного чата.
    """
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            DELETE FROM chat_history
            WHERE chat_id = ?
            """,
            (chat_id,),
        )
        await db.commit()


async def add_gif(
    file_id: str,
    file_unique_id: Optional[str],
    tag: Optional[str],
    caption: Optional[str],
    chat_id: int,
) -> None:
    """
    Добавляет гифку в базу.
    Если такая гифка уже есть, обновляет тег и подпись.
    """
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            INSERT INTO gifs (
                file_id,
                file_unique_id,
                tag,
                caption,
                chat_id
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                tag = excluded.tag,
                caption = excluded.caption,
                chat_id = excluded.chat_id,
                added_at = CURRENT_TIMESTAMP
            """,
            (
                file_id,
                file_unique_id,
                tag,
                caption,
                chat_id,
            ),
        )
        await db.commit()


async def get_random_gif(tag: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Возвращает случайную гифку.
    Если tag указан, сначала ищет по тегу.
    Если не находит, возвращает любую случайную.
    """
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        if tag:
            cursor = await db.execute(
                """
                SELECT file_id, caption
                FROM gifs
                WHERE lower(tag) = lower(?)
                   OR lower(caption) LIKE lower(?)
                ORDER BY RANDOM()
                LIMIT 1
                """,
                (tag, f"%{tag}%"),
            )
            row = await cursor.fetchone()
            if row:
                return row["file_id"], row["caption"]
        cursor = await db.execute(
            """
            SELECT file_id, caption
            FROM gifs
            ORDER BY RANDOM()
            LIMIT 1
            """
        )
        row = await cursor.fetchone()
        if not row:
            return None, None
        return row["file_id"], row["caption"]


async def save_incoming_gif(
    file_id: str,
    file_unique_id: str,
    emoji: Optional[str] = None,
    chat_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> None:
    """
    Сохраняет гифку, которую прислали Альме.
    Emoji используется как тег.
    """
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            INSERT INTO gifs (
                file_id,
                file_unique_id,
                tag,
                caption,
                chat_id
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO NOTHING
            """,
            (
                file_id,
                file_unique_id,
                emoji,  # emoji как тег
                emoji,  # emoji как caption для простоты
                chat_id,
            ),
        )
        await db.commit()