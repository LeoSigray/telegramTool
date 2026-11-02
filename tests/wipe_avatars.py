"""Удалить ВСЕ фото профиля (аватары) аккаунта. Перед удалением — локальная копия.

Запуск: .venv/bin/python -m tests.wipe_avatars <имя_сессии> [--yes]
Без --yes только показывает, что будет удалено, и ничего не трогает.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")


async def main() -> int:
    from telethon import TelegramClient, functions

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    session = args[0] if args else "232497406_telethon"
    do_it = "--yes" in sys.argv

    here = os.path.dirname(os.path.abspath(__file__))
    client = TelegramClient(os.path.join(here, "..", "sessions", session), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("сессия не авторизована — выхожу, ничего не делаю")
        return 1

    me = await client.get_me()
    print(f"аккаунт: {me.first_name or ''} @{me.username or '—'} id={me.id}")

    photos = await client.get_profile_photos("me")
    print(f"фото профиля сейчас: {len(photos)}")
    if not photos:
        print("удалять нечего")
        await client.disconnect()
        return 0
    for p in photos:
        print(f"   id={p.id}  от {p.date:%Y-%m-%d %H:%M}")

    if not do_it:
        print("\nэто был показ без изменений. Для удаления добавь --yes")
        await client.disconnect()
        return 0

    backup = os.path.join(here, "..", "data", f"avatars_backup_{session}")
    os.makedirs(backup, exist_ok=True)
    saved = 0
    for i, p in enumerate(photos, 1):
        try:
            path = await client.download_media(p, file=os.path.join(backup, f"{i:02d}_{p.id}.jpg"))
            if path:
                saved += 1
        except Exception as e:  # noqa: BLE001
            print(f"   не сохранилось id={p.id}: {str(e)[:60]}")
    print(f"\nкопия сохранена: {saved} из {len(photos)} → {os.path.abspath(backup)}")

    res = await client(functions.photos.DeletePhotosRequest(id=photos))
    print(f"удалено сервером: {len(res)}")

    left = await client.get_profile_photos("me")
    me2 = await client.get_me()
    print(f"осталось фото профиля: {len(left)}")
    print(f"аватар у профиля: {'ЕЩЁ ЕСТЬ' if me2.photo else 'нет'}")
    await client.disconnect()
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
