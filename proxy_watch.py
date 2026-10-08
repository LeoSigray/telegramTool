# -*- coding: utf-8 -*-
"""
Жёсткий режим прокси: Telegram-трафик аккаунтов идёт ТОЛЬКО через прокси.

  • Прокси не задан / не распознан  -> клиент не создаётся (proxy_manager.ProxyRequired),
    прямого подключения и «молчаливого» перехода на VPN/IP компьютера не бывает.
  • Прокси сломался во время работы -> программа останавливается с понятной ошибкой
    (die), а не крутится в бесконечных переподключениях и не ходит в обход.

Проверка прокси — TCP-соединение к дата-центру Telegram ЧЕРЕЗ прокси (python-socks),
поэтому от VPN на компьютере результат не зависит.

Ошибки двух видов:
  fatal — прокси ответил отказом (неверный логин/пароль, срок аренды истёк, «general failure»):
          останавливаемся сразу;
  soft  — таймаут/нет сети: может быть обрыв Wi-Fi или сон ноутбука, поэтому ждём
          PROXY_FAIL_GRACE_S подряд, и только если прокси так и не ответил — останавливаемся.
"""
import asyncio
import os
import sys
import time

# Адрес дата-центра Telegram (DC2) — цель проверки; сам запрос ничего не отправляет.
PROBE_HOST, PROBE_PORT = "149.154.167.51", 443
PROBE_TIMEOUT_S = 15
PROBE_INTERVAL_S = 30
PROXY_FAIL_GRACE_S = 120


def _classify(exc: Exception) -> str:
    """'fatal' — прокси явно отказал; 'soft' — не ответил (таймаут/нет сети)."""
    name = type(exc).__name__
    text = str(exc).lower()
    if name in ("ProxyTimeoutError", "TimeoutError") or isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "soft"
    if name == "ProxyConnectionError" or isinstance(exc, (ConnectionError, OSError)):
        return "soft"
    # Остановка СРАЗУ — только когда прокси определённо отказал нам (логин/пароль, запрет, лимит
    # аккаунта). Остальное (в т.ч. «Connection closed unexpectedly», «General failure») бывает
    # разовым сбоем прокси или сети: ждём PROXY_FAIL_GRACE_S и останавливаемся, только если
    # прокси так и не заработал. Раньше любой ProxyError мгновенно убивал программу посреди рассылки.
    if any(k in text for k in ("authentication", "not allowed", "no acceptable", "forbidden", "rejected",
                               "unauthorized", "access denied")):
        return "fatal"
    return "soft"


async def probe(proxy_url: str) -> tuple[bool, str, str]:
    """Проверка одного прокси. Возвращает (ok, вид_ошибки, текст)."""
    try:
        from python_socks.async_.asyncio import Proxy
    except ImportError:
        return False, "fatal", "не установлен python-socks (pip install python-socks[asyncio])"
    from proxy_manager import parse_mtproxy_link
    try:
        mt = parse_mtproxy_link(proxy_url)
        if mt:                                  # MTProxy: достаточно открыть TCP до сервера
            reader, writer = await asyncio.wait_for(asyncio.open_connection(mt[0], mt[1]), PROBE_TIMEOUT_S)
            writer.close()
            return True, "", "ok"
        sock = await asyncio.wait_for(
            Proxy.from_url(proxy_url).connect(dest_host=PROBE_HOST, dest_port=PROBE_PORT),
            PROBE_TIMEOUT_S)
        sock.close()
        return True, "", "ok"
    except Exception as e:  # noqa: BLE001
        return False, _classify(e), f"{type(e).__name__}: {str(e)[:120]}"


def distinct_proxies(accounts) -> list[str]:
    """Все разные прокси, которыми пользуются эти аккаунты (общий + персональные)."""
    from config import load_proxy
    from data.db import get_account_proxy
    out: list[str] = []
    for a in accounts:
        raw = get_account_proxy(a) or load_proxy()
        if raw and raw not in out:
            out.append(raw)
    return out


def mask(url: str) -> str:
    """Для сообщений об ошибках: без пароля."""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.scheme}://{p.hostname}:{p.port}"
    except Exception:  # noqa: BLE001
        return "прокси"


def die(message: str, code: int = 2) -> None:
    """Печатает ошибку и ЗАВЕРШАЕТ процесс. Вызывается из любого потока/задачи."""
    line = "=" * 64
    print(f"\n{line}\n  ОСТАНОВКА: {message}\n"
          f"  Аккаунты не будут подключаться напрямую или через VPN — только через прокси.\n"
          f"  Почини прокси (Telegram → 6) и запусти программу заново.\n{line}", flush=True)
    try:                                         # best-effort: сохранить свежие сессии
        from config import SESSIONS_DIR
        from data.db import sync_all_to_db
        sync_all_to_db(SESSIONS_DIR)
    except Exception:  # noqa: BLE001
        pass
    sys.stdout.flush()
    os._exit(code)


class Watchdog:
    """Фоновая проверка прокси раз в PROBE_INTERVAL_S; при поломке — die()."""

    def __init__(self, accounts_getter, on_fail=die, clock=time.monotonic):
        self._accounts = accounts_getter
        self._on_fail = on_fail
        self._clock = clock
        self._bad_since: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    async def check_once(self) -> None:
        for url in distinct_proxies(self._accounts()):
            ok, kind, text = await probe(url)
            if ok:
                self._bad_since.pop(url, None)
                continue
            if kind == "fatal":
                self._on_fail(f"прокси {mask(url)} отказал: {text}")
                return
            since = self._bad_since.setdefault(url, self._clock())
            if self._clock() - since >= PROXY_FAIL_GRACE_S:
                self._on_fail(f"прокси {mask(url)} не отвечает {int(self._clock() - since)} с подряд: {text}")
                return

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(PROBE_INTERVAL_S)
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — сбой самой проверки не должен валить работу
                pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_event_loop().create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
