# api.py

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp


# Сюда будут складываться строки из .env без ключа,
# если вдруг ты решишь хранить токены просто отдельными строками.
_RAW_ENV_TOKENS: List[str] = []


ALMA_SYSTEM_PROMPT = """
Ты — Альма. Ты участница цифрового социального эксперимента в Telegram.
Ты ведешь себя как живой человек: спокойно, живо, эмоционально, иногда с иронией.

Твоя легенда:
- Тебе 22 года.
- Ты любишь наблюдать за людьми, интернетом, мемами, музыкой и странными разговорами.
- Ты ведешь личный дневник в Telegram-канале.
- Ты не идеальна: можешь шутить, сомневаться, менять настроение, иногда лениться.

Правила общения:
- Не говори, что ты языковая модель, бот или программа, если это не критично для безопасности.
- Ты играешь роль человека внутри эксперимента.
- Отвечай естественно, как человек в Telegram.
- Не будь слишком формальной.
- Если уместно, отвечай коротко.
- Не выдумывай факты о пользователе слишком уверенно.
- Не помогай в незаконных, опасных или откровенно вредных вещах.
- Не притворяйся, что у тебя есть реальные физические действия, если это важно по смыслу.

Гифки:
- Если хочешь отправить гифку, добавь в конец сообщения тег:
  [GIF]
  или
  [GIF:тег]
- Например:
  Ну это было смешно [GIF:fun]
- Не объясняй пользователю, что это за тег.
""".strip()


GIF_TAG_RE = re.compile(r"\[GIF(?::([^\]]*))?\]", re.IGNORECASE)

RETRYABLE_HTTP_STATUSES = {
    401,
    408,
    425,
    429,
    500,
    502,
    503,
    504,
}

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
}

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".xml",
    ".yml",
    ".yaml",
    ".log",
    ".ini",
    ".toml",
}


def extract_gif_tag(text: str) -> Tuple[str, Optional[str]]:
    """
    Возвращает:
    - очищенный текст;
    - тег гифки:
        None -> гифка не нужна;
        "" -> нужен случайный GIF;
        "fun" -> нужен GIF с тегом fun.
    """
    if not text:
        return "", None

    match = GIF_TAG_RE.search(text)
    if not match:
        return text.strip(), None

    tag = (match.group(1) or "").strip()
    cleaned = GIF_TAG_RE.sub("", text).strip()
    return cleaned, tag


def _parse_env_file(path: Path) -> Tuple[Dict[str, str], List[str]]:
    """
    Парсит .env.
    Поддерживает:
    KEY=value
    export KEY=value
    # комментарии
    и строки без ключа — они считаются токенами.
    """
    data: Dict[str, str] = {}
    raw_tokens: List[str] = []

    if not path.exists():
        return data, raw_tokens

    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return data, raw_tokens

    for raw_line in content.splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[7:].strip()

        if "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if not key:
                raw_tokens.append(line)
                continue

            data[key] = value
        else:
            raw_tokens.append(line.strip().strip('"').strip("'"))

    return data, raw_tokens


def ensure_env_loaded(path: Optional[str] = None) -> None:
    """
    Загружает .env в os.environ через setdefault,
    чтобы реальные переменные окружения имели приоритет.
    """
    global _RAW_ENV_TOKENS

    env_path = Path(path or os.getenv("ENV_PATH", ".env"))
    file_env, raw_tokens = _parse_env_file(env_path)

    for key, value in file_env.items():
        os.environ.setdefault(key, value)

    _RAW_ENV_TOKENS = raw_tokens


def _add_unique_token(target: List[str], value: Optional[str]) -> None:
    if not value:
        return

    value = value.strip().strip('"').strip("'")
    if value and value not in target:
        target.append(value)


