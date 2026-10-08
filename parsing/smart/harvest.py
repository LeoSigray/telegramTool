"""Сбор сообщений из выбранных источников.

Группа: последние сообщения за окно (не больше msgs_per_source).
Канал: последние посты, у которых есть комментарии, и комментарии к ним.

Сбор инкрементальный: в SQLite хранится последний прочитанный id, и повторный
запуск дочитывает только новое. В чаты не вступаем, только читаем публичное
(или то, где аккаунт уже состоит).
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from .models import Author, Message


def _status(user) -> tuple[str, str]:
    st = getattr(user, "status", None)
    name = type(st).__name__ if st is not None else ""
    mapping = {
        "UserStatusOnline": "online",
        "UserStatusRecently": "recently",
        "UserStatusLastWeek": "last_week",
        "UserStatusLastMonth": "last_month",
        "UserStatusOffline": "offline",
    }
    kind = mapping.get(name, "empty")
    was = ""
    if kind == "offline" and getattr(st, "was_online", None):
        was = st.was_online.astimezone(timezone.utc).isoformat()
    return kind, was


def author_from_user(user) -> Author:
    username = getattr(user, "username", None) or ""
    if not username:
        for u in getattr(user, "usernames", None) or []:
            if getattr(u, "active", False):
                username = u.username
                break
    kind, was = _status(user)
    return Author(user_id=user.id, username=username, first_name=user.first_name or "",
                  last_name=user.last_name or "", is_bot=bool(user.bot),
                  is_deleted=bool(user.deleted), premium=bool(getattr(user, "premium", False)),
                  status=kind, was_online=was, access_hash=getattr(user, "access_hash", 0) or 0)


def to_message(source_id: int, m, post_id: int = 0, is_post: bool = False) -> Message | None:
    from telethon.tl.types import PeerUser

    text = (m.message or "").strip()
    if not text:
        return None
    if isinstance(m.from_id, PeerUser):
        kind, sender = "user", m.from_id.user_id
    elif m.from_id is not None:
        kind, sender = "channel", 0
    else:
        kind, sender = "none", 0
    reply_to = 0
    if m.reply_to is not None:
        reply_to = getattr(m.reply_to, "reply_to_msg_id", 0) or 0
    return Message(source_id=source_id, msg_id=m.id, date=m.date.astimezone(timezone.utc),
                   sender_id=sender, sender_kind=kind, text=text, reply_to=reply_to,
                   post_id=post_id, is_post=is_post, is_fwd=m.fwd_from is not None)


def _collect_author(m, authors: dict) -> None:
    from telethon.tl.types import User

    u = getattr(m, "sender", None)
    if isinstance(u, User) and u.id not in authors:
        authors[u.id] = author_from_user(u)


async def _harvest_group(client, src, ent, params, store, since) -> int:
    # позиция чтения — из базы: источник из поиска приходит «свежим», без неё
    # повторный запуск перечитывал бы всю историю
    stored = store.get_source(src.chat_id)
    last = max(src.last_msg_id, stored.last_msg_id if stored else 0)
    msgs, authors, top_id = [], {}, last
    async for m in client.iter_messages(ent, limit=params.msgs_per_source,
                                        min_id=last, wait_time=params.wait_time):
        if m.date < since:
            break
        top_id = max(top_id, m.id)
        rec = to_message(src.chat_id, m)
        if rec:
            msgs.append(rec)
            _collect_author(m, authors)
    added = store.add_messages(msgs)
    store.upsert_authors(authors.values())
    store.set_harvested(src.chat_id, top_id)
    return added


async def _search_in_chat(client, ent, terms: list, params, since) -> list:
    """Сообщения чата за окно, где есть хоть одно из слов (поиск Telegram внутри чата)."""
    found: dict = {}
    for t in terms:
        async for m in client.iter_messages(ent, search=t, limit=params.targeted_limit,
                                            wait_time=params.wait_time):
            if m.date < since:
                break
            found[m.id] = m
        await asyncio.sleep(random.uniform(0.6, 1.2))
    return list(found.values())


async def _harvest_group_targeted(client, src, ent, params, store, since, terms) -> int:
    """Точечное чтение группы: последние targeted_tail сообщений + поиск по словам.
    Позицию чтения не двигаем: точечное чтение не «дочитывает» историю целиком."""
    msgs, authors, seen = [], {}, set()
    tail = [m async for m in client.iter_messages(ent, limit=params.targeted_tail,
                                                  wait_time=params.wait_time)]
    for m in tail + await _search_in_chat(client, ent, terms, params, since):
        if m.id in seen or m.date < since:
            continue
        seen.add(m.id)
        rec = to_message(src.chat_id, m)
        if rec:
            msgs.append(rec)
            _collect_author(m, authors)
    added = store.add_messages(msgs)
    store.upsert_authors(authors.values())
    store.set_harvested(src.chat_id, 0)
    return added


async def _harvest_channel(client, src, ent, params, store, since, log, terms=None) -> int:
    posts = []
    # при точечном чтении — несколько свежих постов, остальное добирает поиск по словам
    limit = max(5, params.posts_per_channel // 3) if terms else params.posts_per_channel
    async for p in client.iter_messages(ent, limit=limit, wait_time=params.wait_time):
        # комментарии под постом недельной давности ещё могут быть свежими
        if p.date < since - timedelta(days=7):
            break
        posts.append(p)
    if terms:
        have = {p.id for p in posts}
        posts += [p for p in await _search_in_chat(client, ent, terms, params, since - timedelta(days=7))
                  if p.id not in have]
    store.add_messages([r for r in (to_message(src.chat_id, p, is_post=True) for p in posts) if r])

    total, top_id = 0, src.last_msg_id
    for p in posts:
        rep = getattr(p, "replies", None)
        if not rep or not getattr(rep, "replies", 0):
            continue
        top_id = max(top_id, p.id)
        last = store.max_comment_id(src.chat_id, p.id)
        if last and getattr(rep, "max_id", None) and rep.max_id <= last:
            continue  # новых комментариев нет
        msgs, authors = [], {}
        try:
            async for c in client.iter_messages(ent, reply_to=p.id, limit=params.comments_per_post,
                                                min_id=last, wait_time=params.wait_time):
                if c.date < since:
                    break
                rec = to_message(src.chat_id, c, post_id=p.id)
                if rec:
                    msgs.append(rec)
                    _collect_author(c, authors)
        except Exception as e:  # noqa: BLE001 — пост без обсуждения, удалённый пост
            from telethon.errors import FloodWaitError
            if isinstance(e, FloodWaitError):
                raise
            log.info("сбор", f"{src.label()}: пост {p.id} пропущен ({str(e)[:80]})")
        total += store.add_messages(msgs)
        store.upsert_authors(authors.values())
        await asyncio.sleep(random.uniform(1.0, 2.0))
    store.set_harvested(src.chat_id, top_id)
    return total


async def harvest(client, items, params, store, log, terms=None) -> dict:
    """items: [(Source, entity)]. Возвращает {chat_id: сколько новых сообщений}.
    terms — слова товара и фразы покупателя: если заданы, чат читается точечно (поиск по ним
    внутри чата + хвост последних сообщений), а не весь подряд."""
    from telethon.errors import FloodWaitError

    since = datetime.now(timezone.utc) - timedelta(days=params.days)
    stats: dict[int, int] = {}
    for n, (src, ent) in enumerate(items, 1):
        try:
            if src.kind == "channel":
                got = await _harvest_channel(client, src, ent, params, store, since, log, terms)
                what = "комментариев"
            elif terms:
                got = await _harvest_group_targeted(client, src, ent, params, store, since, terms)
                what = "сообщений"
            else:
                got = await _harvest_group(client, src, ent, params, store, since)
                what = "сообщений"
            stats[src.chat_id] = got
            log.info("сбор", f"[{n}/{len(items)}] {src.label()}: новых {what} {got}"
                             + (" (точечно)" if terms else ""))
        except FloodWaitError as e:
            if e.seconds > params.max_flood_wait:
                log.error("сбор", f"FloodWait {e.seconds} с на {src.label()} — сбор остановлен, "
                                  "считаем по уже собранному")
                break
            log.warn("сбор", f"FloodWait {e.seconds} с на {src.label()} — ждём и идём дальше")
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:  # noqa: BLE001 — один плохой источник не роняет запуск
            src.status, src.reason = "error", f"ошибка чтения: {str(e)[:120]}"
            store.save_decision(params.channel, src)
            log.warn("сбор", f"{src.label()}: {src.reason}")
        await asyncio.sleep(random.uniform(2.0, 4.0))
    return stats
