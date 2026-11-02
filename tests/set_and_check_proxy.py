"""Записать прокси в БД проекта и проверить, что через него реально подключается Telegram.

Запуск: .venv/bin/python -m tests.set_and_check_proxy "<строка прокси>" [сессия]
Прокси сохраняется штатной функцией проекта (config.save_proxy → data.db.set_proxy).
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()


async def main() -> int:
    from telethon import TelegramClient

    import config
    from data.db import init_db
    from proxy_manager import ProxyRequired, client_kwargs_for

    raw = sys.argv[1] if len(sys.argv) > 1 else ""
    session = sys.argv[2] if len(sys.argv) > 2 else "232497406_telethon"
    if not raw:
        print("нужна строка прокси"); return 2

    init_db()
    config.save_proxy(raw)
    print(f"прокси сохранён в БД: {raw[:48]}…")
    print(f"читается обратно: {(config.load_proxy() or '')[:48]}…\n")

    try:
        kwargs = client_kwargs_for(session)
    except ProxyRequired as e:
        print(f"проект отверг прокси: {e}"); return 1
    kind = "MTProxy" if "connection" in kwargs else "SOCKS5"
    print(f"распознан как: {kind}")
    print(f"параметры клиента: { {k: (str(v)[:60]) for k, v in kwargs.items()} }\n")

    here = os.path.dirname(os.path.abspath(__file__))
    client = TelegramClient(os.path.join(here, "..", "sessions", session),
                            int(os.getenv("TELEGRAM_API_ID", "0")),
                            os.getenv("TELEGRAM_API_HASH", ""),
                            timeout=20, **kwargs)
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(client.connect(), timeout=40)
        if not await client.is_user_authorized():
            print("подключились, но сессия не авторизована"); return 1
        me = await client.get_me()
        print(f"ПОДКЛЮЧИЛОСЬ за {time.perf_counter()-t0:.1f} c — "
              f"{me.first_name} @{me.username or '—'} id={me.id}")
        return 0
    except asyncio.TimeoutError:
        print(f"ТАЙМАУТ за {time.perf_counter()-t0:.1f} c — через этот прокси не соединилось")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"ОШИБКА за {time.perf_counter()-t0:.1f} c — {type(e).__name__}: {str(e)[:120]}")
        return 1
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
