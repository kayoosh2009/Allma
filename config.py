import os
from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val else default


BOT_TOKEN = os.environ["BOT_TOKEN"]

CHANNEL_ID = _int("CHANNEL_ID", 0)
REACTION_CHAT_ID = _int("REACTION_CHAT_ID", 0)

OLLAMA_API_KEY = os.environ["OLLAMA_API_KEY"]
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:cloud")

BOT_NAME = os.environ.get("BOT_NAME", "Бот")
BOT_PERSONA = os.environ.get(
    "BOT_PERSONA",
    "дружелюбный, живой человек, а не ассистент",
)

REACTION_PROB_MIN = _int("REACTION_PROB_MIN", 15)
REACTION_PROB_MAX = _int("REACTION_PROB_MAX", 60)

HISTORY_LIMIT = _int("HISTORY_LIMIT", 20)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
BACKUP_INTERVAL_HOURS = _int("BACKUP_INTERVAL_HOURS", 6)

MOOD_UPDATE_INTERVAL_HOURS = _int("MOOD_UPDATE_INTERVAL_HOURS", 3)

DB_PATH = os.environ.get("DB_PATH", "bot.db")
