# -*- coding: utf-8 -*-
"""
Сброс чужих сессий купленного аккаунта (продавец, панель LZT, прежний владелец).

Telegram не даёт завершать другие сессии, пока текущая младше 24 часов
(FRESH_RESET_AUTHORISATION_FORBIDDEN). Поэтому сразу после входа по коду сброс
часто невозможен. Тогда аккаунт записывается в «отложенные», и сброс повторяется
автоматически при первом подключении аккаунта после того, как сессии исполнилось 24 часа.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import data.db as _db

FRESH_AGE = timedelta(hours=24, minutes=5)
_FMT = "%Y-%m-%dT%H:%M:%S"

ALL, PARTIAL, FRESH, ERROR = "all", "partial", "fresh", "error"


@contextmanager
def _c():
    conn = sqlite3.connect(_db.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS session_kick_pending (
            account TEXT PRIMARY KEY, since TEXT NOT NULL, last_try TEXT, tries INTEGER DEFAULT 0)""")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mark_pending(account: str) -> None:
    """Запомнить: у аккаунта остались чужие сессии, сбросить позже (сессии 24ч+)."""
    with _c() as c:
        c.execute("INSERT OR IGNORE INTO session_kick_pending(account, since) VALUES (?, ?)",
                  (str(account), _now().strftime(_FMT)))


def clear_pending(account: str) -> None:
    with _c() as c:
        c.execute("DELETE FROM session_kick_pending WHERE account=?", (str(account),))


def get_pending(account: str) -> dict | None:
    with _c() as c:
        r = c.execute("SELECT * FROM session_kick_pending WHERE account=?", (str(account),)).fetchone()
        return dict(r) if r else None


async def kick_other_sessions(client) -> tuple[str, int]:
    """
    Завершает все сессии аккаунта, кроме текущей.
    Возвращает (статус, сколько завершено поштучно): ALL / PARTIAL / FRESH / ERROR.
    """
    from telethon.errors import FreshResetAuthorisationForbiddenError
    from telethon.tl.functions.auth import ResetAuthorizationsRequest
    from telethon.tl.functions.account import (
        GetAuthorizationsRequest, ResetAuthorizationRequest,
    )
    try:
        await client(ResetAuthorizationsRequest())
        return ALL, 0
    except FreshResetAuthorisationForbiddenError:
        return FRESH, 0
    except Exception:  # noqa: BLE001 — не вышло скопом, пробуем поштучно
        pass
    try:
        auths = await client(GetAuthorizationsRequest())
    except Exception:  # noqa: BLE001
        return ERROR, 0
    killed = left = 0
    for a in auths.authorizations:
        if getattr(a, "current", False):
            continue
        try:
            await client(ResetAuthorizationRequest(hash=a.hash))
            killed += 1
        except FreshResetAuthorisationForbiddenError:
            return FRESH, killed
        except Exception:  # noqa: BLE001
            left += 1
    return (PARTIAL if left else ALL), killed


async def process_pending(account: str, client, *, now: datetime | None = None,
                          log=print) -> str | None:
    """
    Если по аккаунту есть отложенный сброс и сессии уже 24ч+ — делает его.
    Возвращает статус или None (нечего делать / ещё рано).
    """
    row = get_pending(account)
    if not row:
        return None
    now = now or _now()
    since = datetime.strptime(row["since"], _FMT).replace(tzinfo=timezone.utc)
    if now - since < FRESH_AGE:
        return None
    status, killed = await kick_other_sessions(client)
    with _c() as c:
        c.execute("UPDATE session_kick_pending SET last_try=?, tries=tries+1 WHERE account=?",
                  (now.strftime(_FMT), str(account)))
    if status == ALL:
        clear_pending(account)
        log(f"[session] {account}: чужие сессии сброшены (отложенный сброс)")
    else:
        log(f"[session] {account}: отложенный сброс не удался ({status}), повторю при следующем запуске")
    return status


def list_pending() -> list[dict]:
    with _c() as c:
        return [dict(r) for r in c.execute("SELECT * FROM session_kick_pending ORDER BY since")]


async def run_due(connected: dict, *, now: datetime | None = None, log=print, make_client=None) -> list[str]:
    """
    Выполняет все отложенные сбросы, у которых прошло 24 часа. Аккаунты, которые сейчас
    подключены в пуле (connected: {имя: клиент}), используют свой клиент; остальные
    подключаются на минуту (через прокси) только ради сброса и отключаются.
    Возвращает список аккаунтов, у которых сброс удался.
    """
    now = now or _now()
    done: list[str] = []
    for row in list_pending():
        since = datetime.strptime(row["since"], _FMT).replace(tzinfo=timezone.utc)
        if now - since < FRESH_AGE:
            continue
        acc = row["account"]
        client, temp = connected.get(acc), False
        if client is None:
            from config import SESSIONS_DIR
            import os
            path = os.path.join(SESSIONS_DIR, acc + ".session")
            if not os.path.exists(path):
                clear_pending(acc)               # сессии больше нет — отложенный сброс не нужен
                continue
            if make_client is None:
                from accounts.manager import create_client as make_client
            client, temp = make_client(path), True
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    clear_pending(acc)           # мёртвый аккаунт — сбрасывать нечего
                    await client.disconnect()
                    continue
            except Exception as e:  # noqa: BLE001
                log(f"[session] {acc}: не удалось подключиться для сброса: {type(e).__name__}")
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                continue
        try:
            if await process_pending(acc, client, now=now, log=log) == ALL:
                done.append(acc)
        finally:
            if temp:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
    return done
