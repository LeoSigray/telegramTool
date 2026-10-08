# -*- coding: utf-8 -*-
"""
SQLite-хранилище прогрева (тот же файл data/database.db, свои таблицы).

Принципы:
  • вся очередь живёт в БД — прогрев переживает перезапуск тула;
  • задача, взятая в работу, помечается 'running'; при старте такие
    сбрасываются в 'pending' (процесс мог упасть посреди действия);
  • время — UTC, строкой фиксированного формата, чтобы сравнивалось как текст.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import data.db as _db

_FMT = "%Y-%m-%dT%H:%M:%S"


def ts(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_FMT)


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, _FMT).replace(tzinfo=timezone.utc)


@contextmanager
def _c():
    # DB_PATH читаем в момент вызова (а не при импорте) — иначе тесты и
    # возможная смена пути не подхватились бы.
    conn = sqlite3.connect(_db.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def ensure_schema() -> None:
    with _c() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS warmup_accounts (
            account     TEXT PRIMARY KEY,
            state       TEXT NOT NULL DEFAULT 'active',   -- active|paused|done|dead
            started_at  TEXT NOT NULL,
            tz_offset_h REAL NOT NULL DEFAULT -5,
            dialogs     INTEGER NOT NULL DEFAULT 1,
            set_name    INTEGER NOT NULL DEFAULT 1,
            phone       TEXT,
            user_id     INTEGER,
            note        TEXT
        );
        CREATE TABLE IF NOT EXISTS warmup_tasks (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            account   TEXT NOT NULL,
            day       INTEGER NOT NULL,
            kind      TEXT NOT NULL,
            due_at    TEXT NOT NULL,
            status    TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed|skipped
            payload   TEXT NOT NULL DEFAULT '{}',
            group_key TEXT,
            seq       INTEGER NOT NULL DEFAULT 0,
            attempts  INTEGER NOT NULL DEFAULT 0,
            error     TEXT,
            done_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_wt_due ON warmup_tasks(status, due_at);
        CREATE INDEX IF NOT EXISTS ix_wt_acc_day ON warmup_tasks(account, day);
        CREATE TABLE IF NOT EXISTS warmup_subs (
            account TEXT NOT NULL, channel TEXT NOT NULL, ts TEXT NOT NULL,
            PRIMARY KEY (account, channel)
        );
        CREATE TABLE IF NOT EXISTS warmup_pairs (
            a TEXT NOT NULL, b TEXT NOT NULL, PRIMARY KEY (a, b)
        );
        CREATE TABLE IF NOT EXISTS warmup_msgs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair_key TEXT NOT NULL, sender TEXT NOT NULL, text TEXT NOT NULL, ts TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS warmup_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, account TEXT, action TEXT NOT NULL,
            ok INTEGER NOT NULL DEFAULT 1, detail TEXT
        );
        CREATE TABLE IF NOT EXISTS account_device (
            account     TEXT PRIMARY KEY,
            model       TEXT NOT NULL,
            system      TEXT NOT NULL,
            app         TEXT NOT NULL,
            lang        TEXT NOT NULL,
            system_lang TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        """)


# ── аккаунты ────────────────────────────────────────────────────────────────

def register_account(account: str, started_at: datetime, tz_offset_h: float,
                     dialogs: bool, set_name: bool) -> None:
    ensure_schema()
    with _c() as c:
        c.execute("""
            INSERT INTO warmup_accounts (account, state, started_at, tz_offset_h, dialogs, set_name)
            VALUES (?, 'active', ?, ?, ?, ?)
            ON CONFLICT(account) DO UPDATE SET
                state='active', started_at=excluded.started_at, tz_offset_h=excluded.tz_offset_h,
                dialogs=excluded.dialogs, set_name=excluded.set_name
        """, (account, ts(started_at), tz_offset_h, int(dialogs), int(set_name)))


def get_account(account: str) -> dict | None:
    ensure_schema()
    with _c() as c:
        r = c.execute("SELECT * FROM warmup_accounts WHERE account=?", (account,)).fetchone()
        return dict(r) if r else None


def list_accounts(states: tuple[str, ...] | None = None) -> list[dict]:
    ensure_schema()
    with _c() as c:
        q = "SELECT * FROM warmup_accounts"
        args: tuple = ()
        if states:
            q += f" WHERE state IN ({','.join('?' * len(states))})"
            args = states
        return [dict(r) for r in c.execute(q + " ORDER BY account", args)]


def set_account(account: str, **fields) -> None:
    if not fields:
        return
    ensure_schema()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _c() as c:
        c.execute(f"UPDATE warmup_accounts SET {cols} WHERE account=?",
                  (*fields.values(), account))


