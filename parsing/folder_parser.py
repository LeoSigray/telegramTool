"""
Парсинг Telegram-папок (addlist ссылки).
Заходит по ссылке, получает список чатов, классифицирует и собирает информацию.
"""

import asyncio
import re
import random

from telethon import TelegramClient
from telethon.tl.functions.chatlists import CheckChatlistInviteRequest
from telethon.errors import FloodWaitError

from .categories import detect_category, detect_city
from .excel_writer import ParsingExcelWriter


async def parse_folder_link(client: TelegramClient, url: str) -> list[dict]:
    """
    Парсит одну ссылку на папку (addlist).
    Возвращает список чатов с информацией.
    """
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
    writer: ParsingExcelWriter,
    delay_min: float = 2.0,
    delay_max: float = 5.0,
) -> dict:
    """
    Парсит все ссылки на папки, классифицирует чаты и записывает в Excel.

    Возвращает статистику: {"total": N, "by_category": {cat: count}}.
    """
    all_chats = []
    total_links = len(folder_links)

    for idx, url in enumerate(folder_links, 1):
        try:
            chats = await parse_folder_link(client, url)
            all_chats.extend(chats)
            print(f"  [{idx}/{total_links}] {len(chats)} чатов")

        except FloodWaitError as e:
            wait_time = e.seconds + 5
            print(f"  [{idx}/{total_links}] FloodWait: ждем {wait_time} сек...")
            await asyncio.sleep(wait_time)
            try:
                chats = await parse_folder_link(client, url)
                all_chats.extend(chats)
                print(f"  [{idx}/{total_links}] {len(chats)} чатов (после ожидания)")
            except Exception as e2:
                print(f"  [{idx}/{total_links}] Ошибка после ожидания: {e2}")

        except Exception as e:
            print(f"  [{idx}/{total_links}] Ошибка: {e}")

        if idx < total_links:
            delay = random.uniform(delay_min, delay_max)
            await asyncio.sleep(delay)

    # Группируем по категориям и записываем в Excel
    by_category = {}
    for chat in all_chats:
        cat = chat["category"]
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(chat)

    total_added = 0
    for cat in sorted(by_category.keys()):
        added = writer.add_folder_chats(cat, by_category[cat], sort_by_subs=True)
        total_added += added
        print(f"  -> {cat}: +{added} новых (всего в папке: {len(by_category[cat])})")

    return {
        "total_parsed": len(all_chats),
        "total_added": total_added,
        "by_category": {cat: len(chats) for cat, chats in by_category.items()},
    }
