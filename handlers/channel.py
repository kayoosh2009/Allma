import asyncio
import random

from aiogram import Router, F
from aiogram.types import Message
from aiogram.enums import ChatAction

import config
import database
import mood as mood_module
import ollama_client
from personality import build_reaction_prompt

router = Router(name="channel")


@router.channel_post(F.chat.id == config.CHANNEL_ID, F.text | F.caption)
async def handle_channel_post(message: Message) -> None:
    post_text = message.text or message.caption or ""
    if not post_text.strip():
        return

    if await database.was_reacted(message.message_id):
        return

    current_mood = await database.get_mood()
    probability = mood_module.reaction_probability(
        current_mood, config.REACTION_PROB_MIN, config.REACTION_PROB_MAX
    )

    await database.mark_reacted(message.message_id)

    if random.random() > probability:
        return  # решил не реагировать на этот пост

    prompt = build_reaction_prompt(post_text, current_mood)

    await message.bot.send_chat_action(config.REACTION_CHAT_ID, ChatAction.TYPING)
    reaction_text = await ollama_client.generate_reply(system_prompt=prompt, history=[])

    # небольшая случайная задержка, чтобы не выглядело как мгновенный бот-ответ
    await asyncio.sleep(random.uniform(2, 12))

    await message.bot.send_message(config.REACTION_CHAT_ID, reaction_text)
    await database.add_message(config.REACTION_CHAT_ID, "assistant", reaction_text)
