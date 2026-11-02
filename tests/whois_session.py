"""Кто такой аккаунт из живой сессии: только чтение своего профиля, ничего не меняем."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

SESSION = sys.argv[1] if len(sys.argv) > 1 else "232497406_telethon"
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")


def approx_created(uid: int) -> str:
    """Очень грубая оценка даты регистрации по id: он растёт со временем."""
    marks = [(1_000_000, 2013), (100_000_000, 2015), (500_000_000, 2017), (1_000_000_000, 2019),
             (2_000_000_000, 2021), (5_000_000_000, 2022), (6_000_000_000, 2023),
             (7_000_000_000, 2024), (8_000_000_000, 2025)]
    year = "до 2013"
    for threshold, y in marks:
        if uid >= threshold:
            year = f"примерно {y} или позже"
    return year


async def main() -> None:
    from telethon import TelegramClient, functions

    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "sessions", SESSION)
    client = TelegramClient(path, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("сессия не авторизована")
        return

    me = await client.get_me()
    full = await client(functions.users.GetFullUserRequest("me"))
    f = full.full_user

    print(f"сессия:        {SESSION}")
    print(f"id:            {me.id}   ({approx_created(me.id)})")
    print(f"имя:           {me.first_name or ''} {me.last_name or ''}".rstrip())
    print(f"username:      @{me.username or '— нет'}")
    print(f"телефон:       +{me.phone}")
    print(f"био:           {f.about or '— пусто'}")
    print(f"premium:       {'да' if getattr(me, 'premium', False) else 'нет'}")
    print(f"бот/скам/фейк: bot={me.bot} scam={me.scam} fake={me.fake} ограничен={me.restricted}")
    print(f"фото профиля:  {'есть' if me.photo else 'НЕТ'}")
    print(f"общих групп:   {f.common_chats_count}")

    dialogs = await client.get_dialogs(limit=100)
    groups = [d for d in dialogs if d.is_group]
    chans = [d for d in dialogs if d.is_channel and not d.is_group]
    users = [d for d in dialogs if d.is_user]
    print(f"\nдиалогов (до 100): всего {len(dialogs)} — "
          f"личных {len(users)}, групп {len(groups)}, каналов {len(chans)}")
    if dialogs:
        last = max(dialogs, key=lambda d: d.date or datetime.min.replace(tzinfo=timezone.utc))
        print(f"последняя активность в диалогах: {last.date:%Y-%m-%d}")
    for d in dialogs[:10]:
        kind = "группа" if d.is_group else ("канал" if d.is_channel else "личка")
        print(f"   {kind:7s} {str(d.name)[:42]:44s} {d.date:%Y-%m-%d}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
