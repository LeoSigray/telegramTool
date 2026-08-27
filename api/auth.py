"""
Простая bearer-token авторизация.

Токен берём из env API_TOKEN, если задан явно. Если нет — генерируем
один раз и сохраняем в ~/.telegramtool/token (права 0600). Это НЕ отключение
авторизации — токен по-прежнему обязателен на каждый запрос (см. why в
api/server.py про открытый CORS: без токена любой сайт в браузере на этой
же машине мог бы дёрнуть /broadcast/dm/start). Смысл — убрать ручной ввод:
десктоп-приложение читает тот же файл напрямую с диска (см. main.rs) и
подключается само, без участия человека.

Файл переживает перезапуски бэкенда (тот же токен), но не коммитится и не
расшаривается — это секрет, специфичный для конкретной машины/установки.
"""
import os
import secrets
from pathlib import Path

from fastapi import Header, HTTPException, status

TOKEN_FILE = Path.home() / ".telegramtool" / "token"


def _load_or_create_token() -> str:
    env = os.getenv("API_TOKEN", "").strip()
    if env:
        return env

    if TOKEN_FILE.exists():
        existing = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    token = secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        TOKEN_FILE.chmod(0o600)  # только владелец — на Windows это no-op, но не падает
    except Exception:
        pass
    return token


API_TOKEN = _load_or_create_token()


def require_token(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {API_TOKEN}"
    if not secrets.compare_digest(authorization or "", expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")
