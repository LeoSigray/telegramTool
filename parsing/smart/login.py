"""Вход в личный аккаунт для умного парсинга (создаёт свою сессию, без tdata).

    python -m parsing.smart.login            # сессия «personal»
    python -m parsing.smart.login --name my  # своё имя сессии

Нужны TELEGRAM_API_ID и TELEGRAM_API_HASH в .env (те, что вы получили на my.telegram.org
для этого аккаунта). Номер, код из Telegram и пароль двухфакторной защиты (если есть)
вводите в терминале сами: программа их нигде не сохраняет, в сессию попадает только
ключ авторизации.

Сессия сохраняется в sessions/ и в базу проекта (иначе её перезапишет старая копия из БД).
После входа запускайте парсинг так:
    python -m parsing.smart --channel @канал --session personal
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys


async def _login(name: str) -> int:
    from accounts.manager import create_client
    from config import CONFIG, SESSIONS_DIR
    from data.db import init_db, save_session_from_file

    if not CONFIG["TELEGRAM_API_ID"] or not CONFIG["TELEGRAM_API_HASH"]:
        print("В .env не заполнены TELEGRAM_API_ID и TELEGRAM_API_HASH.")
        return 1

    init_db()
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    path = os.path.join(SESSIONS_DIR, f"{name}.session")
    client = create_client(path)
    try:
        # start() сам спросит номер телефона, код из Telegram и пароль 2FA
        await client.start()
        me = await client.get_me()
        print(f"\nВход выполнен: {me.first_name or ''} (@{me.username or 'без username'}), id {me.id}")
    finally:
        await client.disconnect()
    if save_session_from_file(path):
        print(f"Сессия сохранена: {path} и в базе проекта.")
    print(f"\nЗапуск парсинга:\n  python -m parsing.smart --channel @канал --session {name}")
    print(f"Чтобы использовать её всегда, добавьте в .env строку: SMART_SESSION={name}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m parsing.smart.login")
    p.add_argument("--name", default="personal", help="имя сессии (по умолчанию personal)")
    args = p.parse_args(argv)
    try:
        return asyncio.run(_login(args.name))
    except KeyboardInterrupt:
        print("\nОтменено.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
