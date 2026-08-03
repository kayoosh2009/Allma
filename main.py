import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import database
import github_backup
from mood import random_walk_mood
from handlers.chat import router as chat_router
from handlers.channel import router as channel_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


async def scheduled_backup() -> None:
    try:
        await asyncio.to_thread(github_backup.backup_database_sync)
    except Exception:
        logger.exception("Ошибка при бэкапе базы данных в GitHub")


async def scheduled_mood_update() -> None:
    new_value = await random_walk_mood()
    logger.info("Настроение бота обновлено: %s", new_value)


async def main() -> None:
    await database.init_db()

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Хендлер канала должен идти раньше общего чата, но channel_post и message —
    # разные типы апдейтов, конфликтов не будет.
    dp.include_router(channel_router)
    dp.include_router(chat_router)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        scheduled_backup,
        "interval",
        hours=config.BACKUP_INTERVAL_HOURS,
        # первый запуск произойдёт через BACKUP_INTERVAL_HOURS после старта бота
    )
    scheduler.add_job(
        scheduled_mood_update,
        "interval",
        hours=config.MOOD_UPDATE_INTERVAL_HOURS,
    )
    scheduler.start()

    logger.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
