"""
Поиск публичных Telegram-каналов по ключевым словам.
Фильтрует по: мин. подписчики, наличие linked discussion group (комментарии включены).
"""
import asyncio
import logging
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel

log = logging.getLogger(__name__)


@dataclass
class FoundChannel:
    channel_id: int
    access_hash: int
    username: str
    title: str
    subscribers: int
    linked_chat_id: int | None  # None = комментарии отключены


async def search_channels(
    client: TelegramClient,
    keyword: str,
    min_subs: int = 500,
    limit: int = 20,
    require_comments: bool = True,
    max_results: int = 5,
) -> list[FoundChannel]:
    """
    Ищет публичные каналы по ключевому слову через нативный TG API.
    require_comments=True — оставляет только каналы с linked discussion group.
    """
    try:
        result = await client(SearchRequest(q=keyword, limit=limit))
    except FloodWaitError as e:
        log.warning("FloodWait при поиске '%s': %ds", keyword, e.seconds)
        await asyncio.sleep(e.seconds)
        return []
    except Exception as e:
        log.warning("Ошибка поиска '%s': %s", keyword, e)
        return []

    channels: list[FoundChannel] = []

    for chat in result.chats:
        if len(channels) >= max_results:
            break
        if not isinstance(chat, Channel):
            continue
        if not getattr(chat, "broadcast", False):
            continue
        if not chat.username:
            continue

        # Быстрая проверка подписчиков из базового объекта (без доп. запроса)
        quick_subs = getattr(chat, "participants_count", 0) or 0
        if quick_subs > 0 and quick_subs < min_subs:
            continue

        print(f"    Проверяем @{chat.username} (~{quick_subs:,} подп.)...", flush=True)

        # GetFullChannelRequest — только для получения linked_chat_id
        linked_chat_id = None
        subs = quick_subs
        if require_comments:
            try:
                full = await client(GetFullChannelRequest(chat))
                linked_chat_id = getattr(full.full_chat, "linked_chat_id", None)
                subs = full.full_chat.participants_count or quick_subs
            except FloodWaitError as e:
                print(f"    FloodWait {e.seconds}s, пауза...", flush=True)
                await asyncio.sleep(min(e.seconds, 30))
                continue
            except Exception as e:
                log.debug("Ошибка GetFullChannel @%s: %s", chat.username, e)
                continue

            if not linked_chat_id:
                print(f"    @{chat.username} — нет обсуждений, пропуск", flush=True)
                continue
            await asyncio.sleep(0.3)

        if subs < min_subs:
            continue

        print(f"    ✓ @{chat.username} ({subs:,} подп.) — добавлен", flush=True)
        channels.append(FoundChannel(
            channel_id=chat.id,
            access_hash=chat.access_hash,
            username=chat.username,
            title=chat.title,
            subscribers=subs,
            linked_chat_id=linked_chat_id,
        ))

    return channels


async def search_channels_multi(
    client: TelegramClient,
    keywords: list[str],
    min_subs: int = 500,
    require_comments: bool = True,
) -> list[FoundChannel]:
    """
    Поиск по нескольким ключевым словам с дедупликацией по channel_id.
    Возвращает объединённый список отсортированный по подписчикам (desc).
    """
    seen_ids: set[int] = set()
    result: list[FoundChannel] = []

    for keyword in keywords:
        print(f"\n  Поиск по «{keyword}»...", flush=True)
        found = await search_channels(
            client, keyword,
            min_subs=min_subs,
            require_comments=require_comments,
            max_results=5,
        )
        new_count = 0
        for ch in found:
            if ch.channel_id not in seen_ids:
                seen_ids.add(ch.channel_id)
                result.append(ch)
                new_count += 1
        print(f"  Новых каналов: {new_count}", flush=True)

        await asyncio.sleep(1)

    result.sort(key=lambda c: c.subscribers, reverse=True)
    return result
