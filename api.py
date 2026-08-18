# api.py
# Генерация ответа от Ollama Cloud.
# Поддерживает ротацию до 10 API-ключей: при ошибке/лимите на одном ключе
# автоматически пробуем следующий, по кругу.

import asyncio
import logging
import os

from ollama import Client

from prompt import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:cloud")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "https://ollama.com")


def _load_tokens() -> list[str]:
    """
    Читает ключи из переменной окружения OLLAMA_API_KEYS.
    В .env храните их через запятую, например:
    OLLAMA_API_KEYS=key1,key2,key3
    """
    raw = os.environ.get("OLLAMA_API_KEYS", "")
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        raise RuntimeError(
            "В .env не найдено ни одного ключа в OLLAMA_API_KEYS "
            "(ожидается список через запятую, до 10 штук)"
        )
    return tokens[:10]


class TokenRotator:
    """Простая круговая ротация токенов с блокировкой для конкурентных запросов."""

    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self._index = 0
        self._lock = asyncio.Lock()

    async def next_token(self) -> str:
        async with self._lock:
            token = self.tokens[self._index]
            self._index = (self._index + 1) % len(self.tokens)
            return token

    def __len__(self) -> int:
        return len(self.tokens)


_tokens = _load_tokens()
_rotator = TokenRotator(_tokens)


def _sync_chat(token: str, messages: list[dict]) -> tuple[str, int, int]:
    """
    Синхронный вызов Ollama (клиент ollama-python синхронный),
    выполняется в отдельном потоке через asyncio.to_thread.
    Возвращает (текст_ответа, prompt_tokens, completion_tokens).
    """
    client = Client(
        host=OLLAMA_HOST,
        headers={"Authorization": "Bearer " + token},
    )

    full_text = ""
    prompt_tokens = 0
    completion_tokens = 0

    for part in client.chat(OLLAMA_MODEL, messages=messages, stream=True):
        message = part.get("message", {}) or {}
        full_text += message.get("content", "")

        if part.get("done"):
            prompt_tokens = part.get("prompt_eval_count", 0) or 0
            completion_tokens = part.get("eval_count", 0) or 0

    return full_text, prompt_tokens, completion_tokens


async def generate_answer(history: list[dict], user_text: str) -> tuple[str, int, int]:
    """
    history   - предыдущие сообщения [{"role": "user"/"assistant", "content": "..."}]
    user_text - новое сообщение пользователя

    Возвращает (ответ_ИИ, prompt_tokens, completion_tokens).
    Перебирает токены по кругу, при ошибке на одном - пробует следующий.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    last_error: Exception | None = None

    for attempt in range(len(_rotator)):
        token = await _rotator.next_token()
        try:
            return await asyncio.to_thread(_sync_chat, token, messages)
        except Exception as e:
            last_error = e
            logger.warning("Ошибка с токеном #%s (попытка %s): %s", token[:8], attempt + 1, e)
            continue

    raise RuntimeError(f"Все токены исчерпаны или вернули ошибку. Последняя ошибка: {last_error}")