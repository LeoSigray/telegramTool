# -*- coding: utf-8 -*-
"""
«Аренда» аккаунтов между программами.

Мессенджер (бэкенд) работает постоянно и держит все свободные аккаунты, чтобы слушать ответы.
Когда консольной рассылке/прогреву нужны аккаунты, они ЗАНИМАЮТ их через эту таблицу:
мессенджер видит запрос, отпускает аккаунты, а когда рассылка закончила (или пропал пульс),
забирает обратно. Останавливать мессенджер вручную не нужно.

Таблица account_leases в общей БД: account, owner, ts (время последнего пульса арендатора).
Аренда считается живой, пока пульс не старше MAX_AGE_S.
"""
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import data.db as _db

MAX_AGE_S = 120
_FMT = "%Y-%m-%dT%H:%M:%S"


@contextmanager
def _c():
    conn = sqlite3.connect(_db.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS account_leases ("
                     "account TEXT PRIMARY KEY, owner TEXT NOT NULL, ts TEXT NOT NULL)")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ts(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime(_FMT)


def request(accounts, owner: str) -> None:
    """Занять аккаунты за owner (повторный вызов обновляет пульс)."""
    with _c() as c:
        for a in accounts:
            c.execute("INSERT INTO account_leases(account, owner, ts) VALUES (?,?,?) "
                      "ON CONFLICT(account) DO UPDATE SET owner=excluded.owner, ts=excluded.ts",
                      (str(a), owner, _ts()))


def touch(owner: str) -> None:
    """Пульс арендатора: пока вызывается раз в ~30 с, аккаунты остаются за ним."""
    with _c() as c:
        c.execute("UPDATE account_leases SET ts=? WHERE owner=?", (_ts(), owner))


def release(owner: str) -> None:
    """Вернуть аккаунты (мессенджер заберёт их обратно)."""
    with _c() as c:
        c.execute("DELETE FROM account_leases WHERE owner=?", (owner,))


def active(max_age_s: float = MAX_AGE_S, now: datetime | None = None) -> dict[str, str]:
    """{аккаунт: владелец} для аренд с живым пульсом; протухшие удаляются."""
    now = now or datetime.now(timezone.utc)
    limit = _ts(now - timedelta(seconds=max_age_s))
    with _c() as c:
        c.execute("DELETE FROM account_leases WHERE ts < ?", (limit,))
        return {r["account"]: r["owner"] for r in c.execute("SELECT account, owner FROM account_leases")}


def take(accounts, owner: str, wait_s: float = 45, poll_s: float = 1.0, acquire=None, sleep=time.sleep) -> list[str]:
    """
    Занять аккаунты и дождаться, пока мессенджер их отпустит. Возвращает аккаунты, которые так и
    остались занятыми кем-то другим (их пул пропустит с понятным сообщением).
    acquire(name) -> bool — попытка взять файловую блокировку (по умолчанию session_lock.acquire).
    """
    if acquire is None:
        from accounts import session_lock
        acquire = session_lock.acquire
    accounts = list(accounts)
    request(accounts, owner)
    waited = 0.0
    missing = accounts
    while True:
        missing = [a for a in accounts if not acquire(a)]
        if not missing or waited >= wait_s:
            return missing
        sleep(poll_s)
        waited += poll_s
        touch(owner)
