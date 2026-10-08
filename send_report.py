# -*- coding: utf-8 -*-
"""
Отчёт по рассылке: сколько сообщений отправил каждый аккаунт и кому. Только читает базу.

    python send_report.py                         # все аккаунты за последние 24 часа
    python send_report.py +380981257112           # один аккаунт: итоги и список получателей
    python send_report.py +380981257112 --hours 6 # за последние 6 часов
"""
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")


def main(argv: list[str]) -> None:
    import data.db as db

    hours = 24.0
    if "--hours" in argv:
        i = argv.index("--hours")
        hours = float(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    account = next((a for a in argv if not a.startswith("--")), None)

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    c = sqlite3.connect(db.DB_PATH)
    c.row_factory = sqlite3.Row
    q = "SELECT account, target, status, error, sent_at FROM sends WHERE sent_at >= ?"
    args: list = [since]
    if account:
        q += " AND account = ?"
        args.append(account)
    rows = c.execute(q + " ORDER BY sent_at", args).fetchall()
    if not rows:
        print(f"За последние {hours:g} ч записей о рассылке нет" + (f" по {account}." if account else "."))
        return

    by_acc: dict[str, list] = {}
    for r in rows:
        by_acc.setdefault(r["account"], []).append(r)

    print(f"Рассылка за последние {hours:g} ч (время UTC)\n")
    print(f"{'аккаунт':<16} {'отправлено':>10} {'пропущено':>9} {'ошибок':>6}  первое → последнее")
    for acc, rs in by_acc.items():
        cnt = Counter(r["status"] for r in rs)
        print(f"{acc:<16} {cnt.get('sent', 0):>10} {cnt.get('skipped', 0):>9} {cnt.get('failed', 0):>6}  "
              f"{rs[0]['sent_at'][11:16]} → {rs[-1]['sent_at'][11:16]}")
    total = sum(1 for r in rows if r["status"] == "sent")
    print(f"\nВсего отправлено сообщений: {total} (это столько же разных людей: каждому уходит одно)")

    if account:
        rs = by_acc.get(account, [])
        print(f"\nПолучатели аккаунта {account}:")
        for r in rs:
            mark = {"sent": "✓ отправлено", "skipped": "· пропущено", "failed": "✗ ошибка"}.get(r["status"], r["status"])
            extra = f"  ({r['error'][:60]})" if r["error"] else ""
            print(f"  {r['sent_at'][11:19]}  {r['target']:<28} {mark}{extra}")
        errs = Counter((r["error"] or "")[:50] for r in rs if r["status"] != "sent" and r["error"])
        if errs:
            print("\nПричины пропусков/ошибок:")
            for e, n in errs.most_common(5):
                print(f"  {n} × {e}")
    else:
        print("\nПодробный список получателей аккаунта: python send_report.py ИМЯ_АККАУНТА")


if __name__ == "__main__":
    main(sys.argv[1:])
