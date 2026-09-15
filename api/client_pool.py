"""
Общий пул Telethon-клиентов. По одной сессии = один долгоживущий TelegramClient.

Зачем:
- listener (.events.NewMessage) должен слушать все сессии всё время.
- broadcast (chat/dm) использует ТЕ ЖЕ клиенты, чтобы не было конфликта sqlite-lock
  на .session-файле и не возникало двух одновременных подключений к одному DC.
"""
import asyncio
import logging
import os

from telethon import TelegramClient

from accounts.manager import create_client, get_session_files

log = logging.getLogger(__name__)


class ClientPool:
    def __init__(self) -> None:
        self.clients: dict[str, TelegramClient] = {}
        self.unauthorized: set[str] = set()
        self._handlers: list[tuple[callable, object]] = []  # (callback, telethon event filter)
        self._lock = asyncio.Lock()

    @staticmethod
    def session_name(path: str) -> str:
        return os.path.splitext(os.path.basename(path))[0]

    async def start_all(self) -> None:
        """Поднимает все .session из sessions/. Неавторизованные пропускает (без падения)."""
        async with self._lock:
            for path in get_session_files():
                name = self.session_name(path)
                if name in self.clients or name in self.unauthorized:
                    continue
                try:
                    client = create_client(path)
                    await client.connect()
                    if not await client.is_user_authorized():
                        log.warning("[pool] %s — не авторизован, пропуск", name)
                        await client.disconnect()
                        self.unauthorized.add(name)
                        continue
                    self._attach_existing_handlers(client)
                    self.clients[name] = client
                    log.info("[pool] подключен %s", name)
                except Exception:  # noqa: BLE001
                    log.exception("[pool] не удалось поднять %s", name)

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
        async with self._lock:
            for name, client in list(self.clients.items()):
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self.clients.clear()


pool = ClientPool()
