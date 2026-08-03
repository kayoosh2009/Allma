import asyncio
import random

from aiogram import Router, F
from aiogram.types import Message
from aiogram.enums import ChatAction

import config
import database
import mood as mood_module
import ollama_client
from personality import build_chat_system_prompt

router = Router(name="chat")


@router.message(F.text, ~F.text.startswith("/"))
async def handle_message(message: Message) -> None:
    chat_id = message.chat.id
    user_text = message.text

    await database.add_message(chat_id, "user", user_text, user_id=message.from_user.id)

    history = await database.get_history(chat_id, limit=config.HISTORY_LIMIT)
    current_mood = await mood_module.random_walk_mood() if random.random() < 0.1 else await database.get_mood()
    system_prompt = build_chat_system_prompt(current_mood)

    # Имитация "человеческого" поведения: небольшая пауза + статус "печатает"
    await message.bot.send_chat_action(chat_id, ChatAction.TYPING)
    reply_text = await ollama_client.generate_reply(system_prompt, history)

    typing_delay = min(4.0, max(0.8, len(reply_text) / 40))
    await asyncio.sleep(typing_delay)

    await message.answer(reply_text)
    await database.add_message(chat_id, "assistant", reply_text)
