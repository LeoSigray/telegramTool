# -*- coding: utf-8 -*-
"""
Отчёт-доказательство для поддержки магазина: проверяет выданные сессии и печатает компактную сводку,
которую удобно сфотографировать (скриншот). Ключи и пароли не печатает.

    python prove_dead.py путь\к\архиву.rar          # архив из заказа (rar/zip) — можно несколько
    python prove_dead.py путь\к\18483442963.session  # или сам файл сессии (json рядом подхватится)
    python prove_dead.py 8378677 ... --order 8378677 # --order — номер заказа для шапки отчёта

Каждая сессия проверяется ДВАЖДЫ: с нашим api_id и с api_id продавца из его json — чтобы было видно,
что дело не в настройках у нас.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, ".")
import config  # noqa: F401,E402
from config import CONFIG  # noqa: E402


async def ask(session_copy: str, api_id: int, api_hash: str, account: str) -> str:
    from telethon import TelegramClient
    from telethon import errors as E
    from telethon.tl.functions.users import GetUsersRequest
    from telethon.tl.types import InputUserSelf
    from proxy_manager import client_kwargs_or_die

    client = TelegramClient(session_copy[:-len(".session")], api_id, api_hash,
                            **client_kwargs_or_die(account), connection_retries=3)
    try:
        await client.connect()
        me = (await client(GetUsersRequest([InputUserSelf()])))[0]
        return f"ЖИВОЙ (аккаунт {me.first_name or ''} id {me.id})"
    except E.AuthKeyUnregisteredError:
        return "AuthKeyUnregisteredError — «ключ сессии не зарегистрирован» (сессия недействительна)"
    except (E.UserDeactivatedBanError, E.UserDeactivatedError):
        return "аккаунт удалён/заблокирован Telegram"
    except Exception as e:  # noqa: BLE001
        return f"не удалось проверить: {type(e).__name__}: {str(e)[:70]}"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def collect(path: str) -> list[tuple[str, str | None]]:
    """-> [(путь к .session, путь к .json или None)]"""
    from accounts import darkstore as ds
    if path.lower().endswith(".session"):
        j = os.path.splitext(path)[0] + ".json"
        return [(path, j if os.path.exists(j) else None)]
    root = ds.unpack(path)
    out = []
    for sp in ds.collect_files(root)["session"]:
        j = os.path.splitext(sp)[0] + ".json"
        out.append((sp, j if os.path.exists(j) else None))
    return out


async def main(argv: list[str]) -> None:
    order = None
    if "--order" in argv:
        i = argv.index("--order")
        order = argv[i + 1] if i + 1 < len(argv) else None
        argv = argv[:i] + argv[i + 2:]
    paths = [a for a in argv if not a.startswith("--") and os.path.exists(a)]
    if not paths:
        print("Укажи путь к архиву (.rar/.zip) или .session. Пример: python prove_dead.py C:/путь/18483442963.rar --order 8378677")
        return
    from urllib.parse import urlparse
    from config import load_proxy
    px = urlparse(load_proxy() or "")
    now = datetime.now(timezone.utc)
    line = "=" * 74
    print(line)
    print(f"ПРОВЕРКА ВЫДАННЫХ СЕССИЙ TELEGRAM   {now:%d.%m.%Y %H:%M:%S} UTC")
    if order:
        print(f"Заказ DarkStore №{order}")
    print(f"Проверка идёт с IP прокси: {px.hostname}:{px.port} (Telegram видит этот адрес)")
    print(line)
    n = dead = 0
    for path in paths:
        for sp, jp in collect(path):
            n += 1
            tmp = tempfile.mkdtemp(prefix="prove_")
            copy = os.path.join(tmp, "x.session")
            shutil.copy(sp, copy)
            name = os.path.splitext(os.path.basename(sp))[0]
            info = {}
            if jp:
                try:
                    info = json.load(open(jp, encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            print(f"\nАккаунт: +{info.get('phone') or name}   (файл {os.path.basename(sp)}, {os.path.getsize(sp)} байт)")
            if info:
                print(f"  из json продавца: сессия создана {info.get('session_created_date')}, "
                      f"последнее подключение {info.get('last_connect_date')}, spamblock={info.get('spamblock')}")
            r1 = await ask(copy, CONFIG["TELEGRAM_API_ID"], CONFIG["TELEGRAM_API_HASH"], "prove")
            print(f"  ответ Telegram (наш api_id):          {r1}")
            if info.get("app_id") and info.get("app_hash"):
                shutil.copy(sp, copy)
                r2 = await ask(copy, int(info["app_id"]), info["app_hash"], "prove")
                print(f"  ответ Telegram (api_id продавца {info['app_id']}): {r2}")
            else:
                r2 = r1
            if "AuthKey" in r1 and "AuthKey" in r2:
                dead += 1
            shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + line)
    print(f"Итого: проверено {n}, недействительных {dead}.")
    if dead:
        print("Вывод: сессии недействительны на стороне Telegram — товар выдан нерабочим.")
    print(line)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
