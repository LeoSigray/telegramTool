"""
api/inbox_service.py — операции платформы диалогов, которым нужен живой
Telethon-клиент из пула: отправка ответа оператора и подкачка истории.

Ключевое правило отправки:
  • по умолчанию отвечаем С ТОГО ЖЕ аккаунта, с которого шла рассылка
    (conversation.account);
  • если он недоступен (не в пуле / отдыхает / выбыл) — берём любой
    активный аккаунт из пула, а диалог «переезжает» на него
    (record_outbound обновляет conversation.account).
"""
import logging

from telethon.errors import FloodWaitError, PeerFloodError, UserPrivacyRestrictedError

try:  # есть не во всех версиях telethon
    from telethon.errors import UserIsBlockedError
except ImportError:  # pragma: no cover
    class UserIsBlockedError(Exception):
        pass

from data import inbox
from optimizer import health
from .client_pool import pool

log = logging.getLogger(__name__)

HISTORY_LIMIT = 50


class InboxError(Exception):
    """Ожидаемая ошибка (нет аккаунтов, заблокировали, FloodWait) — уходит в 4xx/5xx."""


def _pick_account(preferred: str | None) -> tuple[str, object]:
    """(имя, клиент). preferred, если он в пуле и доступен; иначе любой активный."""
    if preferred and preferred in pool.clients and health.is_available(preferred):
        return preferred, pool.clients[preferred]
    for name in pool.list_active():
        if health.is_available(name):
            return name, pool.clients[name]
    # ни одного «здорового» — берём хоть какой-то подключённый
    if preferred and preferred in pool.clients:
        return preferred, pool.clients[preferred]
    for name, client in pool.clients.items():
        return name, client
    raise InboxError("нет подключённых аккаунтов в пуле")


async def _resolve_peer(client, conv: dict):
    """Пробуем достучаться до собеседника: сперва по user_id, потом по @username."""
    peer_id = conv["peer_id"]
    try:
        return await client.get_entity(int(peer_id))
    except (ValueError, TypeError):
        pass
    except Exception:  # noqa: BLE001 — аккаунт мог не знать этот id, пробуем username
        pass
    if conv.get("username"):
        try:
            return await client.get_entity("@" + conv["username"].lstrip("@"))
        except Exception:  # noqa: BLE001
            pass
    raise InboxError("этот аккаунт не может найти собеседника — "
                     "попробуйте, когда аккаунт рассылки снова будет онлайн")


async def send_reply(conv_id: int, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise InboxError("пустое сообщение")

    conv = inbox.get_conversation(conv_id)
    if not conv:
        raise InboxError("диалог не найден")

    account, client = _pick_account(conv["account"])
    entity = await _resolve_peer(client, conv)

    try:
        sent = await client.send_message(entity, text)
    except FloodWaitError as e:
        health.on_error(account, "flood_wait", f"inbox reply {e.seconds}s")
        inbox.record_outbound(conv_id, account=account, text=text, tg_id=None,
                              status="failed", error=f"FloodWait {e.seconds}s")
        raise InboxError(f"FloodWait на {account}: {e.seconds} c — сообщение не отправлено")
    except PeerFloodError:
        health.on_error(account, "peer_flood", "inbox reply")
        inbox.record_outbound(conv_id, account=account, text=text, tg_id=None,
                              status="failed", error="PeerFlood")
        raise InboxError(f"PeerFlood на {account} — сообщение не отправлено")
    except (UserIsBlockedError, UserPrivacyRestrictedError) as e:
        inbox.record_outbound(conv_id, account=account, text=text, tg_id=None,
                              status="failed", error=type(e).__name__)
        raise InboxError("собеседник заблокировал этот аккаунт или закрыл ЛС")
    except Exception as e:  # noqa: BLE001
        inbox.record_outbound(conv_id, account=account, text=text, tg_id=None,
                              status="failed", error=str(e))
        raise InboxError(f"не отправилось: {e}")

    mid = inbox.record_outbound(
        conv_id, account=account, text=text,
        tg_id=getattr(sent, "id", None),
        sent_at=(sent.date.isoformat() if getattr(sent, "date", None) else None),
        via="platform")
    return {
        "ok": True,
        "message_id": mid,
        "account": account,
        "fallback": account != conv["account"],
    }


async def hydrate(conv_id: int, limit: int = HISTORY_LIMIT) -> dict:
    """Тянет последние `limit` сообщений переписки из Telegram и мерджит в ленту."""
    conv = inbox.get_conversation(conv_id)
    if not conv:
        raise InboxError("диалог не найден")

    # для чтения истории аккаунт-владелец приоритетен, но сгодится любой,
    # кто реально видит собеседника
    tried: list[str] = []
    order = [conv["account"]] + [n for n in pool.list_active() if n != conv["account"]]
    for name in order:
        client = pool.clients.get(name)
        if client is None:
            continue
        tried.append(name)
        try:
            entity = await _resolve_peer(client, conv)
        except InboxError:
            continue
        try:
            tg_msgs = await client.get_messages(entity, limit=max(1, min(limit, 100)))
        except Exception as e:  # noqa: BLE001
            log.warning("[inbox] hydrate %s via %s: %s", conv_id, name, e)
            continue
        rows = []
        for m in tg_msgs:
            if getattr(m, "action", None) is not None:
                continue  # сервисные сообщения пропускаем
            rows.append({
                "tg_id": m.id,
                "direction": "out" if m.out else "in",
                "text": m.message or ("" if m.media is None else "[медиа]"),
                "sent_at": m.date.isoformat() if m.date else inbox._now(),
            })
        added = inbox.store_history(conv_id, rows)
        return {"ok": True, "via": name, "fetched": len(rows), "added": added}

    raise InboxError(f"не удалось подтянуть историю (проверенные аккаунты: {tried or '—'})")
