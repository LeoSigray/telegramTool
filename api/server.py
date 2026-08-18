"""
FastAPI-сервер для telegramTool.

Запуск:
    export API_TOKEN=<секрет>
    # опционально (для отправки входящих в CRM):
    export CRM_WEBHOOK_URL=http://127.0.0.1:8000/external/incoming
    export CRM_WEBHOOK_TOKEN=<общий секрет с CRM>
    uvicorn api.server:app --host 0.0.0.0 --port 9000

Аутентификация: заголовок `Authorization: Bearer <token>` на всех endpoints
(кроме /health).
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from accounts.manager import migrate_all_sessions
from config import SESSIONS_DIR, DATA_DIR
from data.analytics import expire_pending, init_analytics
from data.db import init_db, migrate_from_files, sync_all_to_db
from optimizer import health

from . import channel_watch, listener, routes_accounts, routes_analytics, routes_broadcast, routes_bulk
from .client_pool import pool

log = logging.getLogger(__name__)

# Как часто крутить обслуживание: закрывать «протухшие» ожидания ответа
# и пересчитывать множители лимитов аккаунтов.
MAINTENANCE_INTERVAL_SEC = 3600


async def _maintenance_loop() -> None:
    """Фоновое обслуживание оптимизатора."""
    while True:
        try:
            await asyncio.sleep(MAINTENANCE_INTERVAL_SEC)
            expired = expire_pending()
            swept = health.sweep()
            log.info("[maintenance] expired=%d restored=%d throttled=%d",
                     expired, swept["restored"], swept["throttled"])
            try:
                synced = await channel_watch.sync_all_recent()
                if synced:
                    log.info("[maintenance] channel sync: %s",
                             {r["channel"]: r.get("matched", r.get("error")) for r in synced})
            except Exception:  # noqa: BLE001 — синк каналов не должен ронять остальное обслуживание
                log.exception("[maintenance] channel sync failed")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("[maintenance] iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. инициализируем БД и мигрируем старые файлы (если нужно)
    init_db()
    init_analytics()
    migrate_from_files(SESSIONS_DIR, DATA_DIR)
    # 2. чиним старые версии sessions
    migrate_all_sessions()
    # 3. поднимаем все клиенты в пул (get_session_files внутри уже читает из БД)
    await pool.start_all()
    # 4. регистрируем аккаунты в аналитике (чтобы у каждого была карточка)
    for name in pool.list_active():
        from data.analytics import ensure_account
        ensure_account(name)
    # 5. вешаем listener (передаст входящие в CRM-webhook если он настроен)
    listener.setup()
    # 6. фоновое обслуживание оптимизатора
    maintenance = asyncio.create_task(_maintenance_loop())
    log.info("ready: %d active accounts in pool", len(pool.clients))
    try:
        yield
    finally:
        maintenance.cancel()
        await listener.shutdown()
        await pool.shutdown()
        # синхронизируем обновлённые сессии обратно в БД
        synced = sync_all_to_db(SESSIONS_DIR)
        if synced:
            log.info("synced %d sessions back to DB", synced)


app = FastAPI(title="telegramTool API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_accounts.router)
app.include_router(routes_bulk.router)
app.include_router(routes_broadcast.router)
app.include_router(routes_analytics.router)
app.include_router(routes_analytics.page_router)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "auth_configured": bool(os.getenv("API_TOKEN")),
        "webhook_configured": bool(os.getenv("CRM_WEBHOOK_URL")),
        "active_accounts": len(pool.clients),
    }
