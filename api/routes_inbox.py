"""
api/routes_inbox.py — HTTP для платформы диалогов.

Все /inbox/* под Bearer-токеном. Отдельная страница /inbox (HTML) — без
авторизации, токен вводится/читается в самой странице (как /dashboard).
"""
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from data import inbox
from .auth import require_token
from .inbox_service import InboxError, hydrate, send_reply

router = APIRouter(prefix="/inbox", tags=["inbox"], dependencies=[Depends(require_token)])
page_router = APIRouter(tags=["inbox"])  # без авторизации — только HTML


@router.get("/stats")
def inbox_stats() -> dict:
    return inbox.stats()


@router.get("/conversations")
def conversations(status: str = Query(default="open", pattern="^(open|closed|all)$"),
                  q: str | None = None,
                  limit: int = Query(default=100, ge=1, le=500),
                  offset: int = Query(default=0, ge=0)) -> dict:
    st = "" if status == "all" else status
    return {
        "conversations": inbox.list_conversations(status=st, q=q, limit=limit, offset=offset),
        "stats": inbox.stats(),
    }


@router.get("/conversations/{conv_id}")
async def conversation(conv_id: int, mark_read: bool = True,
                       hydrate_history: bool = True) -> dict:
    conv = inbox.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    hydrate_result = None
    if hydrate_history and inbox.needs_hydration(conv_id):
        try:
            hydrate_result = await hydrate(conv_id)
        except InboxError as e:
            hydrate_result = {"ok": False, "error": str(e)}

    if mark_read:
        inbox.mark_read(conv_id)

    return {
        "conversation": inbox.get_conversation(conv_id),
        "messages": inbox.get_messages(conv_id),
        "hydrate": hydrate_result,
    }


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


@router.post("/conversations/{conv_id}/reply")
async def reply(conv_id: int, body: ReplyIn) -> dict:
    if inbox.get_conversation(conv_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    try:
        return await send_reply(conv_id, body.text)
    except InboxError as e:
        # 409 — «не смогли отправить сейчас» (нет аккаунтов, FloodWait, блок)
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/conversations/{conv_id}/read")
def read(conv_id: int) -> dict:
    if inbox.get_conversation(conv_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    inbox.mark_read(conv_id)
    return {"ok": True}


class StatusIn(BaseModel):
    status: str = Field(pattern="^(open|closed)$")


@router.post("/conversations/{conv_id}/status")
def set_status(conv_id: int, body: StatusIn) -> dict:
    if not inbox.set_status(conv_id, body.status):
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"ok": True, "status": body.status}


@router.post("/conversations/{conv_id}/hydrate")
async def rehydrate(conv_id: int) -> dict:
    if inbox.get_conversation(conv_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    try:
        return await hydrate(conv_id)
    except InboxError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/backfill")
def backfill() -> dict:
    """Посев диалогов из истории рассылок (sends с ответом). Идемпотентно."""
    return inbox.seed_from_sends()


_import_state: dict = {"running": False}
_import_task = None   # ссылка на фоновую задачу, чтобы её не убрал сборщик мусора


async def _run_import(days: float) -> None:
    """Фоновая загрузка старых ответов со всех подключённых сейчас аккаунтов."""
    import asyncio
    import glob
    import time
    from config import SESSIONS_DIR
    from . import inbox_catchup
    from .client_pool import pool
    st = _import_state
    st.update(running=True, days=days, started=time.time(), finished=None, error=None,
              done=0, total=0, current=None, accounts={}, new=0, messages=0, skipped=[],
              seeded=inbox.seed_from_sends())
    try:
        names = pool.list_active()
        st["total"] = len(names)
        all_names = {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(SESSIONS_DIR, "*.session"))}
        st["skipped"] = sorted(all_names - set(names) - pool.unauthorized)   # сейчас заняты рассылкой/прогревом
        for name in names:
            client = pool.clients.get(name)
            if client is None:
                continue
            st["current"] = name
            try:
                r = await inbox_catchup.import_history(client, name, days)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                r = {"error": f"{type(e).__name__}: {str(e)[:100]}"}
            st["accounts"][name] = r
            st["done"] += 1
            st["new"] += r.get("new", 0)
            st["messages"] += r.get("messages", 0)
    except Exception as e:  # noqa: BLE001
        st["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    finally:
        st.update(running=False, current=None, finished=time.time())


@router.post("/import-old")
async def import_old(days: float = Query(default=30, ge=1, le=365)) -> dict:
    """Загрузить в «Диалоги» старые ответы за последние days дней (работает в фоне, прогресс — GET /inbox/import-old)."""
    import asyncio
    global _import_task
    if _import_state.get("running"):
        return {"started": False, **_import_state}
    _import_task = asyncio.get_event_loop().create_task(_run_import(days))
    await asyncio.sleep(0)
    return {"started": True, **_import_state}


@router.get("/import-old")
def import_old_status() -> dict:
    return dict(_import_state)


@page_router.get("/inbox", include_in_schema=False)
def inbox_page() -> FileResponse:
    path = os.path.join(os.path.dirname(__file__), "inbox.html")
    return FileResponse(path, media_type="text/html")
