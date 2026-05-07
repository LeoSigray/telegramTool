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
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from accounts.manager import migrate_all_sessions

from . import listener, routes_accounts, routes_broadcast
from .client_pool import pool

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. чиним старые версии sessions
    migrate_all_sessions()
    # 2. поднимаем все клиенты в пул
    await pool.start_all()
    # 3. вешаем listener (передаст входящие в CRM-webhook если он настроен)
    listener.setup()
    log.info("ready: %d active accounts in pool", len(pool.clients))
    try:
        yield
    finally:
        await listener.shutdown()
        await pool.shutdown()


app = FastAPI(title="telegramTool API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_accounts.router)
app.include_router(routes_broadcast.router)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "auth_configured": bool(os.getenv("API_TOKEN")),
        "webhook_configured": bool(os.getenv("CRM_WEBHOOK_URL")),
        "active_accounts": len(pool.clients),
    }
