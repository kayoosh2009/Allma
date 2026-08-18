# app.py
# Точка входа: собирает все модули вместе и запускает polling.

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

import api
import database as db
import secure
from commands import router as commands_router

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в .env")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Команды подключаем первым роутером - они должны обрабатываться раньше общего чата
dp.include_router(commands_router)

chat_router = Router()


@chat_router.message()
async def handle_message(message: types.Message) -> None:
    if not message.text:
        return

    user = message.from_user
    await db.ensure_user(user.id, user.username, user.first_name)

    # Сохраняем сообщение пользователя и берём историю ДО него для контекста
    history = await db.get_history(user.id, limit=20)
    await db.add_message(user.id, "user", message.text)

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        answer, prompt_tokens, completion_tokens = await api.generate_answer(history, message.text)
    except Exception as e:
        logger.error("Ошибка генерации ответа для user_id=%s: %s", user.id, e)
        await message.answer("Извини, произошла ошибка при генерации ответа. Попробуй ещё раз чуть позже 🙏")
        return

    await db.add_message(user.id, "assistant", answer)
    await db.update_stats(user.id, prompt_tokens, completion_tokens)

    await message.answer(answer)

    # Пересылка в админ-канал не должна блокировать ответ пользователю
    asyncio.create_task(secure.log_conversation(bot, user, message.text, answer))


dp.include_router(chat_router)


async def main() -> None:
    await db.init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен, начинаю polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())