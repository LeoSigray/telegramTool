"""Прогон умного парсинга БЕЗ прокси — осознанно, для проверки пайплайна.

Обычно proxy_manager запрещает подключение без прокси (чтобы аккаунты не ходили с IP
компьютера или VPN). Здесь мы НЕ трогаем эту защиту в коде: run_smart_parse умеет
принимать уже готовый клиент, и мы передаём свой, созданный напрямую.

Так делать можно только для теста своим аккаунтом. Для боевых рассылок нужен
отдельный SOCKS5 на каждый аккаунт.

Запуск:
  .venv/bin/python -m tests.run_parse_noproxy @neuroclub_ai --people 50 --days 30 --minutes 25
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()


async def main() -> int:
    from telethon import TelegramClient

    from parsing.smart.run import run_smart_parse
    from parsing.smart.settings import Params

    ap = argparse.ArgumentParser()
    ap.add_argument("channel")
    ap.add_argument("--people", type=int, default=50)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--minutes", type=float, default=25.0)
    ap.add_argument("--session", default="232497406_telethon")
    ap.add_argument("--no-llm", action="store_true", help="только правила, без LLM")
    a = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    client = TelegramClient(os.path.join(here, "..", "sessions", a.session),
                            int(os.getenv("TELEGRAM_API_ID", "0")),
                            os.getenv("TELEGRAM_API_HASH", ""))
    await asyncio.wait_for(client.connect(), timeout=40)
    if not await client.is_user_authorized():
        print("сессия не авторизована"); return 1
    me = await client.get_me()
    print(f"аккаунт: {me.first_name} @{me.username or '—'} id={me.id}  (БЕЗ ПРОКСИ, напрямую)\n")
    client._smart_session_name = a.session  # noqa: SLF001

    params = Params(channel=a.channel, people=a.people, days=a.days,
                    max_minutes=a.minutes, no_llm=a.no_llm, session=a.session,
                    source="youtube")

    # отмечаем, когда появился каждый человек — чтобы видеть скорость набора
    t0 = time.perf_counter()
    marks: list = []

    def on_found(people):
        marks.append((len(people), time.perf_counter() - t0))
        print(f"  >>> людей набрано {len(people):3d}  за {time.perf_counter()-t0:6.1f} c", flush=True)

    try:
        out = await run_smart_parse(params, client=client, echo=True,
                                    on_found=on_found, handle_signals=False)
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    dt = time.perf_counter() - t0
    print(f"\n==== ИТОГО {dt/60:.1f} мин ====")
    print(f"отчёт: {out}")
    if marks:
        print("скорость набора людей:")
        for n, t in marks:
            print(f"   {n:3d} чел. — {t/60:5.1f} мин")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
