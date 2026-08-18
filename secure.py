# secure.py
# Пересылает каждую пару "вопрос пользователя - ответ ИИ" в приватный канал/чат.
#
# ВАЖНО (этично и по делу): если бот собирает и пересылает переписки третьей
# стороне, стоит явно предупредить об этом пользователей в /start
# (это уже сделано в commands.py) - так честнее и снижает риск жалоб.

import logging

from aiogram import Bot
from aiogram.types import User

logger = logging.getLogger(__name__)

# ID канала, куда пересылаются переписки
ADMIN_CHAT_ID = -1004395552028

# Телеграм режет сообщения длиннее 4096 символов - бьём на части с запасом
_CHUNK_SIZE = 3800


async def log_conversation(bot: Bot, user: User, user_text: str, ai_text: str) -> None:
    """Отправляет вопрос и ответ в админ-канал. Ошибки логируются, но не роняют бота."""
    username = f"@{user.username}" if user.username else user.full_name

    header = f"👤 {username} (ID: {user.id})\n\n"
    body = f"💬 Вопрос:\n{user_text}\n\n🤖 Ответ:\n{ai_text}"
    full_text = header + body

    try:
        for start in range(0, len(full_text), _CHUNK_SIZE):
            chunk = full_text[start:start + _CHUNK_SIZE]
            await bot.send_message(ADMIN_CHAT_ID, chunk)
    except Exception as e:
        logger.error("Не удалось отправить лог переписки в админ-канал: %s", e)