# -*- coding: utf-8 -*-
"""Хранилище VK: цели, токены-аккаунты, настройки, ответы.

По умолчанию лежит в общей базе проекта (data/database.db), но на сервере VK живёт отдельно от Mini App:
путь задаёт VK_DB_PATH (например, data/vk.db), и ни один покупатель Mini App этих таблиц не видит."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import data.db as _db

_FMT = "%Y-%m-%dT%H:%M:%S"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ts(dt: datetime | None = None) -> str:
    return (dt or _now()).astimezone(timezone.utc).strftime(_FMT)


def db_path() -> str:
    return os.getenv("VK_DB_PATH", "").strip() or _db.DB_PATH


@contextmanager
def _c():
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    new_file = not os.path.exists(path)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    if new_file:
        try:
            os.chmod(path, 0o600)             # здесь токены и пароли аккаунтов: читать может только владелец
        except OSError:
            pass
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
        CREATE TABLE IF NOT EXISTS vk_settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS vk_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, user_id INTEGER,
            msg_id INTEGER, text TEXT, ts TEXT, notified INTEGER NOT NULL DEFAULT 0,
            UNIQUE(account_id, msg_id));
        """)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn) -> None:
    """Колонки, которых не было в первой версии (старые базы дополняются на лету)."""
    def add(table: str, col: str, kind: str) -> None:
        if col not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
    for col, kind in (("age", "INTEGER"), ("last_seen", "INTEGER"), ("act", "REAL"), ("a_w", "REAL"),
                      ("edu_status", "TEXT"), ("source", "TEXT")):
        add("vk_targets", col, kind)
    add("vk_sends", "variant", "TEXT")


# ── настройки (ссылка-рефералка, лимиты и т. п.) ────────────────────────────

def get_setting(key: str, default: str | None = None) -> str | None:
    with _c() as c:
        row = c.execute("SELECT value FROM vk_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _c() as c:
        c.execute("INSERT INTO vk_settings(key,value) VALUES (?,?)"
                  " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


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
                    r.get("university"), r.get("start_year"), r.get("age"), r.get("last_seen"),
                    r.get("act"), r.get("a_w"), r.get("edu_status"))
            if exists:
                c.execute("""UPDATE vk_targets SET first_name=?, last_name=?, communities=?, a=?, b=?,
                             score=?, university=?, start_year=?, age=?, last_seen=?, act=?, a_w=?,
                             edu_status=?, collected_at=? WHERE user_id=?""",
                          (*vals, now, r["user_id"]))
                upd += 1
            else:
                c.execute("""INSERT INTO vk_targets(first_name,last_name,communities,a,b,score,
                             university,start_year,age,last_seen,act,a_w,edu_status,collected_at,user_id)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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


def log_send(account_id: int, user_id: int, status: str, error: str | None = None,
             variant: str | None = None) -> None:
    with _c() as c:
        c.execute("INSERT INTO vk_sends(account_id,user_id,status,error,ts,variant) VALUES (?,?,?,?,?,?)",
                  (account_id, user_id, status, error, ts(), variant))


def sends_today() -> dict[str, int]:
    """Сколько сообщений за сутки по статусам (sent/closed/failed) — для сводки."""
    since = ts(_now() - timedelta(hours=24))
    with _c() as c:
        return {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM vk_sends WHERE ts>=? GROUP BY status", (since,))}


def variant_stats() -> list[dict]:
    """По вариантам текста: сколько отправлено и сколько ответили (ответ на сообщение этому человеку)."""
    with _c() as c:
        return [dict(r) for r in c.execute("""
            SELECT COALESCE(s.variant,'—') variant, COUNT(*) sent,
                   SUM(CASE WHEN EXISTS (SELECT 1 FROM vk_replies r WHERE r.user_id=s.user_id) THEN 1 ELSE 0 END) replied
            FROM vk_sends s WHERE s.status='sent' GROUP BY s.variant ORDER BY sent DESC""")]


# ── ответы ──────────────────────────────────────────────────────────────────

def add_reply(account_id: int, user_id: int, msg_id: int, text: str) -> bool:
    """Новый ответ от человека. False — такой уже видели."""
    with _c() as c:
        cur = c.execute("INSERT OR IGNORE INTO vk_replies(account_id,user_id,msg_id,text,ts) VALUES (?,?,?,?,?)",
                        (account_id, user_id, msg_id, text, ts()))
        return cur.rowcount > 0


def unnotified_replies(limit: int = 50) -> list[dict]:
    with _c() as c:
        return [dict(r) for r in c.execute(
            "SELECT r.*, t.first_name, t.last_name FROM vk_replies r LEFT JOIN vk_targets t ON t.user_id=r.user_id"
            " WHERE r.notified=0 ORDER BY r.id LIMIT ?", (limit,))]


def mark_replies_notified(ids: list[int]) -> None:
    with _c() as c:
        c.executemany("UPDATE vk_replies SET notified=1 WHERE id=?", [(i,) for i in ids])
