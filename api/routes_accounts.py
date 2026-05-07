import os

from fastapi import APIRouter, Depends

from accounts.manager import check_account, get_session_files

from .auth import require_token

router = APIRouter(prefix="/accounts", tags=["accounts"], dependencies=[Depends(require_token)])


@router.get("")
def list_accounts() -> list[dict]:
    sessions = get_session_files()
    return [
        {"name": os.path.splitext(os.path.basename(p))[0], "path": p}
        for p in sessions
    ]


@router.get("/check")
async def check_all_accounts() -> list[dict]:
    """Проверяет авторизацию всех аккаунтов. Тяжёлый запрос — каждый аккаунт коннектится к Telegram."""
    sessions = get_session_files()
    out = []
    for p in sessions:
        ok, info = await check_account(p)
        out.append({
            "name": os.path.splitext(os.path.basename(p))[0],
            "ok": ok,
            "info": info,
        })
    return out
