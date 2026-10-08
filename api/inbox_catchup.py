# -*- coding: utf-8 -*-
"""
«Догонялка» ответов: находит сообщения, пришедшие, пока аккаунт никто не слушал, и кладёт их в «Диалоги».

Telegram хранит всё, что пришло аккаунту, поэтому ничего не теряется: при подключении (и раз в полчаса)
просматриваем последние личные диалоги аккаунта, выбираем те, где последним написал ЧЕЛОВЕК, которому
писала наша рассылка, и добавляем в мессенджер. Повторный запуск дубликатов не создаёт.
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


def targets_we_wrote(account: str) -> dict[str, str]:
    """{peer_id или username: статус} из истории рассылки этого аккаунта."""
    out: dict[str, str] = {}
    try:
        import data.db as db
        c = sqlite3.connect(db.DB_PATH)
        for pid, tg, st in c.execute("SELECT peer_id, target, status FROM sends WHERE account=?", (account,)):
            if pid:
                out[str(pid)] = st
            if tg:
                out[str(tg).lstrip("@").lower()] = st
        c.close()
    except Exception:  # noqa: BLE001
        pass
    return out


async def collect_incoming(client, account: str, hours: float = 48, show_all: bool = False,
                           limit: int = 300) -> list[dict]:
    """Личные диалоги аккаунта за последние hours часов (по умолчанию — только где последним написал человек)."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    wrote = targets_we_wrote(account)
    out: list[dict] = []
    async for d in client.iter_dialogs(limit=limit):
        if not getattr(d, "is_user", False):
            continue
        ent, msg = d.entity, d.message
        if getattr(ent, "bot", False) or msg is None or msg.date is None or msg.date < since:
            continue
        incoming = not getattr(msg, "out", False)
        if not incoming and not show_all:
            continue
        uname = getattr(ent, "username", None)
        known = wrote.get(str(ent.id)) or (wrote.get((uname or "").lower()) if uname else None)
        out.append({
            "name": " ".join(x for x in (getattr(ent, "first_name", None), getattr(ent, "last_name", None)) if x) or "(без имени)",
            "username": uname, "id": ent.id, "date": msg.date,
            "text": (msg.text or msg.raw_text or "").replace(chr(10), " "),
            "unread": getattr(d, "unread_count", 0), "incoming": incoming, "from_broadcast": known,
            "msg_id": getattr(msg, "id", None), "first_name": getattr(ent, "first_name", None),
            "last_name": getattr(ent, "last_name", None), "phone": getattr(ent, "phone", None),
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def save_rows(account: str, rows: list[dict]) -> int:
    """Кладёт ответы людей из нашей рассылки в «Диалоги» и аналитику. Возвращает число НОВЫХ диалогов."""
    from data import analytics as an
    from data import inbox
    inbox.init_inbox()
    created = 0
    for r in rows:
        if not r["incoming"] or not r["from_broadcast"]:
            continue
        stamp = r["date"].isoformat()
        conv = inbox.find_conversation_by_peer(str(r["id"]))
        if conv and any(m["text"] == r["text"] and m["sent_at"] == stamp for m in inbox.get_messages(conv["id"])):
            continue                                  # это сообщение уже в мессенджере
        try:
            send_id = an.record_reply(peer_id=str(r["id"]), text=r["text"], account=account)
        except Exception:  # noqa: BLE001
            send_id = None
        _cid, new = inbox.record_inbound(
            str(r["id"]), account, tg_id=r.get("msg_id"), text=r["text"], sent_at=stamp,
            username=r["username"], first_name=r.get("first_name"), last_name=r.get("last_name"),
            phone=r.get("phone"), send_id=send_id)
        created += 1 if new else 0
    return created


async def import_history(client, account: str, days: float = 30, max_dialogs: int = 3000,
                         pause: float = 0.4) -> dict:
    """
    Загрузка СТАРЫХ ответов: все сообщения людей из нашей рассылки за последние days дней — даже если мы им
    потом ответили сами. (catch_up берёт только диалоги, где последним написал человек.)
    Диалоги идут от свежих к старым, поэтому обходим только то, что попало в период.
    Возвращает {"dialogs": сколько диалогов с нашими адресатами просмотрено, "messages": найдено входящих, "new": новых диалогов}.
    """
    import asyncio
    from telethon.errors import FloodWaitError
    since = datetime.now(timezone.utc) - timedelta(days=days)
    wrote = targets_we_wrote(account)
    rows: list[dict] = []
    seen = 0
    flood = None
    async for d in client.iter_dialogs(limit=max_dialogs):
        msg = d.message
        if msg is None or msg.date is None:
            continue
        if msg.date < since:
            if getattr(d, "pinned", False):
                continue                                   # закреплённые идут первыми независимо от даты
            break
        ent = d.entity
        if not getattr(d, "is_user", False) or getattr(ent, "bot", False):
            continue
        uname = getattr(ent, "username", None)
        known = wrote.get(str(ent.id)) or (wrote.get((uname or "").lower()) if uname else None)
        if not known:
            continue
        seen += 1
        try:
            async for m in client.iter_messages(ent, limit=40):
                if m.date is None or m.date < since:
                    break
                if getattr(m, "out", False):
                    continue
                rows.append({
                    "name": " ".join(x for x in (getattr(ent, "first_name", None), getattr(ent, "last_name", None)) if x) or "(без имени)",
                    "username": uname, "id": ent.id, "date": m.date,
                    "text": (m.text or m.raw_text or "").replace(chr(10), " "),
                    "unread": 0, "incoming": True, "from_broadcast": known,
                    "msg_id": getattr(m, "id", None), "first_name": getattr(ent, "first_name", None),
                    "last_name": getattr(ent, "last_name", None), "phone": getattr(ent, "phone", None),
                })
        except FloodWaitError as e:
            flood = int(getattr(e, "seconds", 0) or 0)
            if flood <= 60:
                await asyncio.sleep(flood + 1)
                flood = None
            else:
                break                                      # длинный лимит Telegram — остановимся, повторите позже
        await asyncio.sleep(pause)
    new = save_rows(account, rows)
    out = {"dialogs": seen, "messages": len(rows), "new": new}
    if flood:
        out["flood_wait_s"] = flood
    return out


async def catch_up(client, account: str, hours: float = 168) -> int:
    """Догоняет пропущенные ответы аккаунта за последние hours часов (по умолчанию неделя)."""
    try:
        rows = await collect_incoming(client, account, hours)
        n = save_rows(account, rows)
        if n:
            log.info("[catch-up] %s: добавлено ответов в «Диалоги»: %d", account, n)
        return n
    except Exception:  # noqa: BLE001 — догонялка не должна ломать работу аккаунта
        log.debug("[catch-up] %s не удалось", account, exc_info=True)
        return 0