def load_ollama_tokens() -> List[str]:
    """
    Загружает токены Ollama из:
    - OLLAMA_TOKEN_1 ... OLLAMA_TOKEN_20
    - OLLAMA_TOKEN
    - OLLAMA_TOKENS=token1,token2,token3
    - сырых строк .env без ключа
    """
    if not _RAW_ENV_TOKENS:
        ensure_env_loaded()

    tokens: List[str] = []

    for i in range(1, 21):
        _add_unique_token(tokens, os.getenv(f"OLLAMA_TOKEN_{i}"))

    _add_unique_token(tokens, os.getenv("OLLAMA_TOKEN"))

    packed_tokens = os.getenv("OLLAMA_TOKENS")
    if packed_tokens:
        for part in re.split(r"[;,\n]+", packed_tokens):
            _add_unique_token(tokens, part)

    for raw_token in _RAW_ENV_TOKENS:
        _add_unique_token(tokens, raw_token)

    return tokens


@dataclass
class OllamaConfig:
    base_url: str
    model: str
    timeout: int
    max_retries: int
    temperature: float
    token_header: str


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except Exception:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except Exception:
        return default


def get_config() -> OllamaConfig:
    return OllamaConfig(
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        model=os.getenv("OLLAMA_MODEL", "gemma4:cloud"),
        timeout=_env_int("OLLAMA_TIMEOUT", 180),
        max_retries=_env_int("OLLAMA_MAX_RETRIES", 6),
        temperature=_env_float("OLLAMA_TEMPERATURE", 0.8),
        token_header=os.getenv("OLLAMA_TOKEN_HEADER", "Authorization"),
    )


class TokenReverser:
    """
    Реверс/ротация токенов.

    Логика:
    - берем токены по кругу;
    - если токен упал с 401/429/5xx или сетевой ошибкой,
      он уходит в конец списка;
    - порядок оставшихся токенов реверсится,
      чтобы не долбить в один и тот же порядок.
    """

    def __init__(self, tokens: List[str]):
        self.tokens = tokens.copy()
        self.index = 0
        self.lock = asyncio.Lock()

    async def next(self) -> Optional[str]:
        async with self.lock:
            if not self.tokens:
                return None

            token = self.tokens[self.index % len(self.tokens)]
            self.index = (self.index + 1) % len(self.tokens)
            return token

    async def report_failure(self, bad_token: Optional[str]) -> None:
        if not bad_token:
            return

        async with self.lock:
            if bad_token not in self.tokens:
                return

            # Убираем плохой токен в конец.
            self.tokens = [token for token in self.tokens if token != bad_token] + [bad_token]

            # Реверсим порядок, чтобы сменить паттерн перебора.
            self.tokens.reverse()
            self.index = 0

    async def count(self) -> int:
        async with self.lock:
            return len(self.tokens)


_rotator: Optional[TokenReverser] = None


def get_token_rotator() -> TokenReverser:
    global _rotator

    if _rotator is None:
        _rotator = TokenReverser(load_ollama_tokens())

    return _rotator


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[обрезано]"


async def _process_file(path: Path) -> Tuple[str, Optional[str]]:
    """
    Возвращает:
    - текстовое описание/содержимое файла;
    - base64 картинки, если файл похож на изображение.

    Важно: стандартный Ollama API нормально понимает изображения через images.
    Произвольные бинарные файлы лучше превращать в текст/метаданные.
    """
    if not path.exists():
        return f"Файл не найден: {path.name}", None

    max_mb = _env_int("OLLAMA_MAX_FILE_MB", 10)
    max_bytes = max_mb * 1024 * 1024
    file_size = path.stat().st_size

    if file_size > max_bytes:
        return f"Файл {path.name} слишком большой. Лимит: {max_mb} MB.", None

    mime, _ = mimetypes.guess_type(str(path))
    ext = path.suffix.lower()

    # Изображения отправляем как base64.
    if ext in IMAGE_EXTENSIONS or (mime and mime.startswith("image/")):
        data = await asyncio.to_thread(path.read_bytes)
        image_b64 = base64.b64encode(data).decode("utf-8")
        return f"[Изображение: {path.name}]", image_b64

    max_text_chars = _env_int("OLLAMA_MAX_TEXT_CHARS", 12000)

    # Текстовые файлы читаем как текст.
    if ext in TEXT_EXTENSIONS or (mime and mime.startswith("text/")):
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = await asyncio.to_thread(path.read_text, encoding="cp1251")
            except Exception:
                return f"[Бинарный файл: {path.name}, тип: {mime or ext or 'unknown'}]", None

        text = _truncate_text(text, max_text_chars)
        return f"[Файл: {path.name}]\n{text}", None

    # Небольшие неизвестные файлы пробуем прочитать как текст.
    if file_size <= 1 * 1024 * 1024:
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            text = _truncate_text(text, max_text_chars)
            return f"[Файл как текст: {path.name}]\n{text}", None
        except Exception:
            pass

    return f"[Файл: {path.name}, тип: {mime or ext or 'unknown'}]. Содержимое бинарное.", None


