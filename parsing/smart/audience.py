"""Аудитория клиента: исключаем тех, кто уже состоит в его группе или канале.

Два уровня проверки:
  1. Список аудитории (кешируется в SQLite на сутки):
     • участники группы обсуждения канала клиента (сколько отдаст Telegram);
     • подписчики самого канала — только если читающий аккаунт там админ;
     • авторы последних комментариев в обсуждении.
  2. Точечная проверка верхушки списка: GetParticipant по группе обсуждения.
     Ловит тех, кого Telegram не показал в общем списке (большие группы
     отдают не всех участников).
"""
from __future__ import annotations

import asyncio
import random

REASON = "уже в аудитории клиента"


async def _resolve(client, params, log):
    """(канал клиента, группа обсуждения или None)."""
    from telethon.tl.functions.channels import GetFullChannelRequest

    own = await client.get_entity(params.channel)
    full = await client(GetFullChannelRequest(own))
    linked_id = getattr(full.full_chat, "linked_chat_id", None)
    group = None
    if linked_id:
        group = next((c for c in getattr(full, "chats", []) or [] if c.id == linked_id), None)
        if group is None:
            try:
                group = await client.get_entity(linked_id)
            except Exception as e:  # noqa: BLE001
                log.warn("аудитория", f"обсуждение канала клиента недоступно: {str(e)[:100]}")
    return own, group


async def _participants(client, ent, limit: int, what: str, log) -> set:
    ids: set = set()
    try:
        async for u in client.iter_participants(ent, limit=limit):
            ids.add(u.id)
        log.info("аудитория", f"{what}: {len(ids)}")
    except Exception as e:  # noqa: BLE001 — нужны права админа / список скрыт
        log.info("аудитория", f"{what}: недоступно ({str(e)[:90]})")
    return ids


async def load_audience(client, params, store, log):
    """Возвращает (множество user_id аудитории клиента, группа обсуждения или None)."""
    cached, fresh = store.load_audience(params.channel, max_age_hours=24)
    if client is None:
        log.info("аудитория", f"офлайн: из кеша {len(cached)} человек")
        return cached, None
    try:
        own, group = await _resolve(client, params, log)
    except Exception as e:  # noqa: BLE001
        log.warn("аудитория", f"канал клиента недоступен ({str(e)[:100]}), "
                             f"проверка только по кешу ({len(cached)})")
        return cached, None
    if fresh:
        log.info("аудитория", f"из кеша (моложе суток): {len(cached)} человек")
        return cached, group

    ids: set = set()
    ids |= await _participants(client, own, params.audience_members, "подписчики канала", log)
    if group is not None:
        ids |= await _participants(client, group, params.audience_members,
                                   "участники обсуждения", log)
        n_before = len(ids)
        try:
            async for m in client.iter_messages(group, limit=params.audience_msgs,
                                                wait_time=params.wait_time):
                if getattr(m, "sender_id", None) and m.sender_id > 0:
                    ids.add(m.sender_id)
            log.info("аудитория", f"авторы комментариев: +{len(ids) - n_before}")
        except Exception as e:  # noqa: BLE001
            log.info("аудитория", f"комментарии обсуждения недоступны ({str(e)[:90]})")
    store.save_audience(params.channel, ids)
    log.info("аудитория", f"всего в аудитории клиента: {len(ids)}")
    return ids, group


async def is_member(client, group, user):
    """True/False — состоит ли человек в группе; None — проверить нельзя."""
    from telethon.errors import UserNotParticipantError
    from telethon.tl.functions.channels import GetParticipantRequest

    try:
        participant = user
        if not isinstance(user, int):
            from telethon.tl.types import InputPeerUser
            participant = InputPeerUser(user.user_id, user.access_hash)
        await client(GetParticipantRequest(channel=group, participant=participant))
        return True
    except UserNotParticipantError:
        return False
    except Exception:  # noqa: BLE001 — нет access_hash, скрытые участники и т.п.
        return None


async def exclude_audience(client, people: list, audience: set, group, params, store, log,
                           not_members: set | None = None) -> list:
    """Убирает аудиторию клиента. Возвращает (оставшиеся, исключённые с .drop)."""
    from telethon.errors import FloodWaitError

    kept, dropped, checked, unknown = [], [], 0, 0
    stop_checks = client is None or group is None
    for n, c in enumerate(people):
        uid = c.msg.sender_id
        member = uid in audience
        known_ok = not_members is not None and uid in not_members
        if not member and not known_ok and not stop_checks and n < params.audience_check_top:
            try:
                who = c.author if (c.author is not None and c.author.access_hash) else uid
                res = await is_member(client, group, who)
            except FloodWaitError as e:
                log.warn("аудитория", f"FloodWait {e.seconds} с — точечная проверка остановлена")
                stop_checks, res = True, None
            checked += 1
            if res is None:
                unknown += 1
            elif res:
                member = True
                store.add_audience(params.channel, uid)
            elif not_members is not None:
                not_members.add(uid)
            await asyncio.sleep(random.uniform(0.5, 1.5))
        if member:
            c.drop = REASON
            dropped.append(c)
        else:
            kept.append(c)
    msg = f"исключено {len(dropped)} из {len(people)}"
    if checked:
        msg += f"; точечно проверено {checked}" + (f" (не удалось: {unknown})" if unknown else "")
    log.info("аудитория", msg)
    log.step("Исключено: уже в аудитории клиента", len(dropped))
    return kept, dropped
