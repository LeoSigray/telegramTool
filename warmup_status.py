# -*- coding: utf-8 -*-
"""
Статус прогрева одной командой. ТОЛЬКО ЧИТАЕТ базу — прогрев не останавливает и аккаунты не трогает.

    python warmup_status.py           # сводка + последние действия + ошибки
    python warmup_status.py 30        # то же, но 30 последних действий
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

MSK = timezone(timedelta(hours=3))


def fmt(ts: str | None) -> str:
    """UTC-строка из базы -> 'ЧЧ:ММ UTC (ЧЧ:ММ МСК)'."""
    if not ts:
        return "—"
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return f"{dt:%d.%m %H:%M} UTC ({dt.astimezone(MSK):%H:%M} МСК)"


def ago(ts: str | None, now: datetime) -> str:
    if not ts:
        return "—"
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    mins = int((now - dt).total_seconds() // 60)
    if mins < 60:
        return f"{mins} мин назад"
    if mins < 60 * 48:
        return f"{mins // 60} ч {mins % 60} мин назад"
    return f"{mins // 1440} сут назад"


def _overdue(ts: str, now: datetime, hours: float = 3) -> bool:
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return now - dt > timedelta(hours=hours)


def main() -> None:
    import data.db as db
    from warmup import store
    from warmup.runner import describe_accounts

    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10
    now = datetime.now(timezone.utc)
    rows = describe_accounts(now)
    if not rows:
        print("Ни один аккаунт не стоит на прогреве.")
        return

    print(f"Сейчас: {now:%d.%m %H:%M} UTC ({now.astimezone(MSK):%H:%M} МСК)")
    c = sqlite3.connect(db.DB_PATH)
    c.row_factory = sqlite3.Row
    px = (db.get_proxy() or "")
    print("Прокси:", px.split("@")[-1] if px else "НЕ ЗАДАН")

    names = {"active": "идёт", "paused": "пауза", "done": "ГОТОВ", "dead": "МЁРТВ"}
    ph = {"profile": "профиль", "life": "жизнь", "dialog": "жизнь+диал.", "done": "—"}
    print("\n--- Аккаунты ---")
    print(f"{'аккаунт':<11} {'статус':<7} {'день':<5} {'фаза':<12} {'сделано':>7} {'ждёт':>5} {'ошиб.':>5}  "
          f"последнее действие / ближайшее")
    for r in rows:
        last = c.execute("SELECT ts FROM warmup_log WHERE account=? AND ok=1 ORDER BY id DESC LIMIT 1",
                         (r["account"],)).fetchone()
        nxt = fmt(r["next"][0]) + f" [{r['next'][1]}]" if r["next"] else "—"
        if r["next"] and not r["done"] and _overdue(r["next"][0], now):
            nxt += "  <- просрочено, аккаунт не подключается (мёртв?)"
        print(f"{r['account']:<11} {names.get(r['state'], r['state']):<7} {str(r['day']) + '/' + str(r['total_days']):<5} "
              f"{ph.get(r['phase'], r['phase']):<12} {r['done']:>7} {r['pending']:>5} {r['failed']:>5}  "
              f"последнее: {ago(last['ts'] if last else None, now)}; ближайшее: {nxt}")

    print(f"\n--- Последние {limit} действий ---")
    for r in c.execute("SELECT ts, account, action, ok, detail FROM warmup_log ORDER BY id DESC LIMIT ?", (limit,)):
        mark = "✓" if r["ok"] else "✗"
        print(f" {mark} {fmt(r['ts'])}  {r['account']}  {r['action']}: {(r['detail'] or '')[:80]}")

    bad = c.execute("""SELECT account, kind, attempts, substr(error,1,90) e FROM warmup_tasks
                       WHERE status='failed' ORDER BY id DESC LIMIT 8""").fetchall()
    print("\n--- Проваленные задачи ---" if bad else "\nПроваленных задач нет.")
    for r in bad:
        print(f" ✗ {r['account']} {r['kind']}: {r['e']}")

    # Подсказка: если ждут аккаунты, но давно ничего не делали — вероятно, ночное окно по их времени
    active = [r for r in rows if r["state"] == "active" and r["next"] and not _overdue(r["next"][0], now)]
    if active:
        soonest = min(r["next"][0] for r in active)
        print(f"\nБлижайшая задача у активных: {fmt(soonest)}. Ночью по времени аккаунтов (США) "
              f"прогрев намеренно молчит.")


if __name__ == "__main__":
    main()
