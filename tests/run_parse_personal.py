"""Прогон умного парсинга личным аккаунтом — через штатные механизмы проекта.

Отличия от прошлой (сгоревшей) версии:
  * клиент создаётся через accounts.manager.create_client, то есть проходит
    proxy_manager — личной сессии прямой выход разрешён только через DIRECT_SESSIONS;
  * берётся файловая блокировка accounts.session_lock: «один аккаунт = один процесс».
    Именно её отсутствие в прошлый раз привело к «database is locked», а затем
    к AuthKeyDuplicated и потере аккаунта;
  * по умолчанию скромные лимиты: для личного аккаунта опасен не IP, а объём активности.

Запуск (вход должен быть сделан заранее: python -m parsing.smart.login):
  .venv/bin/python -m tests.run_parse_personal @neuroclub_ai --people 10 --minutes 8
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
    from accounts import session_lock
    from accounts.manager import create_client
    from config import SESSIONS_DIR
    from parsing.smart.run import run_smart_parse
    from parsing.smart.settings import Params
    from proxy_manager import ProxyRequired, direct_sessions

    ap = argparse.ArgumentParser()
    ap.add_argument("channel")
    ap.add_argument("--session", default="personal")
    ap.add_argument("--people", type=int, default=10)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--minutes", type=float, default=8.0)
    ap.add_argument("--max-rounds", type=int, default=3)
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()

    path = os.path.join(SESSIONS_DIR, f"{a.session}.session")
    if not os.path.exists(path):
        print(f"нет сессии {a.session}. Сначала войди:\n"
              f"    cd {os.getcwd()} && .venv/bin/python -m parsing.smart.login --name {a.session}")
        return 1

    if a.session.lower() not in direct_sessions():
        print(f"сессия «{a.session}» не в DIRECT_SESSIONS — ей нужен прокси. "
              f"Для личного аккаунта добавь в .env: DIRECT_SESSIONS={a.session}")
        return 1

    # «один аккаунт = один процесс»: без этого Telegram рвёт сессию
    if not session_lock.acquire(a.session):
        pid = session_lock.holder_pid(a.session)
        print(f"сессия {a.session} уже занята другим процессом "
              f"({session_lock.describe_holder(pid)}). Закрой его и повтори.")
        return 1

    client = None
    try:
        try:
            client = create_client(path)
        except ProxyRequired as e:
            print(f"прокси: {e}")
            return 1
        await asyncio.wait_for(client.connect(), timeout=40)
        if not await client.is_user_authorized():
            print("сессия не авторизована — войди заново: python -m parsing.smart.login")
            return 1
        me = await client.get_me()
        print(f"аккаунт: {me.first_name or ''} @{me.username or '—'} id={me.id}")
        print(f"лимиты: людей {a.people}, раундов {a.max_rounds}, минут {a.minutes}\n")
        client._smart_session_name = a.session  # noqa: SLF001

        params = Params(channel=a.channel, people=a.people, days=a.days,
                        max_minutes=a.minutes, max_rounds=a.max_rounds,
                        no_llm=a.no_llm, session=a.session, source="youtube")

        t0 = time.perf_counter()
        marks: list = []

        def on_found(people):
            marks.append((len(people), time.perf_counter() - t0))
            print(f"  >>> людей {len(people):3d} — {time.perf_counter() - t0:6.1f} c", flush=True)

        out = await run_smart_parse(params, client=client, echo=True,
                                    on_found=on_found, handle_signals=False)
        dt = time.perf_counter() - t0
        print(f"\n==== {dt / 60:.1f} мин ====\nотчёт: {out}")
        if marks:
            print("когда появлялись люди:")
            for n, t in marks:
                print(f"   {n:3d} чел. — {t / 60:5.1f} мин")
        else:
            print("людей не найдено")
        return 0
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        session_lock.release(a.session)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
