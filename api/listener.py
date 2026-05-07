"""
Слушает входящие сообщения на ВСЕХ подключённых аккаунтах из пула и
постит их в CRM-вебхук. Запускается из lifespan'а server.py.

env:
  CRM_WEBHOOK_URL    — куда постить (например http://127.0.0.1:8000/external/incoming)
  CRM_WEBHOOK_TOKEN  — общий секрет (Authorization: Bearer ...)
"""
import asyncio
import logging
import os

import httpx
from telethon import events
from telethon.tl.types import User as TgUser

from .client_pool import pool

log = logging.getLogger(__name__)

WEBHOOK_URL = os.getenv("CRM_WEBHOOK_URL", "")
WEBHOOK_TOKEN = os.getenv("CRM_WEBHOOK_TOKEN", "")

_http: httpx.AsyncClient | None = None


def _which_session(client) -> str | None:
    for name, c in pool.clients.items():
        if c is client:
            return name
    return None


async def _on_incoming(event):
    if not WEBHOOK_URL:
        return  # вебхук не настроен — listener молча работает в no-op

    session_name = _which_session(event.client)
    if session_name is None:
        return

    try:
        chat = await event.get_chat()
    except Exception:  # noqa: BLE001
        return

    # MVP: пропускаем боты, группы, каналы — только личные диалоги (1:1)
    if not isinstance(chat, TgUser) or chat.bot:
        return

    msg = event.message
    payload = {
        "account_name": session_name,
        "platform_user_id": str(chat.id),
        "username": chat.username,
        "display_name": ((chat.first_name or "") + (f" {chat.last_name}" if chat.last_name else "")) or None,
        "phone": chat.phone,
        "chat_id": str(chat.id),
        "message_id": str(msg.id),
        "text": msg.message or None,
        "from_me": False,
        "sent_at": (msg.date.isoformat() if msg.date else None),
    }

    try:
        global _http
        if _http is None:
            _http = httpx.AsyncClient(timeout=10.0)
        headers = {"Authorization": f"Bearer {WEBHOOK_TOKEN}"} if WEBHOOK_TOKEN else {}
        r = await _http.post(WEBHOOK_URL, json=payload, headers=headers)
        if r.status_code >= 400:
            log.warning("webhook %s -> %s %s", WEBHOOK_URL, r.status_code, r.text[:200])
    except Exception:  # noqa: BLE001
        log.exception("webhook post failed")


def setup() -> None:
    """Регистрирует listener в пуле. Зовётся ОДИН раз при старте."""
    pool.attach_handler(_on_incoming, events.NewMessage(incoming=True))
    if WEBHOOK_URL:
        log.info("[listener] webhook → %s", WEBHOOK_URL)
    else:
        log.warning("[listener] CRM_WEBHOOK_URL не задан — события только логируются")


async def shutdown() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None
