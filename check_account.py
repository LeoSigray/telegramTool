# -*- coding: utf-8 -*-
"""
Проверка конкретных аккаунтов (подключение ТОЛЬКО через прокси — жёсткий режим).

    python check_account.py 18325147520
    python check_account.py 18325147520 +380981257044 --spambot
    python check_account.py 18325147520 --kick        # завершить чужие сессии (продавца и др.)

Показывает: авторизован ли аккаунт, имя/юзернейм/телефон, Premium, сколько других устройств
сейчас в аккаунте (чужие входы продавца) и их страны. С --spambot дополнительно спрашивает у
@SpamBot, есть ли ограничения (это обычное сообщение боту Telegram от имени аккаунта).
Остальные аккаунты не трогает.
"""
import asyncio
import os
import sys

sys.path.insert(0, ".")
import config  # noqa: F401,E402  (подхватывает .env)
from accounts.manager import create_client  # noqa: E402
from config import SESSIONS_DIR  # noqa: E402


async def check(name: str, spambot: bool, make_client=create_client, kick: bool = False) -> dict:
    from telethon.errors import AuthKeyUnregisteredError, UserDeactivatedBanError
    from telethon.tl.functions.account import GetAuthorizationsRequest

    path = os.path.join(SESSIONS_DIR, name + ".session")
    out = {"name": name, "ok": False, "info": ""}
    if not os.path.exists(path):
        out["info"] = f"нет файла сессии {path}"
        return out
    from accounts import session_lock
    if not session_lock.acquire(name):
        out["info"] = "занят другим процессом (прогрев/рассылка/бэкенд в другом окне) — проверить нельзя, чтобы не сломать сессию"
        return out
    client = make_client(path)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            out["info"] = "НЕ авторизован (сессия мёртвая или аккаунт заблокирован)"
            return out
        me = await client.get_me()
        out["ok"] = True
        lines = [f"{me.first_name or ''} {me.last_name or ''}".strip() or "(без имени)",
                 f"@{me.username}" if me.username else "(нет юзернейма)",
                 f"id {me.id}", f"+{me.phone}" if me.phone else "(телефон скрыт)"]
        if getattr(me, "premium", False):
            lines.append("Premium")
        out["info"] = " | ".join(lines)
        try:
            auths = await client(GetAuthorizationsRequest())
            others = [a for a in auths.authorizations if not getattr(a, "current", False)]
            out["others"] = [f"{a.device_model or '?'} / {a.app_name or '?'} / {a.country or '?'}" for a in others]
        except Exception as e:  # noqa: BLE001
            out["others_err"] = str(e)[:80]
        if kick:
            from accounts import session_guard as sg
            status, killed = await sg.kick_other_sessions(client)
            out["kick"] = {sg.ALL: "чужие сессии завершены", sg.FRESH: "Telegram не даёт (сессия младше 24ч) — повторю позже",
                           sg.PARTIAL: f"завершены не все ({killed})", sg.ERROR: "не удалось"}.get(status, status)
            if status == sg.ALL:
                sg.clear_pending(name)
            elif status in (sg.FRESH, sg.PARTIAL):
                sg.mark_pending(name)
        if spambot:
            try:
                async with client.conversation("@SpamBot", timeout=25) as conv:
                    await conv.send_message("/start")
                    reply = await conv.get_response()
                    out["spambot"] = (reply.raw_text or "").replace(chr(10), " ")[:220]
            except Exception as e:  # noqa: BLE001
                out["spambot"] = f"не удалось спросить @SpamBot: {type(e).__name__}"
    except (AuthKeyUnregisteredError, UserDeactivatedBanError) as e:
        out["info"] = f"НЕ авторизован / забанен ({type(e).__name__})"
    except Exception as e:  # noqa: BLE001
        out["info"] = f"ошибка подключения: {type(e).__name__}: {str(e)[:100]}"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        session_lock.release(name)
    return out


async def main(argv: list[str]) -> None:
    spambot = "--spambot" in argv
    kick = "--kick" in argv
    names = [a.lstrip("+") if a.lstrip("+").isdigit() and not os.path.exists(os.path.join(SESSIONS_DIR, a + ".session"))
             else a for a in argv if not a.startswith("--")]
    if not names:
        print("Укажи имя аккаунта (как файл в sessions/ без .session). Пример: python check_account.py 18325147520")
        return
    for n in names:
        r = await check(n, spambot, kick=kick)
        print(f"{'✓' if r['ok'] else '✗'} {n}: {r['info']}")
        if r["ok"]:
            others = r.get("others")
            if others is not None:
                print(f"    другие устройства в аккаунте: {len(others)}" + (f" -> {'; '.join(others[:6])}" if others else ""))
            if "others_err" in r:
                print(f"    список устройств не получен: {r['others_err']}")
            if "kick" in r:
                print(f"    сброс чужих сессий: {r['kick']}")
            if "spambot" in r:
                print(f"    @SpamBot: {r['spambot']}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
