"""Проверка договора между YouTubeFinder и discovery.py — на живом YouTube, без Telegram.

discovery._web_candidates() после web.run(queries) пользуется ровно этим:
    web.blocked, web.score (dict имя→очки), web.via(name), web.context(name)
Здесь мы убеждаемся, что новый быстрый поиск отдаёт всё это в нужном виде.

Запуск: .venv/bin/python -m tests.check_youtube_contract
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parsing.smart.settings import Params      # noqa: E402
from parsing.smart.store import Store          # noqa: E402
from parsing.smart.youtube import YouTubeFinder  # noqa: E402

QUERIES = ["нейросети для бизнеса обучение", "chatgpt для работы урок"]


class Log:
    def info(self, tag, msg): print(f"  [i] {tag}: {msg}")
    def warn(self, tag, msg): print(f"  [!] {tag}: {msg}")
    def error(self, tag, msg): print(f"  [E] {tag}: {msg}")


def check(cond: bool, what: str) -> bool:
    print(f"  {'OK  ' if cond else 'ПЛОХО'} {what}")
    return cond


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "s.db"))
        params = Params(channel="contract")
        finder = YouTubeFinder(store, params, Log())

        print(f"запросы: {QUERIES}")
        t0 = time.perf_counter()
        ranked = await finder.run(QUERIES)
        dt = time.perf_counter() - t0
        print(f"\nвремя {dt:.1f} c, каналов {len(ranked)}\n")

        ok = True
        ok &= check(isinstance(ranked, list), "run() вернул список")
        ok &= check(len(ranked) > 0, f"нашлись каналы ({len(ranked)})")
        ok &= check(not finder.blocked, "blocked=False при удачном поиске")
        ok &= check(all(isinstance(n, str) and n for n in ranked), "все имена — непустые строки")
        ok &= check(all(not n.startswith("@") for n in ranked),
                    "имена без @ (discovery отдаёт их прямо в client.get_entity)")
        ok &= check(len(set(ranked)) == len(ranked), "без повторов")

        # очки: discovery сортирует кандидатов по web.score
        ok &= check(all(n in finder.score for n in ranked), "у каждого канала есть очки")
        ok &= check(all(finder.score[n] >= 1.0 for n in ranked), "очки >= 1")
        order = [finder.score[n] for n in ranked]
        ok &= check(order == sorted(order, reverse=True), "список отсортирован по убыванию очков")

        # пояснения: уходят в отчёт и в Source.about
        ok &= check(all(isinstance(finder.via(n), str) and finder.via(n) for n in ranked),
                    "via() даёт текст по каждому каналу")
        ok &= check(all("YouTube" in finder.via(n) for n in ranked), "via() упоминает источник")
        ok &= check(all(isinstance(finder.context(n), str) for n in ranked),
                    "context() даёт строку (идёт в Source.about)")

        # один автор — один голос
        dup = [n for n in ranked if len(finder.authors[n]) != int(finder.score[n])]
        ok &= check(not dup, "очки == число разных авторов (один автор — один голос)")

        # повторный раунд не читает сеть заново
        f2 = YouTubeFinder(store, params, Log())
        t0 = time.perf_counter()
        await f2.run(QUERIES)
        ok &= check(time.perf_counter() - t0 < 1.0, "повторный раунд берётся из кеша (<1 c)")

        top = ranked[:5]
        print("\n  примеры:")
        for n in top:
            print(f"    @{n:24s} очки {finder.score[n]:.0f}  {finder.via(n)[:70]}")

        store.close()
        print("\n" + ("ДОГОВОР СОБЛЮДЁН" if ok else "ЕСТЬ НАРУШЕНИЯ"))
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
