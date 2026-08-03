import logging
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone

import config

logger = logging.getLogger(__name__)

_REPO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_gh_backup_repo")


def _run_git(*args: str, cwd: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error("git %s failed: %s", args, result.stderr)
        raise RuntimeError(f"git {args} failed: {result.stderr}")


def _remote_url() -> str:
    return f"https://{config.GITHUB_TOKEN}@github.com/{config.GITHUB_REPO}.git"


def _ensure_repo_cloned() -> None:
    if os.path.isdir(os.path.join(_REPO_DIR, ".git")):
        return
    if os.path.isdir(_REPO_DIR):
        shutil.rmtree(_REPO_DIR)
    subprocess.run(
        ["git", "clone", "--branch", config.GITHUB_BRANCH, _remote_url(), _REPO_DIR],
        check=True,
        capture_output=True,
        text=True,
    )


def _safe_snapshot(dest_path: str) -> None:
    """Копирует БД через sqlite backup API — безопасно даже пока бот пишет в неё."""
    src = sqlite3.connect(config.DB_PATH)
    dst = sqlite3.connect(dest_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def backup_database_sync() -> None:
    """Синхронная функция: удаляет старый файл БД в репо, кладёт новый снапшот, коммитит и пушит."""
    if not config.GITHUB_TOKEN or not config.GITHUB_REPO:
        logger.warning("GITHUB_TOKEN/GITHUB_REPO не заданы — бэкап пропущен")
        return

    _ensure_repo_cloned()

    # На случай, если кто-то запушил изменения в репозиторий вручную
    _run_git("fetch", "origin", cwd=_REPO_DIR)
    _run_git("reset", "--hard", f"origin/{config.GITHUB_BRANCH}", cwd=_REPO_DIR)

    db_filename = os.path.basename(config.DB_PATH)
    repo_db_path = os.path.join(_REPO_DIR, db_filename)

    # Удаляем старую версию файла, если есть
    if os.path.exists(repo_db_path):
        os.remove(repo_db_path)
        _run_git("add", "-A", cwd=_REPO_DIR)

    # Кладём свежий снапшот
    _safe_snapshot(repo_db_path)

    _run_git("add", "-A", cwd=_REPO_DIR)

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=_REPO_DIR, capture_output=True, text=True
    )
    if not status.stdout.strip():
        logger.info("Бэкап: изменений нет, коммит не нужен")
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    _run_git("commit", "-m", f"Backup {timestamp}", cwd=_REPO_DIR)
    _run_git("push", "origin", config.GITHUB_BRANCH, cwd=_REPO_DIR)
    logger.info("Бэкап базы данных успешно запушен в GitHub (%s)", timestamp)
