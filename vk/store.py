# -*- coding: utf-8 -*-
"""Хранилище VK в общей SQLite-базе (data/database.db): цели и токены-аккаунты."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import data.db as _db

_FMT = "%Y-%m-%dT%H:%M:%S"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ts(dt: datetime | None = None) -> str:
    return (dt or _now()).astimezone(timezone.utc).strftime(_FMT)


@contextmanager
def _c():
    conn = sqlite3.connect(_db.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS vk_targets (
            user_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT,
            communities INTEGER, a REAL, b REAL, score REAL,
            university TEXT, start_year INTEGER,
            status TEXT NOT NULL DEFAULT 'new', error TEXT,
            collected_at TEXT, done_at TEXT, done_by TEXT);
        CREATE TABLE IF NOT EXISTS vk_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL,
            user_id INTEGER, name TEXT, status TEXT NOT NULL DEFAULT 'active',
            rest_until TEXT, added_at TEXT);
        CREATE TABLE IF NOT EXISTS vk_sends (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, user_id INTEGER,
            status TEXT, error TEXT, ts TEXT);
        CREATE TABLE IF NOT EXISTS vk_purchased (
            id INTEGER PRIMARY KEY AUTOINCREMENT, login TEXT NOT NULL, password TEXT,
            extra TEXT, order_id TEXT, product TEXT, cost REAL, bought_at TEXT,
            has_token INTEGER NOT NULL DEFAULT 0, UNIQUE(login, order_id));
        """)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── цели ────────────────────────────────────────────────────────────────────

def save_targets(rows: list[dict]) -> tuple[int, int]:
    """
    Сохраняет подобранную ЦА. Уже обработанные (sent/closed/failed) НЕ воскрешаются:
    у них обновляются только метрики, статус остаётся. Возвращает (новых, обновлённых).
    """
    new = upd = 0
    now = ts()
    with _c() as c:
        for r in rows:
            exists = c.execute("SELECT 1 FROM vk_targets WHERE user_id=?", (r["user_id"],)).fetchone()
            vals = (r["first_name"], r["last_name"], r["communities"], r["a"], r["b"], r["score"],
                    r.get("university"), r.get("start_year"))
            if exists:
                c.execute("""UPDATE vk_targets SET first_name=?, last_name=?, communities=?, a=?, b=?,
                             score=?, university=?, start_year=?, collected_at=? WHERE user_id=?""",
                          (*vals, now, r["user_id"]))
                upd += 1
            else:
                c.execute("""INSERT INTO vk_targets(first_name,last_name,communities,a,b,score,
                             university,start_year,collected_at,user_id) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                          (*vals, now, r["user_id"]))
                new += 1
    return new, upd


def list_targets(status: str | None = "new", limit: int | None = None) -> list[dict]:
    q = "SELECT * FROM vk_targets"
    args: list = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY score DESC, communities DESC, user_id"
    if limit:
        q += " LIMIT ?"
        args.append(int(limit))
    with _c() as c:
        return [dict(r) for r in c.execute(q, args)]


def counts() -> dict[str, int]:
    with _c() as c:
        return {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM vk_targets GROUP BY status")}


def mark_target(user_id: int, status: str, account: str | None = None, error: str | None = None) -> None:
    with _c() as c:
        c.execute("UPDATE vk_targets SET status=?, error=?, done_at=?, done_by=? WHERE user_id=?",
                  (status, error, ts(), account, user_id))


# ── купленные аккаунты VK (логин/пароль; для работы нужен токен) ─────────────

def add_purchased(login: str, password: str, extra: str | None, order_id: str,
                  product: str, cost: float) -> None:
    with _c() as c:
        c.execute("""INSERT OR IGNORE INTO vk_purchased(login,password,extra,order_id,product,cost,bought_at)
                     VALUES (?,?,?,?,?,?,?)""", (login, password, extra, order_id, product, cost, ts()))


def list_purchased(only_without_token: bool = False) -> list[dict]:
    q = "SELECT * FROM vk_purchased"
    if only_without_token:
        q += " WHERE has_token=0"
    with _c() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id")]


def mark_purchased_token(purchased_id: int, has_token: bool = True) -> None:
    with _c() as c:
        c.execute("UPDATE vk_purchased SET has_token=? WHERE id=?", (1 if has_token else 0, purchased_id))


# ── токены (аккаунты VK) ────────────────────────────────────────────────────

def add_account(token: str, user_id: int | None, name: str | None) -> int:
    with _c() as c:
        row = c.execute("SELECT id FROM vk_accounts WHERE token=?", (token,)).fetchone()
        if row:
            c.execute("UPDATE vk_accounts SET user_id=?, name=?, status='active', rest_until=NULL WHERE id=?",
                      (user_id, name, row["id"]))
            return row["id"]
        cur = c.execute("INSERT INTO vk_accounts(token,user_id,name,added_at) VALUES (?,?,?,?)",
                        (token, user_id, name, ts()))
        return cur.lastrowid


def list_accounts() -> list[dict]:
    with _c() as c:
        return [dict(r) for r in c.execute("SELECT * FROM vk_accounts ORDER BY id")]


def delete_account(account_id: int) -> None:
    with _c() as c:
        c.execute("DELETE FROM vk_accounts WHERE id=?", (account_id,))


def set_account_status(account_id: int, status: str, rest_hours: float | None = None) -> None:
    until = ts(_now() + timedelta(hours=rest_hours)) if rest_hours else None
    with _c() as c:
        c.execute("UPDATE vk_accounts SET status=?, rest_until=? WHERE id=?", (status, until, account_id))


def sent_last_24h(account_id: int, now: datetime | None = None) -> int:
    since = ts((now or _now()) - timedelta(hours=24))
    with _c() as c:
        return c.execute("SELECT COUNT(*) n FROM vk_sends WHERE account_id=? AND status='sent' AND ts>=?",
                         (account_id, since)).fetchone()["n"]


def log_send(account_id: int, user_id: int, status: str, error: str | None = None) -> None:
    with _c() as c:
        c.execute("INSERT INTO vk_sends(account_id,user_id,status,error,ts) VALUES (?,?,?,?,?)",
                  (account_id, user_id, status, error, ts()))
