# diary.py

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta

from aiogram import Bot

from api import ensure_env_loaded, generate_response
from database import get_history, add_message

ensure_env_loaded()

CHANNEL_ID = os.getenv("ALMA_CHANNEL_ID", "").strip()
POST_INTERVAL_MIN = int(os.getenv("POST_INTERVAL_MIN", "120"))  # минут
POST_INTERVAL_MAX = int(os.getenv("POST_INTERVAL_MAX", "360"))  # минут

logger = logging.getLogger(__name__)


async def generate_diary_entry(bot: Bot) -> str:
    """
    Генерирует запись для дневника.
    """
    system_prompt = """
Ты — Альма. Ты ведешь личный дневник в Telegram-канале.
Напиши короткую запись (2-5 предложений) о своих мыслях, наблюдениях или эмоциях.
Будь естественной, живой, иногда с иронией.
Не упоминай, что ты ИИ или бот.
Пиши от первого лица.
""".strip()
    
    try:
        entry = await generate_response(
            history=[],
            user_text="Напиши запись в свой дневник. О чем думаешь сейчас?",
            system_prompt=system_prompt,
        )
        return entry
    except Exception as e:
        logger.error(f"Error generating diary entry: {e}")
        return random.choice([
            "Сегодня был странный день...",
            "Иногда мне кажется, что я наблюдаю за миром со стороны.",
            "Записываю свои мысли, пока они не исчезли.",
        ])


async def post_to_channel(bot: Bot, text: str) -> bool:
    """
    Публикует пост в канале.
    """
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID не задан. Пропускаю пост.")
        return False
    
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
        )
        logger.info(f"Posted to channel: {text[:50]}...")
        return True
    except Exception as e:
        logger.error(f"Failed to post to channel: {e}")
        return False


async def diary_scheduler(bot: Bot) -> None:
    """
    Планировщик постов в дневник.
    """
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID не задан. Дневник отключен.")
        return
    
    logger.info("Diary scheduler started.")
    
    while True:
        # Случайный интервал между постами
        interval_minutes = random.randint(POST_INTERVAL_MIN, POST_INTERVAL_MAX)
        interval_seconds = interval_minutes * 60
        
        logger.info(f"Next diary post in {interval_minutes} minutes.")
        await asyncio.sleep(interval_seconds)
        
        # Генерируем и публикуем запись
        entry = await generate_diary_entry(bot)
        success = await post_to_channel(bot, entry)
        
        if success:
            # Сохраняем в историю (для контекста)
            await add_message(
                chat_id=-int(CHANNEL_ID.replace("-", "")) if CHANNEL_ID.startswith("-") else int(CHANNEL_ID),
                user_id=None,
                role="assistant",
                content=f"[Diary post]: {entry}",
            )