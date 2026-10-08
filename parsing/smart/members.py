"""Участники чатов ниши — данные для пересечений «кто есть в нескольких чатах».

В чаты НЕ вступаем: Telegram отдаёт список участников публичной группы и без вступления,
если админы его не скрыли. Скрытый список не увидит и вступивший (только админы), поэтому
вступление ничего не дало бы, а личному аккаунту добавило бы лимитов и риска.
Для канала берём участников его группы обсуждения. Списки кешируются (members_ttl_days)
и удаляются по сроку хранения вместе с сообщениями.
"""
from __future__ import annotations

import asyncio
import random

from .harvest import author_from_user


async def _members_entity(client, src):
    """Сущность, у которой берём участников: сама группа или обсуждение канала."""
    from telethon.tl.functions.channels import GetFullChannelRequest

    ent = await client.get_entity(src.username)
    if src.kind == "group":
        return ent
    full = await client(GetFullChannelRequest(ent))
    linked = getattr(full.full_chat, "linked_chat_id", None)
    if not linked:
        return None
    return next((c for c in getattr(full, "chats", []) or [] if c.id == linked), None)


async def collect_members(client, sources: list, store, params, log) -> dict:
    """Списки участников лучших чатов ниши. sources — по убыванию важности.
    Возвращает chat_id → множество user_id (из кеша и свежезагруженные)."""
    from telethon.errors import FloodWaitError

    chosen = [s for s in sources if s.username][:params.members_chats]
    fetched = cached = failed = hidden = 0
    for src in chosen:
        if store.members_fresh(src.chat_id, params.members_ttl_days):
            cached += 1
            continue
        if client is None:
            continue
        try:
            ent = await _members_entity(client, src)
            if ent is None:
                failed += 1
                log.info("пересечения", f"{src.label()}: нет группы обсуждения — участников не взять")
                continue
            ids, users = set(), []
            async for u in client.iter_participants(ent, limit=params.members_limit):
                if getattr(u, "bot", False) or getattr(u, "deleted", False):
                    continue
                ids.add(u.id)
                users.append(author_from_user(u))
            store.upsert_authors(users)
            store.save_members(src.chat_id, ids)
            fetched += 1
            if src.members and len(ids) < min(50, 0.05 * src.members):
                hidden += 1
                log.info("пересечения", f"{src.label()}: отдано только {len(ids)} из ~{src.members} — "
                                        "похоже, список участников скрыт админами")
        except FloodWaitError as e:
            if e.seconds > params.max_flood_wait:
                log.warn("пересечения", f"FloodWait {e.seconds} с — сбор участников остановлен")
                break
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:  # noqa: BLE001 — нужны права админа, список скрыт и т.п.
            failed += 1
            log.info("пересечения", f"{src.label()}: участники недоступны ({str(e)[:90]})")
        await asyncio.sleep(random.uniform(2.0, 4.0))
    members = store.members([s.chat_id for s in sources])
    people = set().union(*members.values()) if members else set()
    log.info("пересечения", f"списки участников: загружено {fetched}, из кеша {cached}, "
                            f"недоступно {failed}, скрыто {hidden}; людей в списках {len(people)}")
    log.step("Чатов со списком участников", len(members))
    return members
