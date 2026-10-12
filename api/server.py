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
from data.inbox import init_inbox
from optimizer import health as optimizer_health   # не "health": ниже есть маршрут /health, он затенял бы модуль

from . import (channel_watch, listener, miniapp, routes_accounts, routes_analytics,
               routes_broadcast, routes_bulk, routes_inbox)
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
            swept = optimizer_health.sweep()
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
    init_inbox()
    migrate_from_files(SESSIONS_DIR, DATA_DIR)
    # 2. чиним старые версии sessions
    migrate_all_sessions()
    # 3. поднимаем все клиенты в пул (get_session_files внутри уже читает из БД)
    # API_ONLY_ACCOUNTS="+380981257112,+380981257044" — подключить только эти аккаунты. Нужно, когда рядом
    # в другом окне идёт прогрев: один аккаунт = одно подключение, иначе конфликт сессий.
    pool.allowed = {a.strip() for a in os.getenv("API_ONLY_ACCOUNTS", "").split(",") if a.strip()} or None
    # По умолчанию мессенджер держит ВСЕ свободные аккаунты (чтобы слушать ответы постоянно) и сам отпускает
    # те, что нужны прогреву (статус «идёт») или арендованы консольной рассылкой (accounts/leases.py), а потом
    # забирает обратно. API_SKIP_WARMUP=0 — не отпускать прогревные (не рекомендуется: будет конфликт сессий).
    skip_warmup = os.getenv("API_SKIP_WARMUP", "1").strip().lower() not in ("0", "false", "no", "off")
    await pool.sync_ownership(skip_warmup)
    # 4. регистрируем аккаунты в аналитике (чтобы у каждого была карточка)
    for name in pool.list_active():
        from data.analytics import ensure_account
        ensure_account(name)
    # 5. вешаем listener (передаст входящие в CRM-webhook если он настроен)
    listener.setup()
    # 6. фоновое обслуживание оптимизатора
    maintenance = asyncio.create_task(_maintenance_loop())
    # 7. Mini App «Клиент в один клик»: таблицы, бот, уведомления. Сбой не должен ронять рассылки.
    try:
        await miniapp.startup()
    except Exception:  # noqa: BLE001
        log.exception("[miniapp] запуск не удался")

    async def _ownership_loop():
        """Каждые ~5 секунд: отдать аккаунты тем, кому нужнее, и подключить освободившиеся."""
        while True:
            await asyncio.sleep(5)
            try:
                await pool.sync_ownership(skip_warmup)
                for name in pool.list_active():
                    from data.analytics import ensure_account
                    ensure_account(name)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("[ownership] iteration failed")

    ownership = asyncio.create_task(_ownership_loop())
    log.info("ready: %d active accounts in pool", len(pool.clients))
    try:
        yield
    finally:
        ownership.cancel()
        maintenance.cancel()
        await miniapp.shutdown()
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
app.include_router(routes_inbox.router)
app.include_router(routes_inbox.page_router)
app.include_router(miniapp.router)


@app.get("/health")
def health() -> dict:
    from . import auth
    return {
        "ok": True,
        # auth всегда обязательна (см. api/auth.py) — тут просто откуда взялся
        # токен: явно из env, или автосгенерирован и лежит в ~/.telegramtool/token
        "auth_source": "env" if os.getenv("API_TOKEN", "").strip() else "auto-generated",
        "webhook_configured": bool(os.getenv("CRM_WEBHOOK_URL")),
        "active_accounts": len(pool.clients),
    }
