"""
Парсинг Telegram-папок (addlist ссылки).
Получает список чатов, классифицирует по категориям, записывает в targets.xlsx.
"""

import asyncio
import re
import random

from telethon import TelegramClient
from telethon.tl.functions.chatlists import CheckChatlistInviteRequest
from telethon.errors import FloodWaitError

from .categories import detect_category, detect_city
from data.excel_manager import add_folder_chats


async def parse_folder_link(client: TelegramClient, url: str) -> list[dict]:
    """Парсит одну addlist-ссылку. Возвращает список чатов."""
    slug_match = re.search(r"/addlist/([A-Za-z0-9_-]+)", url or "")
    if not slug_match:
        return []

    slug = slug_match.group(1)
    check = await client(CheckChatlistInviteRequest(slug=slug))

    chats = []
    for chat in check.chats:
        title = getattr(chat, "title", "") or ""
        username = getattr(chat, "username", None)
        chat_id = getattr(chat, "id", 0)
        link = f"https://t.me/{username}" if username else f"tg://private?id={chat_id}"
        subs = getattr(chat, "participants_count", 0) or 0
        chat_type = "Канал" if getattr(chat, "broadcast", False) else "Группа"

        chats.append({
            "title": title,
            "username": username or "",
            "link": link,
            "chat_type": chat_type,
            "subscribers": subs,
            "category": detect_category(title),
            "city": detect_city(title) or "",
        })

    return chats


async def parse_all_folders(
    client: TelegramClient,
    folder_links: list[str],
    delay_min: float = 2.0,
    delay_max: float = 5.0,
) -> dict:
    """
    Парсит все ссылки, классифицирует и записывает в targets.xlsx.
    Возвращает статистику.
    """
    all_chats = []
    total = len(folder_links)

    for idx, url in enumerate(folder_links, 1):
        try:
            chats = await parse_folder_link(client, url)
            all_chats.extend(chats)
            print(f"  [{idx}/{total}] {len(chats)} чатов")

        except FloodWaitError as e:
            wait = e.seconds + 5
            print(f"  [{idx}/{total}] FloodWait: ждем {wait} сек...")
            await asyncio.sleep(wait)
            try:
                chats = await parse_folder_link(client, url)
                all_chats.extend(chats)
                print(f"  [{idx}/{total}] {len(chats)} чатов (повтор)")
            except Exception as e2:
                print(f"  [{idx}/{total}] Ошибка: {e2}")

        except Exception as e:
            print(f"  [{idx}/{total}] Ошибка: {e}")

        if idx < total:
            await asyncio.sleep(random.uniform(delay_min, delay_max))

    # Группируем по категориям и пишем в Excel
    by_category: dict[str, list[dict]] = {}
    for chat in all_chats:
        by_category.setdefault(chat["category"], []).append(chat)

    total_added = 0
    for cat in sorted(by_category):
        added = add_folder_chats(cat, by_category[cat], sort_by_subs=True)
        total_added += added
        if added > 0:
            print(f"  -> {cat}: +{added} новых")

    return {
        "total_parsed": len(all_chats),
        "total_added": total_added,
        "by_category": {cat: len(chats) for cat, chats in by_category.items()},
    }
