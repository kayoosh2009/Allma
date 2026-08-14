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
from typing import Dict, List, Tuple, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import BotCommand, Message

from api import ensure_env_loaded, generate_response
from database import init_db, save_user, add_message, get_history, clear_history

ensure_env_loaded()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
MEMORY_LIMIT = int(os.getenv("MEMORY_LIMIT", "12"))
GROUP_RANDOM_ANSWER_CHANCE = float(os.getenv("GROUP_RANDOM_ANSWER_CHANCE", "0.07"))
MAX_TELEGRAM_MESSAGE_LEN = 4000

router = Router()
CHAT_LOCKS: Dict[int, asyncio.Lock] = {}

# --- Буферизация сообщений (ждем, пока пользователь допечатает) ---
USER_BUFFERS: Dict[int, List[Tuple[str, Message]]] = {}
USER_TIMERS: Dict[int, asyncio.Task] = {}

async def handle_user_input(message: Message, text: str) -> None:
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
        if not buffer: return
            
        # Склеиваем сообщения через пустую строку
        combined_text = "\n\n".join([item[0] for item in buffer])
        last_message = buffer[-1][1]  # Отвечаем на последнее сообщение
        await process_message(last_message, combined_text)

    task = asyncio.create_task(wait_and_process())
    USER_TIMERS[chat_id] = task

# ----------------------------
# Helpers
# ----------------------------
def is_admin(message: Message) -> bool:
    if not message.from_user: return False
    if not ADMIN_IDS: return message.chat.type == "private"
    return message.from_user.id in ADMIN_IDS

def get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in CHAT_LOCKS: CHAT_LOCKS[chat_id] = asyncio.Lock()
    return CHAT_LOCKS[chat_id]

async def typing_keeper(bot: Bot, chat_id: int) -> None:
    while True:
        with suppress(Exception):
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        await asyncio.sleep(4)

def should_answer(message: Message) -> bool:
    if message.chat.type == "private": return True
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == message.bot.id: return True
    text = (message.text or message.caption or "").lower()
    if "альма" in text or "alma" in text: return True
    return random.random() < GROUP_RANDOM_ANSWER_CHANCE

async def human_delay() -> None:
    await asyncio.sleep(random.uniform(3.0, 15.0))

async def typing_pause(text: str) -> None:
    words = len(text.split())
    await asyncio.sleep(min(1.0 + words * 0.15 + random.uniform(0.5, 1.5), 8.0))

async def send_long_message(message: Message, text: str) -> None:
    text = text.strip()
    if not text: return
    for i in range(0, len(text), MAX_TELEGRAM_MESSAGE_LEN):
        await message.answer(text[i : i + MAX_TELEGRAM_MESSAGE_LEN])

async def download_message_file(message: Message) -> Optional[str]:
    file_id, suffix = None, ".bin"
    if message.photo: file_id, suffix = message.photo[-1].file_id, ".jpg"
    elif message.document: file_id, suffix = message.document.file_id, Path(message.document.file_name or "doc.bin").suffix or ".bin"
    elif message.video: file_id, suffix = message.video.file_id, ".mp4"
    elif message.audio: file_id, suffix = message.audio.file_id, ".mp3"
    elif message.voice: file_id, suffix = message.voice.file_id, ".ogg"
    if not file_id: return None

    try:
        tg_file = await message.bot.get_file(file_id)
        buffer = io.BytesIO()
        await message.bot.download(tg_file, destination=buffer)
        data = buffer.getvalue()
        if not data: return None
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="alma_")
        tmp.write(data); tmp.close()
        return tmp.name
    except Exception as exc:
        logging.warning("Download error: %s", exc)
        return None

# ----------------------------
# Core processing
# ----------------------------
async def process_message(message: Message, text: str, file_path: Optional[str] = None) -> None:
    if not message.from_user: return
    chat_id = message.chat.id
    lock = get_lock(chat_id)

    async with lock:
        await save_user(message.from_user)
        history = await get_history(chat_id, MEMORY_LIMIT)
        
        display_text = (text or "").strip()
        if file_path: display_text = f"{display_text}\n[вложение: {Path(file_path).name}]".strip()
        if not display_text: display_text = "[пустое сообщение]"

        await add_message(chat_id=chat_id, user_id=message.from_user.id, role="user", content=display_text)

        # Шаг 1: Задержка 3-15 сек
        await human_delay()
        
        # Шаг 2: Явно показываем "печатает..."
        await message.bot.send_chat_action(chat_id, ChatAction.TYPING)
        typing_task = asyncio.create_task(typing_keeper(message.bot, chat_id))
        
        try:
            try:
                answer = await generate_response(history=history, user_text=display_text, files=[file_path] if file_path else [])
            except Exception as exc:
                logging.exception("Generation error: %s", exc)
                answer = "Что-то я зависла... Попробуй еще раз."

            # Сохраняем ответ в историю
            await add_message(chat_id=chat_id, user_id=message.bot.id, role="assistant", content=answer)
            
            # Шаг 3: Пауза "набора текста"
            await typing_pause(answer)
        finally:
            typing_task.cancel()
            
        # Шаг 4: Разбиваем ответ на части, если Альма использовала '---'
        parts = [p.strip() for p in answer.split('\n---\n') if p.strip()]
        if not parts: parts = [answer]
        
        for i, part in enumerate(parts):
            if i > 0:
                await asyncio.sleep(random.uniform(1.0, 2.5))
                await message.bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(random.uniform(0.5, 1.5))
            await send_long_message(message, part)

        if file_path:
            with suppress(Exception): Path(file_path).unlink(missing_ok=True)

# ----------------------------
# Handlers
# ----------------------------
@router.message(Command(commands=["start"]))
async def cmd_start(message: Message) -> None:
    if not message.from_user: return
    await save_user(message.from_user)
    await message.answer("Привет. Я Альма.\nМогу просто поболтать, посмотреть файлы или написать что-нибудь в дневник.\n\n/reset — очистить память этого чата")

@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    if message.chat.type != "private" and not is_admin(message): return
    await clear_history(message.chat.id)
    await message.answer("Память этого чата очищена.")

@router.message(F.photo | F.document | F.video | F.audio | F.voice)
async def handle_media(message: Message) -> None:
    if not message.from_user: return
    if message.chat.type != "private" and not should_answer(message): return
    file_path = await download_message_file(message)
    caption = message.caption or ""
    if file_path is None and not caption:
        await message.answer("Не смогла прочитать файл.")
        return
    await process_message(message, caption, file_path)

@router.message(F.text)
async def handle_text(message: Message) -> None:
    if not message.from_user: return
    if not should_answer(message): return
    
    text = message.text or ""
    # Команды обрабатываем сразу, без буферизации
    if text.startswith('/'):
        await process_message(message, text)
        return

    # Обычный текст кладем в буфер (Альма подождет 4 сек)
    await handle_user_input(message, text)

# ----------------------------
# Bot startup
# ----------------------------
async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать общение"),
        BotCommand(command="reset", description="Очистить память чата"),
    ])

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    if not BOT_TOKEN: raise SystemExit("BOT_TOKEN не найден. Добавь его в .env")
    
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await set_commands(bot)
    
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