# app.py

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

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import BotCommand, Message

from api import ensure_env_loaded, extract_gif_tag, generate_response
from database import (
    init_db,
    save_user,
    add_message,
    get_history,
    clear_history,
)

# --- Буферизация сообщений (чтобы ждать, пока пользователь допечатает) ---
USER_BUFFERS: Dict[int, List[Tuple[str, Message]]] = {}
USER_TIMERS: Dict[int, asyncio.Task] = {}

async def handle_user_input(message: Message, text: str) -> None:
    """
    Кладет сообщение в буфер и запускает таймер.
    Если за 4 секунды придет еще сообщение, таймер сбросится.
    """
    chat_id = message.chat.id
    
    if chat_id not in USER_BUFFERS:
        USER_BUFFERS[chat_id] = []
        
    USER_BUFFERS[chat_id].append((text, message))
    
    if chat_id in USER_TIMERS:
        USER_TIMERS[chat_id].cancel()
        
    async def wait_and_process():
        try:
            await asyncio.sleep(4.0)  # Ждем 4 секунды
        except asyncio.CancelledError:
            return
            
        buffer = USER_BUFFERS.pop(chat_id, [])
        USER_TIMERS.pop(chat_id, None)
        
        if not buffer:
            return
            
        # Соединяем все сообщения в одно с пустой строкой между ними
        combined_text = "\n\n".join([item[0] for item in buffer])
        last_message = buffer[-1][1]  # Отвечаем на последнее сообщение
        
        await process_message(last_message, combined_text)
        
    task = asyncio.create_task(wait_and_process())
    USER_TIMERS[chat_id] = task

ensure_env_loaded()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {
    int(x)
    for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",")
    if x.isdigit()
}

MEMORY_LIMIT = int(os.getenv("MEMORY_LIMIT", "12"))
GROUP_RANDOM_ANSWER_CHANCE = float(os.getenv("GROUP_RANDOM_ANSWER_CHANCE", "0.07"))
MAX_TELEGRAM_MESSAGE_LEN = 4000

router = Router()
CHAT_LOCKS: Dict[int, asyncio.Lock] = {}


# ----------------------------
# Helpers
# ----------------------------

def is_admin(message: Message) -> bool:
    if not message.from_user:
        return False
    if not ADMIN_IDS:
        return message.chat.type == "private"
    return message.from_user.id in ADMIN_IDS


def get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in CHAT_LOCKS:
        CHAT_LOCKS[chat_id] = asyncio.Lock()
    return CHAT_LOCKS[chat_id]


async def typing_keeper(bot: Bot, chat_id: int) -> None:
    while True:
        with suppress(Exception):
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        await asyncio.sleep(4)


def should_answer(message: Message) -> bool:
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


async def human_delay() -> None:
    """
    Случайная задержка перед тем, как начать печатать (3-15 секунд).
    """
    delay = random.uniform(3.0, 15.0)
    await asyncio.sleep(delay)


async def typing_pause(text: str) -> None:
    """
    Задержка имитирующая набор текста.
    """
    words = len(text.split())
    delay = min(1.0 + words * 0.15 + random.uniform(0.5, 1.5), 8.0)
    await asyncio.sleep(delay)


async def send_long_message(message: Message, text: str) -> None:
    text = text.strip()
    if not text:
        return
    for i in range(0, len(text), MAX_TELEGRAM_MESSAGE_LEN):
        await message.answer(text[i : i + MAX_TELEGRAM_MESSAGE_LEN])


async def download_message_file(message: Message) -> Optional[str]:
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
        
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="alma_")
        tmp.write(data)
        tmp.close()
        return tmp.name
    except Exception as exc:
        logging.warning("Download error: %s", exc)
        return None


async def send_random_gif(message: Message, tag: Optional[str] = None) -> None:
    """
    Отправляет гифку, если она есть в базе.
    Если гифок нет — просто молча пропускаем этот шаг, чтобы не навязываться.
    """
    file_id, caption = await get_random_gif(tag)
    if not file_id:
        return  # Молча игнорируем, если база пуста
    
    try:
        await message.answer_animation(animation=file_id, caption=caption or None)
    except Exception:
        pass  # Молча игнорируем ошибки отправки (например, если гифка была удалена)


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

        # Шаг 1: Случайная задержка перед "чтением" сообщения (3-15 сек)
        await human_delay()
        
        # Шаг 2: ИСПРАВЛЕНО! Показываем "печатает..." через bot
        await message.bot.send_chat_action(chat_id, ChatAction.TYPING)
        
        typing_task = asyncio.create_task(typing_keeper(message.bot, chat_id))
        
        try:
            try:
                answer = await generate_response(
                    history=history,
                    user_text=display_text,
                    files=[file_path] if file_path else [],
                )
            except Exception as exc:
                logging.exception("Generation error: %s", exc)
                answer = random.choice([
                    "Что-то я зависла... Попробуй еще раз.",
                    "Мне сейчас трудно собраться с мыслями. Повтори?",
                    "Хм, у меня что-то оборвалось. Напиши еще раз?",
                ])

            # Сохраняем ответ в историю
            await add_message(
                chat_id=chat_id,
                user_id=message.bot.id,
                role="assistant",
                content=answer,
            )

            # Шаг 3: Небольшая пауза "набора текста" перед отправкой
            await typing_pause(answer)
            
        finally:
            typing_task.cancel()
            
        # Отправляем ответ, разбивая на части (если Альма захотела)
        await send_split_message(message, answer)

        if file_path:
            with suppress(Exception):
                Path(file_path).unlink(missing_ok=True)


async def send_split_message(message: Message, text: str) -> None:
    """
    Отправляет текст. Если Альма использовала разделитель '---',
    текст разбивается на несколько отдельных сообщений.
    """
    text = text.strip()
    if not text:
        return
        
    parts = [p.strip() for p in text.split('\n---\n') if p.strip()]
    if not parts:
        parts = [text]
        
    for i, part in enumerate(parts):
        if i > 0:
            # Имитируем паузу и набор текста между сообщениями
            await asyncio.sleep(random.uniform(1.0, 2.5))
            await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            
        await send_long_message(message, part)
        
# ----------------------------
# Handlers
# ----------------------------

@router.message(Command(commands=["start"]))
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

@router.message(F.animation)
async def handle_incoming_gif(message: Message) -> None:
    """
    Сохраняет все гифки, которые присылают Альме.
    """
    if not message.animation or not message.from_user:
        return
    
    # Получаем emoji из caption (если есть)
    emoji = message.caption.strip() if message.caption else None
    
    # Сохраняем гифку в базу
    await save_incoming_gif(
        file_id=message.animation.file_id,
        file_unique_id=message.animation.file_unique_id,
        emoji=emoji,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
    )
    
    # Если это не приватный чат и не упоминание - не отвечаем
    if message.chat.type != "private" and not should_answer(message):
        return
    
    # Обрабатываем как обычное сообщение
    caption = message.caption or ""
    await process_message(message, caption, None)


@router.message(F.photo | F.document | F.video | F.audio | F.voice)
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
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать общение"),
        BotCommand(command="help", description="Что я умею"),
        BotCommand(command="gif", description="Случайная гифка"),
        BotCommand(command="reset", description="Очистить память чата"),
        BotCommand(command="add_gif", description="Добавить гифку"),
    ])


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
    
    # Запускаем планировщик дневника
    from diary import diary_scheduler
    diary_task = asyncio.create_task(diary_scheduler(bot))
    
    try:
        await dp.start_polling(bot)
    finally:
        diary_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Альма остановлена.")