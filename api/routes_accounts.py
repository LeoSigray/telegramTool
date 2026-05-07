import os
import re
import shutil
import tempfile
import zipfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from accounts.manager import check_account, get_session_files
from accounts.tdata_importer import convert_tdata, find_tdata_dirs, has_tgcrypto
from config import SESSIONS_DIR

from .auth import require_token
from .client_pool import pool

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


def _safe_name(raw: str) -> str:
    """Имя для .session: только [\\w], короткое, без путей."""
    s = re.sub(r"[^A-Za-z0-9_+\-]+", "_", raw).strip("_")
    return (s or "imported")[:40]


def _unique_path(directory: str, base: str) -> str:
    """Возвращает имя без коллизии: name, name_2, name_3..."""
    candidate = base
    i = 1
    while os.path.exists(os.path.join(directory, candidate + ".session")):
        i += 1
        candidate = f"{base}_{i}"
    return candidate


@router.post("/import-tdata")
async def import_tdata_zip(file: UploadFile = File(...)) -> dict:
    """
    Принимает zip-архив с одной/несколькими папками tdata, конвертирует в .session
    и кладёт в SESSIONS_DIR. Не перезаписывает существующие — даёт уникальные имена.
    """
    if not has_tgcrypto():
        raise HTTPException(status_code=503, detail="tgcrypto не установлен на сервере")

    name = file.filename or "upload.zip"
    if not name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="ожидаю .zip архив")

    tmp_dir = tempfile.mkdtemp(prefix="tdata_api_")
    try:
        zip_path = os.path.join(tmp_dir, "upload.zip")
        with open(zip_path, "wb") as f:
            content = await file.read()
            f.write(content)

        if not zipfile.is_zipfile(zip_path):
            raise HTTPException(status_code=400, detail="файл не является валидным zip")

        extract_dir = os.path.join(tmp_dir, "x")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                # защита от path traversal
                norm = os.path.normpath(member)
                if norm.startswith("..") or os.path.isabs(norm):
                    raise HTTPException(status_code=400, detail=f"подозрительный путь в zip: {member}")
            zf.extractall(extract_dir)

        accounts = find_tdata_dirs(extract_dir)
        if not accounts and os.path.isfile(os.path.join(extract_dir, "key_datas")):
            base = _safe_name(os.path.splitext(name)[0])
            accounts = [(base, extract_dir)]

        if not accounts:
            return {"total_found": 0, "imported": [], "failed": [], "message": "tdata папки не найдены (нет key_datas)"}

        os.makedirs(SESSIONS_DIR, exist_ok=True)

        imported, failed = [], []
        for raw_name, tpath in accounts:
            base = _safe_name(raw_name)
            session_name = _unique_path(SESSIONS_DIR, base)
            ok, err, info = convert_tdata(session_name, tpath, sessions_dir=SESSIONS_DIR)
            entry = {"name": session_name, "source": raw_name}
            if ok:
                imported.append({**entry, "user_id": info.get("user_id"), "dc_id": info.get("dc_id")})
            else:
                failed.append({**entry, "error": err})

        return {
            "total_found": len(accounts),
            "imported": imported,
            "failed": failed,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


class SendIn(BaseModel):
    chat_id: str   # telegram user/chat id (число строкой)
    text: str


@router.post("/{name}/send")
async def send_via_account(name: str, body: SendIn) -> dict:
    """Отправить сообщение через указанный tool-аккаунт (использует pool-клиент).
    Используется CRM'ом для reply-through по диалогам с tool-shadow аккаунтов.
    """
    client = pool.get(name)
    if client is None:
        raise HTTPException(status_code=404, detail=f"account '{name}' is not active in pool")

    try:
        peer = int(body.chat_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="chat_id must be integer (telegram user id)")

    try:
        msg = await client.send_message(peer, body.text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"send failed: {e}")

    return {
        "ok": True,
        "message_id": str(msg.id),
        "sent_at": (msg.date.isoformat() if msg.date else None),
    }
