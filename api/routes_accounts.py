import os
import re
import shutil
import tempfile
import zipfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
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


@router.get("/{name}/avatar/{user_id}")
async def get_avatar(name: str, user_id: int):
    client = pool.get(name)
    if client is None:
        raise HTTPException(status_code=404, detail="account not in pool")

    cache_dir = os.path.join("avatars_cache")
    os.makedirs(cache_dir, exist_ok=True)
    dest = os.path.join(cache_dir, f"{name}_{user_id}.jpg")
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return FileResponse(dest, media_type="image/jpeg")

    try:
        entity = await client.get_entity(user_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"resolve: {e}")
    path = await client.download_profile_photo(entity, file=dest)
    if path and os.path.exists(path):
        return FileResponse(path, media_type="image/jpeg")
    return Response(status_code=204)


class DeleteMessagesIn(BaseModel):
    chat_id: str
    message_ids: list[str]


class EditMessageIn(BaseModel):
    chat_id: str
    message_id: str
    text: str


@router.delete("/{name}/messages")
async def delete_messages(name: str, body: DeleteMessagesIn) -> dict:
    client = pool.get(name)
    if client is None:
        raise HTTPException(status_code=404, detail="account not in pool")
    try:
        peer = int(body.chat_id)
        ids = [int(m) for m in body.message_ids]
    except ValueError:
        raise HTTPException(status_code=400, detail="chat_id and message_ids must be ints")
    try:
        await client.delete_messages(peer, ids, revoke=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"delete failed: {e}")
    return {"ok": True}


@router.patch("/{name}/messages")
async def edit_message_endpoint(name: str, body: EditMessageIn) -> dict:
    client = pool.get(name)
    if client is None:
        raise HTTPException(status_code=404, detail="account not in pool")
    try:
        peer = int(body.chat_id)
        mid = int(body.message_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="chat_id and message_id must be ints")
    try:
        await client.edit_message(peer, mid, body.text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"edit failed: {e}")
    return {"ok": True}


@router.delete("/{name}/dialogs/{chat_id}")
async def delete_dialog_endpoint(name: str, chat_id: str) -> dict:
    client = pool.get(name)
    if client is None:
        raise HTTPException(status_code=404, detail="account not in pool")
    try:
        peer = int(chat_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="chat_id must be int")
    try:
        await client.delete_dialog(peer)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"delete dialog failed: {e}")
    return {"ok": True}


# ─────────────────────────── Profile editing ────────────────────────────────


class ProfileIn(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    about: str | None = None


async def _apply_profile(client, body: ProfileIn) -> dict:
    """Применяет UpdateProfileRequest на клиенте. Поля None — не трогаем."""
    from telethon.tl.functions.account import UpdateProfileRequest
    kwargs = {}
    if body.first_name is not None:
        kwargs["first_name"] = body.first_name
    if body.last_name is not None:
        kwargs["last_name"] = body.last_name
    if body.about is not None:
        kwargs["about"] = body.about
    if not kwargs:
        return {"ok": True, "noop": True}
    await client(UpdateProfileRequest(**kwargs))
    me = await client.get_me()
    return {
        "ok": True,
        "first_name": me.first_name,
        "last_name": me.last_name,
    }


@router.get("/{name}/profile")
async def get_profile(name: str) -> dict:
    client = pool.get(name)
    if client is None:
        raise HTTPException(status_code=404, detail="account not in pool")
    from telethon.tl.functions.users import GetFullUserRequest
    me = await client.get_me()
    full = await client(GetFullUserRequest("me"))
    about = ""
    if hasattr(full, "full_user") and getattr(full.full_user, "about", None):
        about = full.full_user.about or ""
    return {
        "name": name,
        "first_name": me.first_name or "",
        "last_name": me.last_name or "",
        "username": me.username,
        "phone": me.phone,
        "about": about,
    }


@router.patch("/{name}/profile")
async def patch_profile(name: str, body: ProfileIn) -> dict:
    client = pool.get(name)
    if client is None:
        raise HTTPException(status_code=404, detail="account not in pool")
    try:
        return await _apply_profile(client, body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{name}/avatar")
async def upload_avatar(name: str, file: UploadFile = File(...)) -> dict:
    client = pool.get(name)
    if client is None:
        raise HTTPException(status_code=404, detail="account not in pool")
    if not (file.filename or "").lower().endswith((".jpg", ".jpeg", ".png")):
        raise HTTPException(status_code=400, detail="only .jpg/.png supported")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename or ".jpg")[1])
    try:
        tmp.write(await file.read())
        tmp.flush()
        tmp.close()
        from telethon.tl.functions.photos import UploadProfilePhotoRequest
        uploaded = await client.upload_file(tmp.name)
        await client(UploadProfilePhotoRequest(file=uploaded))
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    # сбрасываем кеш аватарки
    cache_dir = "avatars_cache"
    if os.path.isdir(cache_dir):
        for fname in os.listdir(cache_dir):
            if fname.startswith(name + "_") or fname == f"{name}_self.jpg":
                try: os.unlink(os.path.join(cache_dir, fname))
                except OSError: pass
    return {"ok": True}


@router.delete("/{name}/avatar")
async def delete_avatar(name: str) -> dict:
    client = pool.get(name)
    if client is None:
        raise HTTPException(status_code=404, detail="account not in pool")
    from telethon.tl.functions.photos import DeletePhotosRequest
    from telethon.tl.types import InputPhoto
    to_delete: list = []
    async for p in client.iter_profile_photos("me", limit=1):
        to_delete.append(InputPhoto(id=p.id, access_hash=p.access_hash, file_reference=p.file_reference))
    if to_delete:
        await client(DeletePhotosRequest(id=to_delete))
    return {"ok": True, "deleted": len(to_delete)}
