"""Замер скорости поиска: сколько ждёт покупатель до первого человека.

Главная метрика продукта — НЕ «сколько лидов за прогон», а TTFL (time to first lead):
сколько секунд от нажатия до первого человека, которому можно написать. Покупатель
жмёт кнопку и ждёт одного человека, а не список из пятидесяти.

Работает по логам прогонов (data/smart_leads/*.log), Telegram не трогает.

    .venv/bin/python -m tests.bench_ttfl                 # все логи, сводка
    .venv/bin/python -m tests.bench_ttfl --last          # только последний прогон, с фазами
    .venv/bin/python -m tests.bench_ttfl --file путь.log
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "smart_leads")
LINE = re.compile(r"\[([\d\-: ]+)\]\s+(\w+)\s+(.*)")
PEOPLE = re.compile(r"подходящих людей:\s*(\d+)")
READ = re.compile(r"прочитано источников:\s*(\d+)")

# по какому тексту узнаём конец фазы
PHASES = [
    ("профиль клиента", "профиль: сохранён"),
    ("слова и запросы", "запросов для YouTube"),
    ("проверка слов в Telegram", "слова товара в живых чатах"),
    ("первый поиск YouTube", "YouTube: запросов"),
    ("первая проба чатов", "проба: прошли"),
]


def parse(path: str) -> dict:
    rows = []
    for line in open(path, encoding="utf-8", errors="ignore"):
        m = LINE.match(line.rstrip())
        if m:
            try:
                rows.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"), m.group(2), m.group(3)))
            except ValueError:
                continue
    if not rows:
        return {}
    t0 = rows[0][0]
    out = {"file": os.path.basename(path), "rows": rows, "t0": t0,
           "total": (rows[-1][0] - t0).total_seconds(), "ttfl": None, "people": 0,
           "sources": 0, "error": None, "phases": []}
    for t, lvl, msg in rows:
        dt = (t - t0).total_seconds()
        m = PEOPLE.search(msg)
        if m:
            n = int(m.group(1))
            if n > 0 and out["ttfl"] is None:
                out["ttfl"] = dt
            out["people"] = max(out["people"], n)
        m = READ.search(msg)
        if m:
            out["sources"] = max(out["sources"], int(m.group(1)))
        if lvl == "ERROR" and out["error"] is None:
            out["error"] = msg[:70]
    for name, needle in PHASES:
        for t, _lvl, msg in rows:
            if needle in msg:
                out["phases"].append((name, (t - t0).total_seconds()))
                break
    return out


def show(r: dict) -> None:
    print(f"\n=== {r['file']} ===")
    print(f"  всего работал : {r['total'] / 60:.1f} мин")
    ttfl = r["ttfl"]
    print(f"  TTFL          : {f'{ttfl / 60:.1f} мин ({ttfl:.0f} c)' if ttfl else 'людей не было'}")
    print(f"  людей всего   : {r['people']}   прочитано чатов: {r['sources']}")
    if r["error"]:
        print(f"  УПАЛ          : {r['error']}")
    if r["phases"]:
        print("  фазы (от старта):")
        prev = 0.0
        for name, at in r["phases"]:
            print(f"    {at / 60:5.1f} мин  (+{at - prev:5.0f} c)  {name}")
            prev = at


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--last", action="store_true")
    a = ap.parse_args()

    if a.file:
        files = [a.file]
    else:
        files = sorted(glob.glob(os.path.join(LOG_DIR, "*.log")), key=os.path.getmtime, reverse=True)
        if a.last:
            files = files[:1]
    if not files:
        print("логов нет — сначала запусти поиск"); return 1

    res = [r for r in (parse(f) for f in files) if r]
    for r in res if (a.last or a.file) else res[:8]:
        show(r)

    done = [r for r in res if r["ttfl"]]
    print("\n" + "=" * 54)
    if done:
        ts = sorted(r["ttfl"] for r in done)
        print(f"прогонов с людьми: {len(done)} из {len(res)}")
        print(f"TTFL: лучший {ts[0] / 60:.1f} мин, медиана {ts[len(ts) // 2] / 60:.1f} мин, "
              f"худший {ts[-1] / 60:.1f} мин")
    else:
        print("ни в одном прогоне людей не нашлось")
    bad = [r for r in res if r["error"]]
    if bad:
        print(f"упало прогонов: {len(bad)} ({bad[0]['error']})")
    print("\nЦЕЛЬ: TTFL меньше 60 c. Прогрев (MINIAPP_PREWARM) убирает его из момента\n"
          "нажатия: человек ищется, пока покупатель читает текст рассылки.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
