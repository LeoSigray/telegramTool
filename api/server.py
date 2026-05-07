"""
FastAPI-сервер для telegramTool.

Запуск:
    export API_TOKEN=<секрет>
    uvicorn api.server:app --host 0.0.0.0 --port 9000

Аутентификация: заголовок `Authorization: Bearer <token>` на всех endpoints.
"""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from accounts.manager import migrate_all_sessions

from . import routes_accounts, routes_broadcast

app = FastAPI(title="telegramTool API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_accounts.router)
app.include_router(routes_broadcast.router)


@app.on_event("startup")
async def _startup() -> None:
    # Поднимаем версию старых .session файлов, иначе Telethon на новых версиях падает
    migrate_all_sessions()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "auth_configured": bool(os.getenv("API_TOKEN")),
    }
