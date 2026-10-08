"""Обогащение авторов: био из профиля (для признака ЛПР).

Запрашиваем только для верхушки списка (enrich_top), с паузами. Результат
кешируется в SQLite на 7 дней, повторный запуск запросов не делает.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from .store import parse_iso

ABOUT_TTL_DAYS = 7


def _fresh(a) -> bool:
    if a.about is None or not a.about_at:
        return False
    try:
        return datetime.now(timezone.utc) - parse_iso(a.about_at) < timedelta(days=ABOUT_TTL_DAYS)
    except ValueError:
        return False


async def refresh_access(client, people: list, params, store, log) -> int:
    """Ключ доступа к профилю для тех, у кого его нет (собраны до сохранения ключей):
    перечитываем их сообщение — Telegram вместе с ним отдаёт автора."""
    from telethon.tl.types import User

    from .harvest import author_from_user
    todo = [c for c in people[:params.enrich_top]
            if c.author is not None and not c.author.access_hash and c.source.username]
    got = 0
    for c in todo:
        try:
            if c.msg.post_id:
                msgs = [m async for m in client.iter_messages(
                    c.source.username, reply_to=c.msg.post_id,
                    min_id=c.msg.msg_id - 1, max_id=c.msg.msg_id + 1)]
            else:
                msgs = await client.get_messages(c.source.username, ids=[c.msg.msg_id])
            for m in msgs or []:
                u = getattr(m, "sender", None) if m else None
                if isinstance(u, User) and u.id == c.author.user_id:
                    fresh = author_from_user(u)
                    fresh.about, fresh.about_at = c.author.about, c.author.about_at
                    c.author.access_hash, c.author.status = fresh.access_hash, fresh.status
                    c.author.was_online, c.author.premium = fresh.was_online, fresh.premium
                    store.upsert_authors([c.author])
                    got += 1
        except Exception as e:  # noqa: BLE001
            log.info("авторы", f"id {c.author.user_id}: сообщение не перечитать ({str(e)[:60]})")
        await asyncio.sleep(random.uniform(0.8, 1.6))
    if todo:
        log.info("авторы", f"ключ доступа к профилю восстановлен: {got} из {len(todo)}")
    return got


async def enrich_authors(client, authors: list, params, store, log) -> int:
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.users import GetFullUserRequest

    todo = [a for a in authors[:params.enrich_top] if not _fresh(a)]
    if not todo:
        log.info("авторы", "био всех авторов уже в кеше")
        return 0
    log.info("авторы", f"запрашиваем био у {len(todo)} авторов (паузы 1.5–3 с)")
    done = failed = 0
    for a in todo:
        try:
            full = await client(GetFullUserRequest(a.input_user()))
            a.about = getattr(full.full_user, "about", "") or ""
        except FloodWaitError as e:
            if e.seconds > params.max_flood_wait:
                log.warn("авторы", f"FloodWait {e.seconds} с — обогащение остановлено на {done}")
                break
            await asyncio.sleep(e.seconds + 1)
            continue
        except Exception as e:  # noqa: BLE001 — скрытый профиль, нет access_hash и т.п.
            # не кешируем как «получено»: следующий запуск попробует снова
            failed += 1
            log.info("авторы", f"id {a.user_id}: био недоступно ({str(e)[:60]})")
            await asyncio.sleep(random.uniform(0.5, 1.0))
            continue
        store.set_about(a.user_id, a.about)
        done += 1
        await asyncio.sleep(random.uniform(1.5, 3.0))
    log.info("авторы", f"био получено: {done}" + (f", не удалось: {failed}" if failed else ""))
    return done
