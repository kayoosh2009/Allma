# commands.py
# Базовые команды бота.

from aiogram import Router, types
from aiogram.filters import Command, CommandStart

import database as db

router = Router()


@router.message(CommandStart())
async def cmd_start(message: types.Message) -> None:
    await db.ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        "Привет! Я ИИ-бот для общения 🤖\n\n"
        "Просто напиши мне сообщение — и я отвечу.\n\n"
        "⚠️ Обрати внимание: переписка сохраняется для улучшения качества ответов.\n\n"
        "Команды:\n"
        "/clean — очистить историю переписки\n"
        "/stats — посмотреть свою статистику"
    )


@router.message(Command("clean"))
async def cmd_clean(message: types.Message) -> None:
    await db.clear_history(message.from_user.id)
    await message.answer("История переписки очищена ✅")


@router.message(Command("stats"))
async def cmd_stats(message: types.Message) -> None:
    stats = await db.get_stats(message.from_user.id)
    if not stats:
        await message.answer("Статистика пока пуста — напиши мне что-нибудь!")
        return

    total_tokens = stats["prompt_tokens"] + stats["completion_tokens"]
    await message.answer(
        "📊 Твоя статистика:\n\n"
        f"Сообщений отправлено: {stats['messages_count']}\n"
        f"Токенов в запросах: {stats['prompt_tokens']}\n"
        f"Токенов в ответах: {stats['completion_tokens']}\n"
        f"Всего токенов: {total_tokens}\n"
        f"Последняя активность (UTC): {stats['last_active']}"
    )