def remove_account(account: str) -> None:
    """Полностью забыть аккаунт: состояние, задачи, подписки, пары, переписка."""
    ensure_schema()
    with _c() as c:
        c.execute("DELETE FROM warmup_accounts WHERE account=?", (account,))
        c.execute("DELETE FROM warmup_tasks WHERE account=?", (account,))
        c.execute("DELETE FROM warmup_subs WHERE account=?", (account,))
        c.execute("DELETE FROM warmup_pairs WHERE a=? OR b=?", (account, account))
        c.execute("DELETE FROM warmup_msgs WHERE pair_key LIKE ?", (f"%{account}%",))


# ── задачи ──────────────────────────────────────────────────────────────────

def add_tasks(tasks: list[dict]) -> int:
    """tasks: [{account, day, kind, due_at(datetime), payload(dict), group_key, seq}]"""
    if not tasks:
        return 0
    ensure_schema()
    with _c() as c:
        c.executemany("""
            INSERT INTO warmup_tasks (account, day, kind, due_at, payload, group_key, seq)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [(t["account"], t["day"], t["kind"], ts(t["due_at"]),
               json.dumps(t.get("payload") or {}, ensure_ascii=False),
               t.get("group_key"), t.get("seq", 0)) for t in tasks])
    return len(tasks)


def reset_running() -> int:
    """Старт процесса: всё, что висело 'running', на самом деле не доделано."""
    ensure_schema()
    with _c() as c:
        return c.execute("UPDATE warmup_tasks SET status='pending' WHERE status='running'").rowcount


def claim_due(now: datetime, accounts: set[str] | None = None) -> list[dict]:
    """Берёт в работу все созревшие pending-задачи (помечает running)."""
    ensure_schema()
    with _c() as c:
        rows = c.execute(
            "SELECT * FROM warmup_tasks WHERE status='pending' AND due_at<=? ORDER BY due_at, id",
            (ts(now),)).fetchall()
        out = []
        for r in rows:
            if accounts is not None and r["account"] not in accounts:
                continue
            c.execute("UPDATE warmup_tasks SET status='running' WHERE id=?", (r["id"],))
            d = dict(r)
            d["payload"] = json.loads(d["payload"] or "{}")
            out.append(d)
        return out


def finish_task(task_id: int, status: str, now: datetime, error: str | None = None) -> None:
    with _c() as c:
        c.execute("UPDATE warmup_tasks SET status=?, done_at=?, error=? WHERE id=?",
                  (status, ts(now), error, task_id))


def reschedule_task(task_id: int, due_at: datetime, *, attempts_inc: int = 0,
                    error: str | None = None, payload: dict | None = None) -> None:
    with _c() as c:
        c.execute("""UPDATE warmup_tasks SET status='pending', due_at=?,
                     attempts=attempts+?, error=? WHERE id=?""",
                  (ts(due_at), attempts_inc, error, task_id))
        if payload is not None:
            c.execute("UPDATE warmup_tasks SET payload=? WHERE id=?",
                      (json.dumps(payload, ensure_ascii=False), task_id))


def skip_pending(account: str, reason: str, now: datetime) -> int:
    with _c() as c:
        return c.execute("""UPDATE warmup_tasks SET status='skipped', error=?, done_at=?
                            WHERE account=? AND status IN ('pending','running')""",
                         (reason, ts(now), account)).rowcount


def has_tasks(account: str, day: int, kinds: tuple[str, ...] | None = None) -> bool:
    ensure_schema()
    with _c() as c:
        q = "SELECT 1 FROM warmup_tasks WHERE account=? AND day=?"
        args: list = [account, day]
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        return c.execute(q + " LIMIT 1", args).fetchone() is not None


def has_group(group_key: str) -> bool:
    ensure_schema()
    with _c() as c:
        return c.execute("SELECT 1 FROM warmup_tasks WHERE group_key=? LIMIT 1",
                         (group_key,)).fetchone() is not None


def group_ready(group_key: str, seq: int) -> bool:
    """Все предыдущие реплики этого захода уже отработаны (не pending/running)?"""
    with _c() as c:
        r = c.execute("""SELECT 1 FROM warmup_tasks WHERE group_key=? AND seq<?
                         AND status IN ('pending','running') LIMIT 1""",
                      (group_key, seq)).fetchone()
        return r is None


def task_counts(account: str) -> dict[str, int]:
    ensure_schema()
    with _c() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM warmup_tasks WHERE account=? GROUP BY status",
                         (account,)).fetchall()
    return {r["status"]: r["n"] for r in rows}


def next_due(account: str) -> tuple[str, str] | None:
    with _c() as c:
        r = c.execute("""SELECT due_at, kind FROM warmup_tasks WHERE account=? AND status='pending'
                         ORDER BY due_at LIMIT 1""", (account,)).fetchone()
        return (r["due_at"], r["kind"]) if r else None


def max_due(account: str, day: int) -> str | None:
    """Самый поздний due_at среди задач аккаунта за день (None — задач нет)."""
    ensure_schema()
    with _c() as c:
        r = c.execute("SELECT MAX(due_at) m FROM warmup_tasks WHERE account=? AND day=?",
                      (account, day)).fetchone()
        return r["m"]


def beat(alive: bool = True) -> None:
    """Пульс цикла прогрева (раз в ~20 с). alive=False — цикл остановлен."""
    with _c() as c:
        c.execute("CREATE TABLE IF NOT EXISTS warmup_loop (id INTEGER PRIMARY KEY CHECK (id=1), ts TEXT)")
        if alive:
            c.execute("INSERT INTO warmup_loop(id, ts) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET ts=excluded.ts", (ts(),))
        else:
            c.execute("DELETE FROM warmup_loop")


def loop_alive(max_age_s: float = 75, now: datetime | None = None) -> bool:
    """Идёт ли сейчас цикл прогрева (в этом или другом окне): пульс не старше max_age_s."""
    with _c() as c:
        c.execute("CREATE TABLE IF NOT EXISTS warmup_loop (id INTEGER PRIMARY KEY CHECK (id=1), ts TEXT)")
        r = c.execute("SELECT ts FROM warmup_loop WHERE id=1").fetchone()
    if not r or not r["ts"]:
        return False
    age = ((now or datetime.now(timezone.utc)) - parse_ts(r["ts"])).total_seconds()
    return age <= max_age_s


def pending_total(account: str) -> int:
    with _c() as c:
        return c.execute("SELECT COUNT(*) n FROM warmup_tasks WHERE account=? "
                         "AND status IN ('pending','running')", (account,)).fetchone()["n"]


def failed_tasks(account: str, limit: int = 5) -> list[dict]:
    with _c() as c:
        return [dict(r) for r in c.execute(
            "SELECT kind, error, done_at FROM warmup_tasks WHERE account=? AND status='failed' "
            "ORDER BY id DESC LIMIT ?", (account, limit))]


# ── подписки, пары, переписка ───────────────────────────────────────────────

def add_sub(account: str, channel: str, now: datetime) -> None:
    ensure_schema()
    with _c() as c:
        c.execute("INSERT OR IGNORE INTO warmup_subs (account, channel, ts) VALUES (?,?,?)",
                  (account, channel, ts(now)))


def get_subs(account: str) -> list[str]:
    ensure_schema()
    with _c() as c:
        return [r["channel"] for r in c.execute(
            "SELECT channel FROM warmup_subs WHERE account=? ORDER BY ts", (account,))]


def pair_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


def set_pairs(pairs: list[tuple[str, str]]) -> None:
    ensure_schema()
    with _c() as c:
        c.executemany("INSERT OR IGNORE INTO warmup_pairs (a, b) VALUES (?,?)",
                      [tuple(sorted(p)) for p in pairs])


def list_pairs() -> list[tuple[str, str]]:
    ensure_schema()
    with _c() as c:
        return [(r["a"], r["b"]) for r in c.execute("SELECT a, b FROM warmup_pairs ORDER BY a, b")]


def paired_accounts() -> set[str]:
    return {x for p in list_pairs() for x in p}


def add_msg(key: str, sender: str, text: str, now: datetime) -> None:
    ensure_schema()
    with _c() as c:
        c.execute("INSERT INTO warmup_msgs (pair_key, sender, text, ts) VALUES (?,?,?,?)",
                  (key, sender, text, ts(now)))


def history(key: str, limit: int) -> list[dict]:
    ensure_schema()
    with _c() as c:
        rows = c.execute("SELECT sender, text FROM warmup_msgs WHERE pair_key=? ORDER BY id DESC LIMIT ?",
                         (key, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


# ── лог ─────────────────────────────────────────────────────────────────────

def log(account: str | None, action: str, ok: bool, detail: str, now: datetime) -> None:
    ensure_schema()
    with _c() as c:
        c.execute("INSERT INTO warmup_log (ts, account, action, ok, detail) VALUES (?,?,?,?,?)",
                  (ts(now), account, action, int(ok), detail[:500]))


def recent_log(limit: int = 30, account: str | None = None) -> list[dict]:
    ensure_schema()
    with _c() as c:
        if account:
            rows = c.execute("SELECT * FROM warmup_log WHERE account=? ORDER BY id DESC LIMIT ?",
                             (account, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM warmup_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
