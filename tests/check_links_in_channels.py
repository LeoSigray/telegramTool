"""Проверка идеи: есть ли в постах каналов, найденных через YouTube, ссылки на их ЧАТЫ.

Если да — значит граф по hints дал бы нам живые чаты, но он не запускается,
потому что такие каналы не становятся зёрнами (у них y_est ≈ 0).

Читает только локальную БД парсера, Telegram не трогает.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "smart_parser.db")
TG = re.compile(r"(?<![\w.])(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?"
                r"([A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_])", re.I)
AT = re.compile(r"(?<![\w@./])@([A-Za-z][A-Za-z0-9_]{4,31})(?![\w.@])")
SKIP = {"share", "joinchat", "addstickers", "addlist", "proxy", "iv", "telegram", "durov"}


def main() -> None:
    con = sqlite3.connect(f"file:{os.path.abspath(DB)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    print(f"таблицы: {tables}\n")

    # источники и откуда они взялись
    try:
        srcs = list(con.execute("select * from sources limit 1"))
        cols = srcs[0].keys() if srcs else []
        print(f"колонки sources: {list(cols)}\n")
    except sqlite3.Error as e:
        print("sources недоступна:", e)

    # тексты постов/сообщений
    found_any = False
    for tbl, col in (("messages", "text"), ("posts", "text")):
        if tbl not in tables:
            continue
        try:
            rows = list(con.execute(f"select * from {tbl} limit 1"))
        except sqlite3.Error:
            continue
        if not rows:
            continue
        cols = rows[0].keys()
        textcol = col if col in cols else next((c for c in cols if "text" in c or "message" in c), None)
        if not textcol:
            continue
        found_any = True
        n = con.execute(f"select count(*) from {tbl}").fetchone()[0]
        print(f"=== {tbl}: {n} записей, текст в «{textcol}» ===")
        links = Counter()
        rows = con.execute(f"select {textcol} from {tbl}")
        total = 0
        for (t,) in rows:
            if not t:
                continue
            total += 1
            for m in list(TG.finditer(t)) + list(AT.finditer(t)):
                u = m.group(1).lower()
                if u not in SKIP and not u.endswith("bot"):
                    links[u] += 1
        print(f"текстов просмотрено: {total}")
        print(f"разных упомянутых @имён: {len(links)}")
        chatty = [(u, c) for u, c in links.most_common(40)
                  if any(k in u for k in ("chat", "chd", "club", "community", "obsuzhd", "talk"))]
        print(f"из них похожих на ЧАТ по имени: {len(chatty)}")
        for u, c in chatty[:15]:
            print(f"    @{u:32s} упомянут {c} раз")
        print("  топ упоминаний вообще:")
        for u, c in links.most_common(12):
            print(f"    @{u:32s} {c}")
        print()
    if not found_any:
        print("в БД пока нет текстов — парсер ещё собирает")
    con.close()


if __name__ == "__main__":
    main()
