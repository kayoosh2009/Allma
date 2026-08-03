import asyncio
import os

from ollama import Client

import config

_client = Client(
    host="https://ollama.com",
    headers={"Authorization": "Bearer " + config.OLLAMA_API_KEY},
)


def _chat_sync(messages: list[dict]) -> str:
    """Синхронный вызов Ollama Cloud (без стрима), выполняется в отдельном потоке."""
    response = _client.chat(config.OLLAMA_MODEL, messages=messages, stream=False)
    return response["message"]["content"]


async def generate_reply(system_prompt: str, history: list[dict], user_text: str | None = None) -> str:
    """
    system_prompt: описание личности бота
    history: список {'role': 'user'/'assistant', 'content': str} — предыдущий контекст
    user_text: новое сообщение пользователя (если есть отдельно от history)
    """
    messages = [{"role": "system", "content": system_prompt}] + history
    if user_text is not None:
        messages.append({"role": "user", "content": user_text})

    # ollama-python клиент синхронный — не блокируем event loop aiogram
    text = await asyncio.to_thread(_chat_sync, messages)
    return text.strip()
