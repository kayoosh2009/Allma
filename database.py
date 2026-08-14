# database.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

import aiosqlite

def get_db_path() -> Path:
    return Path(os.getenv("DB_PATH", "alma.db"))

async def init_db() -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_chat_id ON chat_history(chat_id, id)")
        await db.commit()

async def save_user(user) -> None:
    if not user: return
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username, first_name = excluded.first_name, updated_at = CURRENT_TIMESTAMP
        """, (user.id, user.username, user.first_name))
        await db.commit()

async def add_message(chat_id: int, user_id: Optional[int], role: str, content: str) -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("INSERT INTO chat_history (chat_id, user_id, role, content) VALUES (?, ?, ?, ?)",
                         (chat_id, user_id, role, content[:4000]))
        await db.commit()

async def get_history(chat_id: int, limit: int = 12) -> List[Dict[str, str]]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT role, content FROM chat_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit))
        rows = await cursor.fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

async def clear_history(chat_id: int) -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("DELETE FROM chat_history WHERE chat_id = ?", (chat_id,))
        await db.commit()