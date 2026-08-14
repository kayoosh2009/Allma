from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Dict, Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import BotCommand, Message

from api import ensure_env_loaded, extract_gif_tag, generate_response


ensure_env_loaded()


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "alma.db")

ADMIN_IDS = {
    int(x)
    for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",")
    if x.isdigit()
}

MEMORY_LIMIT = int(os.getenv("MEMORY_LIMIT", "12"))
GROUP_RANDOM_ANSWER_CHANCE = float(os.getenv("GROUP_RANDOM_ANSWER_CHANCE", "0.07"))

MAX_TELEGRAM_MESSAGE_LEN = 4000

router = Router()

# Локи по чатам, чтобы не обрабатывать параллельно один и тот же диалог.
CHAT_LOCKS: Dict[int, asyncio.Lock] = {}


# ----------------------------
# Database
# ----------------------------


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
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
    if not user:
        return

    async with aiosqlite.connect(DB_PATH) as db:
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
    content = (content or "")[:4000]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO chat_history (chat_id, user_id, role, content)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, user_id, role, content),
        )
        await db.commit()


async def get_history(chat_id: int, limit: int = 12) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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


async def get_random_gif(tag: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    async with aiosqlite.connect(DB_PATH) as db:
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


# ----------------------------
# Helpers
# ----------------------------


def is_admin(message: Message) -> bool:
    if not message.from_user:
        return False

    # Если ADMIN_IDS не заданы, разрешаем админские команды в личке.
    if not ADMIN_IDS:
        return message.chat.type == "private"

    return message.from_user.id in ADMIN_IDS


def get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in CHAT_LOCKS:
        CHAT_LOCKS[chat_id] = asyncio.Lock()
    return CHAT_LOCKS[chat_id]


async def typing_keeper(bot: Bot, chat_id: int) -> None:
    """
    Держит статус 'печатает...' во время генерации.
    """
    while True:
        with suppress(Exception):
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        await asyncio.sleep(4)


def should_answer(message: Message) -> bool:
    """
    В личке отвечаем всегда.
    В группах отвечаем, если:
    - ответили боту;
    - упомянули 'альма' / 'alma';
    - просто случайно повезло.
    """
    if message.chat.type == "private":
        return True

    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == message.bot.id
    ):
        return True

    text = (message.text or message.caption or "").lower()

    if not text:
        return False

    if "альма" in text or "alma" in text:
        return True

    return random.random() < GROUP_RANDOM_ANSWER_CHANCE


async def human_pause(text: str) -> None:
    """
    Небольшая задержка перед ответом, чтобы не отвечать мгновенно.
    """
    words = len(text.split())
    delay = min(0.8 + words * 0.1 + random.uniform(0.1, 0.8), 7.0)
    await asyncio.sleep(delay)


async def send_long_message(message: Message, text: str) -> None:
    text = text.strip()
    if not text:
        return

    for i in range(0, len(text), MAX_TELEGRAM_MESSAGE_LEN):
        await message.answer(text[i : i + MAX_TELEGRAM_MESSAGE_LEN])


async def download_message_file(message: Message) -> Optional[str]:
    """
    Скачивает файл из Telegram во временный файл.
    """
    file_id: Optional[str] = None
    suffix = ".bin"

    if message.photo:
        file_id = message.photo[-1].file_id
        suffix = ".jpg"

    elif message.animation:
        file_id = message.animation.file_id
        suffix = Path(message.animation.file_name or "animation.mp4").suffix or ".mp4"

    elif message.document:
        file_id = message.document.file_id
        suffix = Path(message.document.file_name or "document.bin").suffix or ".bin"

    elif message.video:
        file_id = message.video.file_id
        suffix = Path(message.video.file_name or "video.mp4").suffix or ".mp4"

    elif message.audio:
        file_id = message.audio.file_id
        suffix = Path(message.audio.file_name or "audio.mp3").suffix or ".mp3"

    elif message.voice:
        file_id = message.voice.file_id
        suffix = ".ogg"

    if not file_id:
        return None

    try:
        tg_file = await message.bot.get_file(file_id)

        buffer = io.BytesIO()
        await message.bot.download(tg_file, destination=buffer)

        data = buffer.getvalue()
        if not data:
            return None

        tmp = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=suffix,
            prefix="alma_",
        )

        tmp.write(data)
        tmp.close()

        return tmp.name

    except Exception as exc:
        logging.warning("Download error: %s", exc)
        return None


async def send_random_gif(message: Message, tag: Optional[str] = None) -> None:
    file_id, caption = await get_random_gif(tag)

    if not file_id:
        await message.answer(
            "У меня пока нет подходящих гифок. Добавь их через /add_gif."
        )
        return

    try:
        await message.answer_animation(
            animation=file_id,
            caption=caption or None,
        )
    except Exception:
        await message.answer("Не получилось отправить гифку.")


# ----------------------------
# Core processing
# ----------------------------