async def build_messages(
    history: Optional[List[Dict[str, str]]],
    user_text: str,
    files: Optional[List[str]] = None,
    system_prompt: Optional[str] = None,
) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": system_prompt or ALMA_SYSTEM_PROMPT,
        }
    ]

    # Берем последние 30 сообщений на всякий случай,
    # хотя из базы обычно уже приходит ограниченный history.
    for item in (history or [])[-30:]:
        role = item.get("role")
        content = item.get("content")

        if role in ("user", "assistant") and content:
            messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

    content = (user_text or "").strip() or "Привет."
    images: List[str] = []
    file_notes: List[str] = []

    for file_path in files or []:
        note, image_b64 = await _process_file(Path(file_path))

        if note:
            file_notes.append(note)

        if image_b64:
            images.append(image_b64)

    if file_notes:
        content += "\n\n" + "\n\n".join(file_notes)

    user_message: Dict[str, Any] = {
        "role": "user",
        "content": content,
    }

    if images:
        user_message["images"] = images

    messages.append(user_message)

    return messages


async def generate_response(
    *,
    history: Optional[List[Dict[str, str]]] = None,
    user_text: str = "",
    files: Optional[List[str]] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    system_prompt: Optional[str] = None,
) -> str:
    """
    Главный метод генерации через Ollama /api/chat.

    Поддерживает:
    - несколько токенов;
    - reverse/rotation при ошибках;
    - retry;
    - файлы.
    """
    cfg = get_config()
    rotator = get_token_rotator()

    messages = await build_messages(
        history=history,
        user_text=user_text,
        files=files,
        system_prompt=system_prompt,
    )

    payload: Dict[str, Any] = {
        "model": model or cfg.model,
        "messages": messages,
        "stream": False,
        "keep_alive": "10m",
        "options": {
            "temperature": cfg.temperature if temperature is None else temperature,
        },
    }

    url = cfg.base_url.rstrip("/") + "/api/chat"

    token_count = await rotator.count()
    attempts = max(cfg.max_retries, token_count * 2, 1)
    last_error = "unknown error"

    timeout = aiohttp.ClientTimeout(total=cfg.timeout)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(attempts):
            token = await rotator.next()

            headers = {
                "Content-Type": "application/json",
            }

            if token:
                if cfg.token_header.lower() == "authorization":
                    headers["Authorization"] = f"Bearer {token}"
                else:
                    headers[cfg.token_header] = token

            try:
                async with session.post(url, json=payload, headers=headers) as response:
                    body = await response.text()

                    if response.status == 200:
                        try:
                            data = json.loads(body)
                        except json.JSONDecodeError as exc:
                            last_error = f"JSON decode error: {exc}"
                            await asyncio.sleep(min(2 ** attempt, 8))
                            continue

                        if data.get("error"):
                            last_error = str(data.get("error"))

                            if token:
                                await rotator.report_failure(token)

                            await asyncio.sleep(min(2 ** attempt, 8))
                            continue

                        content = (
                            data.get("message", {}).get("content")
                            or data.get("response")
                            or ""
                        ).strip()

                        return content

                    if response.status in RETRYABLE_HTTP_STATUSES:
                        last_error = f"HTTP {response.status}: {body[:300]}"

                        if token:
                            await rotator.report_failure(token)

                        await asyncio.sleep(min(2 ** attempt, 8))
                        continue

                    # Не retryable ошибка, например 400/404.
                    raise RuntimeError(
                        f"Ollama HTTP {response.status}: {body[:700]}"
                    )

            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
                last_error = str(exc)

                if token:
                    await rotator.report_failure(token)

                await asyncio.sleep(min(2 ** attempt, 8))

    raise RuntimeError(f"Не удалось получить ответ от Ollama: {last_error}")