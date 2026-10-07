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


async def enrich_authors(client, authors: list, params, store, log) -> int:
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.users import GetFullUserRequest

    todo = [a for a in authors[:params.enrich_top] if not _fresh(a)]
    if not todo:
        log.info("авторы", "био всех авторов уже в кеше")
        return 0
    log.info("авторы", f"запрашиваем био у {len(todo)} авторов (паузы 1.5–3 с)")
    done = 0
    for a in todo:
        try:
            full = await client(GetFullUserRequest(a.user_id))
            a.about = getattr(full.full_user, "about", "") or ""
        except FloodWaitError as e:
            if e.seconds > params.max_flood_wait:
                log.warn("авторы", f"FloodWait {e.seconds} с — обогащение остановлено на {done}")
                break
            await asyncio.sleep(e.seconds + 1)
            continue
        except Exception as e:  # noqa: BLE001 — скрытый профиль, нет access_hash и т.п.
            log.info("авторы", f"id {a.user_id}: био недоступно ({str(e)[:60]})")
            a.about = ""
        store.set_about(a.user_id, a.about)
        done += 1
        await asyncio.sleep(random.uniform(1.5, 3.0))
    log.info("авторы", f"био получено: {done}")
    return done
