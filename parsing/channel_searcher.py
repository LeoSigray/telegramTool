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
    limit: int = 50,
    require_comments: bool = True,
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
        if not isinstance(chat, Channel):
            continue
        # Только каналы (broadcast=True), не группы
        if not getattr(chat, "broadcast", False):
            continue
        if not chat.username:
            continue

        # Быстрая проверка подписчиков — participants_count есть в базовом объекте
        quick_subs = getattr(chat, "participants_count", 0) or 0
        if quick_subs < min_subs:
            continue

        # GetFullChannelRequest только для каналов прошедших фильтр — нужен linked_chat_id
        linked_chat_id = None
        if require_comments:
            try:
                full = await client(GetFullChannelRequest(chat))
                linked_chat_id = getattr(full.full_chat, "linked_chat_id", None)
                subs = full.full_chat.participants_count or quick_subs
            except FloodWaitError as e:
                log.warning("FloodWait GetFullChannel @%s: %ds", chat.username, e.seconds)
                await asyncio.sleep(e.seconds)
                continue
            except Exception as e:
                log.debug("Ошибка GetFullChannel @%s: %s", chat.username, e)
                continue

            if not linked_chat_id:
                continue
            await asyncio.sleep(0.4)  # анти-флуд только для каналов прошедших фильтр
        else:
            subs = quick_subs

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
        log.info("Поиск каналов по: '%s'", keyword)
        found = await search_channels(
            client, keyword,
            min_subs=min_subs,
            require_comments=require_comments,
        )
        for ch in found:
            if ch.channel_id not in seen_ids:
                seen_ids.add(ch.channel_id)
                result.append(ch)
                log.info("  Найден: @%s (%d подп.)", ch.username, ch.subscribers)

        await asyncio.sleep(2)  # между ключевыми словами

    result.sort(key=lambda c: c.subscribers, reverse=True)
    return result
