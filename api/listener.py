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

from data import analytics as an
from data import inbox
from .client_pool import pool

log = logging.getLogger(__name__)

WEBHOOK_URL = os.getenv("CRM_WEBHOOK_URL", "")
WEBHOOK_TOKEN = os.getenv("CRM_WEBHOOK_TOKEN", "")

# По умолчанию диалог в /inbox заводится только на ОТВЕТ на рассылку.
# INBOX_ACCEPT_ANY_DM=1 — заводить на ЛЮБОЕ входящее 1:1 (удобно для теста,
# и на случай, когда пишет тёплый контакт вне трекинга рассылки).
ACCEPT_ANY_DM = os.getenv("INBOX_ACCEPT_ANY_DM", "").strip().lower() in ("1", "true", "yes", "on")

_http: httpx.AsyncClient | None = None


def _which_session(client) -> str | None:
    for name, c in pool.clients.items():
        if c is client:
            return name
    return None


async def _on_incoming(event):
    # ВАЖНО: раньше тут был ранний return при пустом WEBHOOK_URL, из-за чего
    # без настроенного CRM аналитика (record_reply/reply-rate/бандит) вообще
    # не работала — учёт ответов и отправка в CRM теперь не связаны:
    # локальная аналитика пишется всегда, вебхук на CRM — только если задан.
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

    # Матчим ответ с нашей отправкой — это и есть источник reply-rate.
    # Заодно ловим отказы ("не пишите", "спам") и кладём человека в стоп-лист.
    send_id = None
    try:
        send_id = an.record_reply(peer_id=str(chat.id), text=msg.message,
                                  account=session_name)
    except Exception:  # noqa: BLE001 — аналитика не должна ломать приём сообщений
        log.exception("record_reply failed")

    # Платформа диалогов: чат заводим ТОЛЬКО когда человек ответил на рассылку
    # (send_id матчнулся) либо диалог с ним уже открыт. Дальше в него копятся
    # все сообщения — см. data/inbox.py.
    try:
        if (send_id is not None
                or ACCEPT_ANY_DM
                or inbox.find_conversation_by_peer(str(chat.id))
                or inbox.ever_sent_to_peer(str(chat.id))):
            inbox.record_inbound(
                str(chat.id), session_name,
                tg_id=msg.id, text=msg.message,
                sent_at=(msg.date.isoformat() if msg.date else None),
                username=chat.username, first_name=chat.first_name,
                last_name=chat.last_name, phone=chat.phone,
                send_id=send_id)
    except Exception:  # noqa: BLE001 — платформа диалогов не должна ронять приём
        log.exception("inbox.record_inbound failed")

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
        # send_id != None → это ответ на нашу рассылку. Верните его обратно
        # в POST /analytics/outcome, когда контакт станет лидом.
        "send_id": send_id,
    }

    if not WEBHOOK_URL:
        return  # CRM не настроен — аналитика выше уже записана, просто некуда постить

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


async def _post_webhook(path: str, payload: dict) -> None:
    if not WEBHOOK_URL:
        return
    base = WEBHOOK_URL.rsplit("/", 1)[0] if WEBHOOK_URL.endswith("/incoming") else WEBHOOK_URL
    full_url = base + path
    try:
        global _http
        if _http is None:
            _http = httpx.AsyncClient(timeout=10.0)
        headers = {"Authorization": f"Bearer {WEBHOOK_TOKEN}"} if WEBHOOK_TOKEN else {}
        r = await _http.post(full_url, json=payload, headers=headers)
        if r.status_code >= 400:
            log.warning("webhook %s -> %s %s", full_url, r.status_code, r.text[:200])
    except Exception:  # noqa: BLE001
        log.exception("webhook post failed")


async def _on_outgoing(event):
    """Исходящее, отправленное вручную из самого Telegram. Дописываем в уже
    открытый диалог — переписки, начатые не через платформу, она не ведёт."""
    session_name = _which_session(event.client)
    if session_name is None:
        return
    try:
        chat = await event.get_chat()
    except Exception:  # noqa: BLE001
        return
    if not isinstance(chat, TgUser) or chat.bot:
        return
    msg = event.message
    try:
        inbox.record_outbound_by_peer(
            str(chat.id), account=session_name, text=msg.message, tg_id=msg.id,
            sent_at=(msg.date.isoformat() if msg.date else None))
    except Exception:  # noqa: BLE001
        log.exception("inbox.record_outbound_by_peer failed")


async def _on_edited(event):
    session_name = _which_session(event.client)
    if session_name is None:
        return
    try:
        chat = await event.get_chat()
    except Exception:  # noqa: BLE001
        return
    if not isinstance(chat, TgUser) or chat.bot:
        return
    try:
        inbox.update_message_text(str(chat.id), event.message.id, event.message.message)
    except Exception:  # noqa: BLE001
        log.exception("inbox.update_message_text failed")
    await _post_webhook("/message-edited", {
        "account_name": session_name,
        "chat_id": str(chat.id),
        "message_id": str(event.message.id),
        "text": event.message.message or None,
    })


async def _on_deleted(event):
    session_name = _which_session(event.client)
    if session_name is None:
        return
    chat_id_val = event.chat_id
    # best-effort «удалил чат» → исход blocked для дашборда ответов.
    # Для 1:1 Telegram часто НЕ передаёт chat_id, а «удалить у себя» вообще
    # не долетает — поэтому сигнал неполный, ловим что можем.
    if chat_id_val is not None:
        try:
            an.record_chat_deleted(str(chat_id_val))
        except Exception:  # noqa: BLE001
            log.exception("record_chat_deleted failed")
    try:
        inbox.mark_messages_deleted(
            str(chat_id_val) if chat_id_val is not None else None,
            [int(i) for i in event.deleted_ids])
    except Exception:  # noqa: BLE001
        log.exception("inbox.mark_messages_deleted failed")
    await _post_webhook("/message-deleted", {
        "account_name": session_name,
        "chat_id": (str(chat_id_val) if chat_id_val is not None else None),
        "message_ids": [str(i) for i in event.deleted_ids],
    })


def setup() -> None:
    """Регистрирует listener в пуле. Зовётся ОДИН раз при старте."""
    pool.attach_handler(_on_incoming, events.NewMessage(incoming=True))
    pool.attach_handler(_on_outgoing, events.NewMessage(outgoing=True))
    pool.attach_handler(_on_edited, events.MessageEdited())
    pool.attach_handler(_on_deleted, events.MessageDeleted())
    if WEBHOOK_URL:
        log.info("[listener] webhook → %s (incoming, edited, deleted)", WEBHOOK_URL)
    else:
        log.warning("[listener] CRM_WEBHOOK_URL не задан — события только логируются")


async def shutdown() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None
