"""
Общий пул Telethon-клиентов. По одной сессии = один долгоживущий TelegramClient.

Зачем:
- listener (.events.NewMessage) должен слушать все сессии всё время.
- broadcast (chat/dm) использует ТЕ ЖЕ клиенты, чтобы не было конфликта sqlite-lock
  на .session-файле и не возникало двух одновременных подключений к одному DC.
"""
import asyncio
import glob
import logging
import os
import time

from telethon import TelegramClient

from accounts.manager import create_client, get_session_files

log = logging.getLogger(__name__)


class ClientPool:
    def __init__(self) -> None:
        self.clients: dict[str, TelegramClient] = {}
        self.unauthorized: set[str] = set()
        self.busy: set[str] = set()        # заняты другим процессом (прогрев/рассылка/бэкенд в другом окне)
        self.allowed: set[str] | None = None   # если задано — работаем только с этими аккаунтами (API_ONLY_ACCOUNTS)
        self._next_try: dict[str, float] = {}  # когда снова пробовать подключить аккаунт (sync_ownership)
        self._catchup_task = None
        self._handlers: list[tuple[callable, object]] = []  # (callback, telethon event filter)
        self._lock = asyncio.Lock()
        self._watchdog = None   # фоновая проверка прокси (proxy_watch.Watchdog)
        self._kick_task = None  # фоновый сброс чужих сессий по истечении 24 часов

    @staticmethod
    def session_name(path: str) -> str:
        return os.path.splitext(os.path.basename(path))[0]

    async def start_all(self, only: set[str] | None = None, on_progress=None) -> None:
        """
        Поднимает все .session из sessions/. Неавторизованные пропускает (без падения).

        only — если задан, поднимаются ТОЛЬКО эти аккаунты (режим тестирования
        конкретных аккаунтов). Остальные не подключаются вообще: лишние
        подключения — это лишние действия в Telegram, при тесте они не нужны.

        on_progress(i, total, name, status) — необязательный колбэк, зовётся на КАЖДЫЙ
        аккаунт (успех/пропуск/ошибка). Подключение аккаунтов идёт по одному (не
        параллельно) и на прокси через интернет может занять десятки секунд на
        аккаунт — без этого колбэка консоль молчит всё это время (видны только
        log.warning через стандартный stderr-перехват, log.info не виден вообще),
        и человеку кажется, что программа зависла. Сервер (api/server.py) колбэк не
        передаёт — его поведение не меняется.
        """
        proxy_failures: list[str] = []
        import proxy_watch
        from proxy_manager import accounts_without_proxy

        # ЖЁСТКИЙ РЕЖИМ ПРОКСИ: без рабочего прокси аккаунты не подключаются вообще
        # (ни напрямую, ни через VPN компьютера) — программа останавливается.
        names = [self.session_name(p) for p in get_session_files()
                 if only is None or self.session_name(p) in only]
        if accounts_without_proxy(names):
            proxy_watch.die("прокси не задан — подключение аккаунтов без прокси запрещено")
        for url in proxy_watch.distinct_proxies(names):
            ok, _kind, text = await proxy_watch.probe(url)
            if not ok:
                proxy_watch.die(f"прокси {proxy_watch.mask(url)} не работает: {text}")

        candidates = [p for p in get_session_files()
                      if (only is None or self.session_name(p) in only)
                      and self.session_name(p) not in self.clients
                      and self.session_name(p) not in self.unauthorized]
        total = len(candidates)

        async with self._lock:
            for i, path in enumerate(candidates, 1):
                name = self.session_name(path)
                # Один аккаунт = один процесс: если им уже пользуется другое окно/программа — не лезем
                # (иначе «database is locked», обрывы подключений, сброс сессии).
                from accounts import session_lock
                if not session_lock.acquire(name):
                    pid = session_lock.holder_pid(name)
                    who = session_lock.describe_holder(pid)
                    status = f"занят другим процессом ({who}, pid {pid})" if who else (
                        f"занят другим процессом (pid {pid})" if pid else "занят другим процессом")
                    log.warning("[pool] %s — %s — пропущен", name, status)
                    if on_progress:
                        on_progress(i, total, name, status)
                    self.busy.add(name)
                    continue
                client = None
                try:
                    client = create_client(path)
                    await client.connect()
                    if not await client.is_user_authorized():
                        log.warning("[pool] %s — не авторизован, пропуск", name)
                        if on_progress:
                            on_progress(i, total, name, "не авторизован")
                        await client.disconnect()
                        self.unauthorized.add(name)
                        session_lock.release(name)
                        continue
                    self._attach_existing_handlers(client)
                    self.clients[name] = client
                    log.info("[pool] подключен %s", name)
                    if on_progress:
                        on_progress(i, total, name, "подключен")
                    # Догоняем ответы, пришедшие, пока аккаунт никто не слушал (не блокирует подключение)
                    try:
                        from . import inbox_catchup
                        asyncio.get_event_loop().create_task(inbox_catchup.catch_up(client, name))
                    except Exception:  # noqa: BLE001
                        pass
                    try:  # отложенный сброс чужих сессий (после 24ч жизни сессии)
                        from accounts.session_guard import process_pending
                        await process_pending(name, client, log=lambda m: log.info(m))
                    except Exception:  # noqa: BLE001 — не должно мешать запуску
                        log.debug("[pool] отложенный сброс сессий %s не выполнен", name, exc_info=True)
                except Exception as e:  # noqa: BLE001
                    # Одна строка вместо полного traceback: при мёртвом/протухшем
                    # прокси падают ВСЕ аккаунты подряд, и 15 трейсбеков подряд
                    # хоронят собой единственное, что важно — причину.
                    log.warning("[pool] не удалось поднять %s: %s: %s",
                                name, type(e).__name__, e)
                    if on_progress:
                        on_progress(i, total, name, f"ошибка: {type(e).__name__}: {str(e)[:60]}")
                    proxy_failures.append(f"{type(e).__name__}: {e}")
                    log.debug("[pool] подробности по %s", name, exc_info=True)
                    # Недоподключённый клиент надо закрыть, иначе остаются «зависшие» задачи Telethon
                    # (в логе: «Task was destroyed but it is pending») и аккаунт остаётся занятым.
                    if client is not None:
                        try:
                            await asyncio.wait_for(client.disconnect(), 10)
                        except Exception:  # noqa: BLE001
                            pass
                    session_lock.release(name)

        if self.clients:
            from data.db import mark_sessions_in_use
            mark_sessions_in_use(True)          # дальше get_session_files() не трогает открытые файлы сессий
            if self._watchdog is None:
                self._watchdog = proxy_watch.Watchdog(lambda: list(self.clients))
            self._watchdog.start()
            if self._kick_task is None or self._kick_task.done():
                self._kick_task = asyncio.get_event_loop().create_task(self._pending_kicks_loop())
            if self._catchup_task is None or self._catchup_task.done():
                self._catchup_task = asyncio.get_event_loop().create_task(self._catchup_loop())

        # Если ВСЕ упали и причина похожа на прокси — скажем прямо, а не
        # оставим пользователя гадать по десятку одинаковых ошибок.
        if proxy_failures and not self.clients:
            joined = " ".join(proxy_failures).lower()
            if "proxy" in joined or "authentication failure" in joined:
                log.warning(
                    "[pool] ⚠ Похоже, дело в прокси, а не в аккаунтах: "
                    "ни один аккаунт не смог подключиться, и все ошибки — "
                    "прокси (отказ авторизации / таймаут). Проверь, не истёк "
                    "ли срок аренды и не сменились ли логин/пароль: "
                    "меню 1 → 6 → 3 покажет, какой прокси сейчас прописан.")

    async def _pending_kicks_loop(self, every_s: int = 1800) -> None:
        """Раз в полчаса: аккаунтам, у которых прошло 24 часа после входа, завершает чужие сессии."""
        from accounts import session_guard
        while True:
            await asyncio.sleep(every_s)
            try:
                await session_guard.run_due(self.clients, log=lambda m: log.info(m))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — сбой сброса не должен мешать работе
                log.debug("[pool] отложенный сброс сессий не выполнен", exc_info=True)

    async def _catchup_loop(self, every_s: int = 1800) -> None:
        """Раз в полчаса догоняет ответы у всех подключённых аккаунтов (страховка на случай пропущенных событий)."""
        from . import inbox_catchup
        while True:
            await asyncio.sleep(every_s)
            for name, client in list(self.clients.items()):
                await inbox_catchup.catch_up(client, name)

    async def sync_ownership(self, skip_warmup: bool = True, retry_s: float = 60) -> None:
        """
        Для бэкенда (мессенджера), который работает ПОСТОЯННО: держим все свободные аккаунты, чтобы слушать ответы,
        и отпускаем те, что нужны другим — арендованные консольной рассылкой (accounts/leases.py) или
        занятые прогревом. Как только они освободятся, подключаем обратно. Зовётся каждые несколько секунд.
        """
        from accounts import leases
        from config import SESSIONS_DIR
        leased = leases.active()
        warm: set[str] = set()
        if skip_warmup:
            try:
                from warmup import store as _wstore
                warm = {r["account"] for r in _wstore.list_accounts(("active",))}
            except Exception:  # noqa: BLE001
                pass
        for name in list(self.clients):
            if name in leased or name in warm:
                await self.release(name)
                self._next_try[name] = time.monotonic() + 5
                log.info("[pool] %s отпущен: %s", name,
                         f"арендован ({leased[name]})" if name in leased else "нужен прогреву")
        now = time.monotonic()
        names = {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(SESSIONS_DIR, "*.session"))}
        if self.allowed is not None:
            names &= self.allowed
        need = {n for n in names if n not in self.clients and n not in self.unauthorized
                and n not in leased and n not in warm and self._next_try.get(n, 0) <= now}
        if not need:
            return
        for n in need:
            self.busy.discard(n)
        await self.start_all(only=need)
        for n in need:
            if n not in self.clients:
                self._next_try[n] = now + retry_s     # занят/ошибка: пробуем снова не чаще раза в минуту

    async def release(self, name: str) -> None:
        """Отключает ОДИН аккаунт и освобождает его для других окон/программ (остальные продолжают работу)."""
        client = self.clients.pop(name, None)
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 15)
            except Exception:  # noqa: BLE001
                pass
        from accounts import session_lock
        session_lock.release(name)
        self.busy.discard(name)

    def _attach_existing_handlers(self, client: TelegramClient) -> None:
        for cb, ev in self._handlers:
            client.add_event_handler(cb, ev)

    def attach_handler(self, callback, event) -> None:
        """Регистрирует обработчик; применит ко всем уже-подключённым и будущим клиентам."""
        self._handlers.append((callback, event))
        for client in self.clients.values():
            client.add_event_handler(callback, event)

    def get(self, session_name: str) -> TelegramClient | None:
        return self.clients.get(session_name)

    def list_active(self) -> list[str]:
        return sorted(self.clients.keys())

    async def shutdown(self) -> None:
        if self._watchdog:
            self._watchdog.stop()
        if self._kick_task:
            self._kick_task.cancel()
            self._kick_task = None
        if self._catchup_task:
            self._catchup_task.cancel()
            self._catchup_task = None
        from accounts import session_lock
        session_lock.release_all()          # отпускаем аккаунты — ими сможет пользоваться другое окно
        async with self._lock:
            for name, client in list(self.clients.items()):
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self.clients.clear()


pool = ClientPool()
