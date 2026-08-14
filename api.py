# api.py
from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ollama import AsyncClient

try:
    from ollama import ResponseError
except ImportError:
    class ResponseError(Exception):
        pass

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

Разбивка на сообщения:
- Люди в Telegram часто отправляют мысли несколькими сообщениями подряд.
- Если ты хочешь разделить свой ответ на несколько отдельных сообщений (например, сначала короткая реакция, а потом мысль), используй разделитель `---` на отдельной строке.
- Например:
  Ого, круто!
  ---
  Я как раз вчера об этом думала.
- Система автоматически отправит эти части как разные сообщения. Не пиши пользователю про этот разделитель.
""".strip()

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css", ".xml", ".yml", ".yaml", ".log", ".ini", ".toml"}

def _parse_env_file(path: Path) -> Tuple[Dict[str, str], List[str]]:
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
            data[key.strip()] = value.strip().strip('"').strip("'")
        else:
            raw_tokens.append(line.strip().strip('"').strip("'"))
    return data, raw_tokens

def ensure_env_loaded(path: Optional[str] = None) -> None:
    global _RAW_ENV_TOKENS
    env_path = Path(path or os.getenv("ENV_PATH", ".env"))
    file_env, raw_tokens = _parse_env_file(env_path)
    for key, value in file_env.items():
        os.environ.setdefault(key, value)
    _RAW_ENV_TOKENS = raw_tokens

def _add_unique_token(target: List[str], value: Optional[str]) -> None:
    if not value: return
    value = value.strip().strip('"').strip("'")
    if value and value not in target: target.append(value)

def load_ollama_tokens() -> List[str]:
    if not _RAW_ENV_TOKENS: ensure_env_loaded()
    tokens: List[str] = []
    for i in range(1, 21): _add_unique_token(tokens, os.getenv(f"OLLAMA_TOKEN_{i}"))
    _add_unique_token(tokens, os.getenv("OLLAMA_TOKEN"))
    packed_tokens = os.getenv("OLLAMA_TOKENS")
    if packed_tokens:
        for part in packed_tokens.replace(";", ",").split(","):
            _add_unique_token(tokens, part)
    for raw_token in _RAW_ENV_TOKENS: _add_unique_token(tokens, raw_token)
    return tokens

class TokenRotator:
    def __init__(self, tokens: List[str]):
        self.clients: List[AsyncClient] = []
        host = os.getenv("OLLAMA_HOST", "https://ollama.com")
        for token in tokens:
            self.clients.append(AsyncClient(host=host, headers={'Authorization': f'Bearer {token}'}))
        self.index = 0
        self.lock = asyncio.Lock()

    async def get_client(self) -> Optional[AsyncClient]:
        async with self.lock:
            if not self.clients: return None
            client = self.clients[self.index % len(self.clients)]
            self.index = (self.index + 1) % len(self.clients)
            return client

    async def report_failure(self, bad_client: AsyncClient) -> None:
        async with self.lock:
            if bad_client in self.clients:
                self.clients.remove(bad_client)
                self.clients.append(bad_client)
                self.clients.reverse()
                self.index = 0
                
    def count(self) -> int: return len(self.clients)

_rotator: Optional[TokenRotator] = None
def get_token_rotator() -> TokenRotator:
    global _rotator
    if _rotator is None: _rotator = TokenRotator(load_ollama_tokens())
    return _rotator

async def _process_file(path: Path) -> Tuple[str, Optional[str]]:
    if not path.exists(): return f"Файл не найден: {path.name}", None
    max_bytes = int(os.getenv("OLLAMA_MAX_FILE_MB", "10")) * 1024 * 1024
    if path.stat().st_size > max_bytes: return f"Файл {path.name} слишком большой.", None
    
    mime, _ = mimetypes.guess_type(str(path))
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS or (mime and mime.startswith("image/")):
        data = await asyncio.to_thread(path.read_bytes)
        return f"[Изображение: {path.name}]", base64.b64encode(data).decode("utf-8")
        
    if ext in TEXT_EXTENSIONS or (mime and mime.startswith("text/")):
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            return f"[Файл: {path.name}]\n{text[:12000]}", None
        except Exception:
            return f"[Бинарный файл: {path.name}]", None
    return f"[Файл: {path.name}]", None

async def build_messages(history, user_text, files=None, system_prompt=None):
    messages = [{"role": "system", "content": system_prompt or ALMA_SYSTEM_PROMPT}]
    for item in (history or [])[-30:]:
        if item.get("role") in ("user", "assistant") and item.get("content"):
            messages.append({"role": item["role"], "content": item["content"]})

    content = (user_text or "").strip() or "Привет."
    images, file_notes = [], []
    for file_path in files or []:
        note, image_b64 = await _process_file(Path(file_path))
        if note: file_notes.append(note)
        if image_b64: images.append(image_b64)
    if file_notes: content += "\n\n" + "\n\n".join(file_notes)

    user_message = {"role": "user", "content": content}
    if images: user_message["images"] = images
    messages.append(user_message)
    return messages

async def generate_response(*, history=None, user_text="", files=None, model=None, temperature=None, system_prompt=None) -> str:
    rotator = get_token_rotator()
    if rotator.count() == 0: raise RuntimeError("Нет доступных токенов Ollama.")
    
    messages = await build_messages(history=history, user_text=user_text, files=files, system_prompt=system_prompt)
    model_name = model or os.getenv("OLLAMA_MODEL", "gemma4:cloud")
    temp = temperature if temperature is not None else float(os.getenv("OLLAMA_TEMPERATURE", "0.8"))
    
    for attempt in range(max(rotator.count() * 2, 6)):
        client = await rotator.get_client()
        if not client: break
        try:
            response = await client.chat(model=model_name, messages=messages, stream=False, options={"temperature": temp}, keep_alive="10m")
            return response['message']['content'].strip()
        except Exception as e:
            await rotator.report_failure(client)
            await asyncio.sleep(min(2 ** attempt, 8))
            
    raise RuntimeError("Не удалось получить ответ от Ollama.")