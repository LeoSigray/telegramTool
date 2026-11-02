"""Какие сессии ещё живы: подключаемся, спрашиваем «кто я», отключаемся. Ничего не меняем."""
from __future__ import annotations

import asyncio
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")


async def check(path: str) -> str:
    from telethon import TelegramClient
    from telethon.errors import AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedBanError

    name = os.path.basename(path)
    client = TelegramClient(path[: -len(".session")], API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=25)
        if not await client.is_user_authorized():
            return f"{name:34s} НЕ авторизована (нужен вход заново)"
        me = await client.get_me()
        return (f"{name:34s} ЖИВА  id={me.id} @{me.username or '—'} "
                f"{(me.first_name or '')[:20]} {me.phone or ''}")
    except asyncio.TimeoutError:
        return f"{name:34s} таймаут подключения (сеть/прокси)"
    except (AuthKeyUnregisteredError, SessionRevokedError) as e:
        return f"{name:34s} ОТОЗВАНА ({type(e).__name__})"
    except UserDeactivatedBanError:
        return f"{name:34s} аккаунт заблокирован"
    except Exception as e:  # noqa: BLE001
        return f"{name:34s} ошибка: {type(e).__name__}: {str(e)[:70]}"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def main() -> None:
    print(f"API_ID={API_ID}  hash={'задан' if API_HASH else 'ПУСТО'}\n")
    paths = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", "sessions", "*.session")))
    for p in paths:
        print(await check(os.path.abspath(p)), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
