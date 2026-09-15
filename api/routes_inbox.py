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


@page_router.get("/inbox", include_in_schema=False)
def inbox_page() -> FileResponse:
    path = os.path.join(os.path.dirname(__file__), "inbox.html")
    return FileResponse(path, media_type="text/html")
