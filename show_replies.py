# -*- coding: utf-8 -*-
"""
Кто написал в ответ на рассылку. Читает входящие личные сообщения выбранных аккаунтов прямо из Telegram
(через прокси) и показывает тех, кто написал последним (то есть ждёт ответа). Ничего не отправляет
и не помечает прочитанным.

    python show_replies.py +380981257112                 # за последние 48 часов
    python show_replies.py +380981257112 +380981257044 --hours 72
    python show_replies.py +380981257112 --all           # и те, кому ответили мы последними
    python show_replies.py +380981257112 --save          # ещё и ДОБАВИТЬ найденные ответы в мессенджер («Диалоги»)

Мессенджер теперь сам догоняет пропущенные ответы (при подключении аккаунта и раз в 30 минут),
этот скрипт нужен, чтобы посмотреть вручную или принудительно.
Не запускай для аккаунтов, которые сейчас в прогреве/рассылке/мессенджере: один аккаунт = одно подключение
(скрипт сам откажется, если аккаунт занят).
"""
import asyncio
import os
import sys

sys.path.insert(0, ".")
import config  # noqa: F401,E402
from config import SESSIONS_DIR  # noqa: E402
from api import inbox_catchup as ic  # noqa: E402


async def replies(account: str, hours: float, show_all: bool, make_client=None) -> list[dict]:
    from accounts.manager import create_client
    from accounts import session_lock
    make_client = make_client or create_client
    path = os.path.join(SESSIONS_DIR, account + ".session")
    if not os.path.exists(path):
        raise FileNotFoundError(f"нет файла сессии {path}")
    if not session_lock.acquire(account):
        raise RuntimeError("аккаунт занят другим процессом (прогрев/рассылка/мессенджер)")
    client = make_client(path)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("аккаунт не авторизован")
        return await ic.collect_incoming(client, account, hours, show_all)
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        session_lock.release(account)


def save_to_inbox(account: str, rows: list[dict]) -> int:
    return ic.save_rows(account, rows)


async def main(argv: list[str]) -> None:
    hours = 48.0
    if "--hours" in argv:
        i = argv.index("--hours")
        hours = float(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    show_all = "--all" in argv
    save = "--save" in argv
    accounts = [a for a in argv if not a.startswith("--")]
    if not accounts:
        print("Укажи аккаунт: python show_replies.py +380981257112 [--hours 72] [--all] [--save]")
        return
    total = 0
    for acc in accounts:
        print(f"\n=== {acc}: сообщения за последние {hours:g} ч ===")
        try:
            rows = await replies(acc, hours, show_all)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {type(e).__name__}: {str(e)[:100]}")
            continue
        if not rows:
            print("  Новых входящих нет.")
        for r in rows:
            who = f"{r['name']}" + (f" (@{r['username']})" if r["username"] else f" (id {r['id']})")
            tag = "← ответ на рассылку" if r["from_broadcast"] else ""
            unread = f" [непрочитано: {r['unread']}]" if r["unread"] else ""
            arrow = "ОН:" if r["incoming"] else "МЫ:"
            print(f"  {r['date']:%d.%m %H:%M} UTC  {who}{unread} {tag}")
            print(f"      {arrow} {r['text'][:160]}")
        total += sum(1 for r in rows if r["incoming"])
        if save:
            n_new = save_to_inbox(acc, rows)
            print(f"  -> добавлено в мессенджер новых диалогов: {n_new} (только те, кому писала наша рассылка)")
    print(f"\nВсего ждут ответа: {total}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
