"""Замер скорости поиска чатов через YouTube — без Telegram и без LLM.

Запуск:  .venv/bin/python -m tests.bench_youtube [--impl old|new] [--queries N] [--no-cache]
Печатает: время по шагам, сколько видео/ссылок, сколько байт скачано.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

QUERIES = [
    "таргетированная реклама обучение",
    "как настроить рекламу в телеграм",
    "смм продвижение для начинающих",
    "арбитраж трафика урок",
]


class Log:
    def __init__(self) -> None:
        self.lines = []

    def _p(self, lvl, tag, msg):
        t = f"[{lvl}] {tag}: {msg}"
        self.lines.append(t)
        print(t, flush=True)

    def info(self, tag, msg):
        self._p("i", tag, msg)

    def warn(self, tag, msg):
        self._p("!", tag, msg)

    def error(self, tag, msg):
        self._p("E", tag, msg)


class MemStore:
    """Кеш в памяти — чтобы замер не зависел от диска и прошлых запусков."""

    def __init__(self, enabled: bool = True) -> None:
        self.d = {}
        self.enabled = enabled

    def llm_get(self, key):
        return self.d.get(key) if self.enabled else None

    def llm_put(self, key, data):
        if self.enabled:
            self.d[key] = data


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl", default="old", choices=["old", "new"])
    ap.add_argument("--queries", type=int, default=4)
    ap.add_argument("--videos", type=int, default=12)
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args()

    from parsing.smart.settings import Params

    params = Params(channel="bench")
    params.yt_videos = a.videos
    params.web_ttl_days = 7

    if a.impl == "old":
        from parsing.smart.youtube import YouTubeFinder as F
    else:
        from parsing.smart.youtube import YouTubeFinder as F  # та же (замер старой — через git stash)

    store = MemStore(enabled=not a.no_cache)
    log = Log()
    finder = F(store, params, log)

    qs = QUERIES[: a.queries]
    print(f"\n=== impl={a.impl}  queries={len(qs)}  videos/query={params.yt_videos} ===")
    t0 = time.perf_counter()
    ranked = await finder.run(qs)
    dt = time.perf_counter() - t0

    print(f"\nВРЕМЯ: {dt:.1f} c")
    print(f"НАЙДЕНО каналов: {len(ranked)}")
    print(f"ТОП-10: {', '.join('@' + n for n in ranked[:10])}")
    print(f"blocked={finder.blocked}")
    nb = getattr(finder, "bytes_down", None)
    if nb is not None:
        print(f"СКАЧАНО: {nb / 1e6:.1f} MB   запросов HTTP: {getattr(finder, 'http_calls', '?')}")


if __name__ == "__main__":
    asyncio.run(main())
