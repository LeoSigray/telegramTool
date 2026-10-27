# -*- coding: utf-8 -*-
"""Долгие дела панели (подбор ЦА, рассылка) идут в потоке; панель показывает ход и умеет остановить."""
import threading
import time


class Job:
    def __init__(self, kind: str, title: str) -> None:
        self.kind, self.title = kind, title
        self.lines: list[str] = []
        self.done = False
        self.result: str | None = None
        self.error: str | None = None
        self.started = time.time()
        self._stop = threading.Event()

    def log(self, text: str) -> None:
        self.lines.append(str(text).strip())
        del self.lines[:-40]

    def stop(self) -> None:
        self._stop.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def tail(self, n: int = 12) -> str:
        return "\n".join(self.lines[-n:])

    def elapsed(self) -> str:
        s = int(time.time() - self.started)
        return f"{s // 60}:{s % 60:02d}"


class Jobs:
    """Одно дело каждого вида за раз: две рассылки или два подбора одновременно не нужны."""

    def __init__(self) -> None:
        self.active: dict[str, Job] = {}

    def running(self, kind: str) -> Job | None:
        j = self.active.get(kind)
        return j if j and not j.done else None

    def start(self, kind: str, title: str, fn) -> Job:
        """fn(job) -> строка с итогом; исключение — в job.error."""
        if self.running(kind):
            raise RuntimeError("такое дело уже идёт")
        job = Job(kind, title)
        self.active[kind] = job

        def run() -> None:
            try:
                job.result = fn(job)
            except Exception as e:  # noqa: BLE001 — показываем человеку, а не роняем службу
                job.error = f"{type(e).__name__}: {e}"
            finally:
                job.done = True

        threading.Thread(target=run, daemon=True, name=f"vkpanel-{kind}").start()
        return job