async def process_message(
    message: Message,
    text: str,
    file_path: Optional[str] = None,
) -> None:
    if not message.from_user:
        return

    chat_id = message.chat.id
    lock = get_lock(chat_id)

    async with lock:
        await save_user(message.from_user)

        # Получаем историю ДО сохранения текущего сообщения.
        history = await get_history(chat_id, MEMORY_LIMIT)

        display_text = (text or "").strip()

        if file_path:
            display_text = f"{display_text}\n[вложение: {Path(file_path).name}]".strip()

        if not display_text:
            display_text = "[пустое сообщение]"

        await add_message(
            chat_id=chat_id,
            user_id=message.from_user.id,
            role="user",
            content=display_text,
        )

        typing_task = asyncio.create_task(typing_keeper(message.bot, chat_id))

        try:
            # Небольшая пауза перед тем, как 'читать' сообщение.
            await asyncio.sleep(random.uniform(0.4, 1.4))

            try:
                answer = await generate_response(
                    history=history,
                    user_text=display_text,
                    files=[file_path] if file_path else [],
                )
            except Exception as exc:
                logging.exception("Generation error: %s", exc)
                answer = random.choice(
                    [
                        "Что-то я зависла... Попробуй еще раз.",
                        "Мне сейчас трудно собраться с мыслями. Повтори?",
                        "Хм, у меня что-то оборвалось. Напиши еще раз?",
                    ]
                )

            clean_text, gif_tag = extract_gif_tag(answer)

            if not clean_text and gif_tag is None:
                clean_text = "Хм... кажется, я потеряла мысль."

            await add_message(
                chat_id=chat_id,
                user_id=message.bot.id,
                role="assistant",
                content=clean_text or "[гифка]",
            )

            if clean_text:
                await human_pause(clean_text)

        finally:
            typing_task.cancel()

        if clean_text:
            await send_long_message(message, clean_text)

        if gif_tag is not None:
            await send_random_gif(message, gif_tag or None)

        if file_path:
            with suppress(Exception):
                Path(file_path).unlink(missing_ok=True)


# ----------------------------
# Handlers
# ----------------------------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not message.from_user:
        return

    await save_user(message.from_user)

    await message.answer(
        "Привет. Я Альма.\n"
        "Могу просто поболтать, посмотреть файлы, прислать гифку или написать что-нибудь в дневник.\n\n"
        "Команды:\n"
        "/gif — случайная гифка\n"
        "/gif тег — гифка по тегу\n"
        "/reset — очистить память этого чата\n"
        "/add_gif — добавить гифку, если ответить на нее"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Я отвечаю как человек, потому что это эксперимент.\n\n"
        "Можно писать мне текст, кидать файлы, фото, документы.\n"
        "Если хочешь гифку: /gif\n"
        "Если хочешь гифку по тегу: /gif fun\n\n"
        "Админ может добавить гифку:\n"
        "/add_gif тег\n"
        "Нужно ответить этой командой на гифку."
    )


@router.message(Command("gif"))
async def cmd_gif(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    tag = parts[1].strip() if len(parts) > 1 else None
    await send_random_gif(message, tag)


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    if message.chat.type != "private" and not is_admin(message):
        return

    await clear_history(message.chat.id)
    await message.answer("Память этого чата очищена.")


@router.message(Command("add_gif"))
async def cmd_add_gif(message: Message) -> None:
    if not is_admin(message):
        await message.answer("Это команда для администратора.")
        return

    reply = message.reply_to_message

    if not reply:
        await message.answer(
            "Ответь этой командой на гифку:\n"
            "/add_gif тег\n\n"
            "Например:\n"
            "/add_gif fun"
        )
        return

    animation = reply.animation
    document = reply.document

    if animation:
        file_id = animation.file_id
        unique_id = animation.file_unique_id

    elif document and (
        document.mime_type in {"image/gif", "video/mp4"}
        or (document.file_name or "").lower().endswith(".gif")
    ):
        file_id = document.file_id
        unique_id = document.file_unique_id

    else:
        await message.answer("Это не похоже на гифку.")
        return

    parts = (message.text or "").split(maxsplit=1)
    tag = parts[1].strip() if len(parts) > 1 else None
    caption = reply.caption or tag

    await add_gif(
        file_id=file_id,
        file_unique_id=unique_id,
        tag=tag,
        caption=caption,
        chat_id=message.chat.id,
    )

    await message.answer("Гифка сохранена.")


@router.message(F.photo | F.document | F.animation | F.video | F.audio | F.voice)
async def handle_media(message: Message) -> None:
    if not message.from_user:
        return

    if message.chat.type != "private" and not should_answer(message):
        return

    file_path = await download_message_file(message)
    caption = message.caption or ""

    if file_path is None and not caption:
        await message.answer("Не смогла прочитать файл или он слишком большой.")
        return

    await process_message(message, caption, file_path)


@router.message(F.text)
async def handle_text(message: Message) -> None:
    if not message.from_user:
        return

    if not should_answer(message):
        return

    await process_message(message, message.text or "")


# ----------------------------
# Bot startup
# ----------------------------


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начать общение"),
            BotCommand(command="help", description="Что я умею"),
            BotCommand(command="gif", description="Случайная гифка"),
            BotCommand(command="reset", description="Очистить память чата"),
            BotCommand(command="add_gif", description="Добавить гифку"),
        ]
    )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не найден. Добавь его в .env")

    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.include_router(router)

    await set_commands(bot)

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Альма остановлена.")