"""Лог запуска: печать в консоль, файл .log рядом с Excel и лист «Лог» в отчёте."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime


class RunLog:
    def __init__(self, path: str | None = None, echo: bool = True) -> None:
        self.rows: list[tuple[str, str, str, str]] = []
        self.funnel: "OrderedDict[str, object]" = OrderedDict()
        self.echo = echo
        self.path = path
        self._fh = open(path, "a", encoding="utf-8") if path else None

    def _add(self, level: str, stage: str, msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.rows.append((ts, level, stage, msg))
        line = f"[{ts[11:]}] {level:<5} {stage}: {msg}"
        if self.echo:
            print(line, flush=True)
        if self._fh:
            self._fh.write(f"[{ts}] {level:<5} {stage}: {msg}\n")
            self._fh.flush()

    def info(self, stage: str, msg: str) -> None:
        self._add("INFO", stage, msg)

    def warn(self, stage: str, msg: str) -> None:
        self._add("WARN", stage, msg)

    def error(self, stage: str, msg: str) -> None:
        self._add("ERROR", stage, msg)

    def step(self, name: str, value) -> None:
        """Шаг воронки: сколько осталось после этапа."""
        self.funnel[name] = value

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
