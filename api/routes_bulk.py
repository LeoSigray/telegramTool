"""Массовые операции по всем active-аккаунтам в пуле."""
import os
import random
import shutil
import tempfile
import zipfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from . import gemini
from .auth import require_token
from .client_pool import pool
from .routes_accounts import ProfileIn, _apply_profile

router = APIRouter(prefix="/accounts/bulk", tags=["bulk"], dependencies=[Depends(require_token)])


@router.post("/profile")
async def bulk_profile(body: ProfileIn) -> dict:
    """Применить first_name/last_name/about ко ВСЕМ активным аккаунтам.
    Если поле None — не трогаем."""
    if body.first_name is None and body.last_name is None and body.about is None:
        raise HTTPException(status_code=400, detail="хотя бы одно из first_name/last_name/about должно быть задано")
    results = []
    for name, client in pool.clients.items():
        try:
            await _apply_profile(client, body)
            results.append({"name": name, "ok": True})
        except Exception as e:  # noqa: BLE001
            results.append({"name": name, "ok": False, "error": str(e)})
    return {"results": results, "total": len(results)}


@router.post("/avatars")
async def bulk_avatars(file: UploadFile = File(...)) -> dict:
    """Принимает ZIP с картинками. Случайно распределяет по активным аккаунтам.
    Если аватарок меньше чем аккаунтов — некоторые будут с одной и той же.
    Если больше — лишние не используются."""
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="ожидаю .zip")

    tmp_dir = tempfile.mkdtemp(prefix="bulk_avatars_")
    try:
        zip_path = os.path.join(tmp_dir, "in.zip")
        with open(zip_path, "wb") as f:
            f.write(await file.read())
        if not zipfile.is_zipfile(zip_path):
            raise HTTPException(status_code=400, detail="невалидный zip")
        out = os.path.join(tmp_dir, "x")
        os.makedirs(out, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                norm = os.path.normpath(member)
                if norm.startswith("..") or os.path.isabs(norm):
                    raise HTTPException(status_code=400, detail=f"подозрительный путь: {member}")
            zf.extractall(out)

        # Собираем все картинки из распакованного дерева
        images: list[str] = []
        for root, _, files in os.walk(out):
            for fn in files:
                if fn.lower().endswith((".jpg", ".jpeg", ".png")):
                    images.append(os.path.join(root, fn))

        if not images:
            return {"results": [], "total": 0, "message": "В архиве нет .jpg/.png"}

        accounts = list(pool.clients.items())
        if not accounts:
            return {"results": [], "total": 0, "message": "Нет активных аккаунтов"}

        results = []
        from telethon.tl.functions.photos import UploadProfilePhotoRequest
        random.shuffle(images)

        # Распределение: для каждого аккаунта выбираем случайную картинку.
        # Если изображений меньше — повторяем (но рандомно).
        for name, client in accounts:
            img_path = random.choice(images)
            try:
                uploaded = await client.upload_file(img_path)
                await client(UploadProfilePhotoRequest(file=uploaded))
                results.append({"name": name, "ok": True, "image": os.path.basename(img_path)})
            except Exception as e:  # noqa: BLE001
                results.append({"name": name, "ok": False, "error": str(e)})

        # сбрасываем закешированные аватарки на стороне tool
        cache_dir = "avatars_cache"
        if os.path.isdir(cache_dir):
            for fname in os.listdir(cache_dir):
                try: os.unlink(os.path.join(cache_dir, fname))
                except OSError: pass

        return {"results": results, "total": len(results), "images_count": len(images)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


class GenerateNamesIn(BaseModel):
    prompt: str = ""           # пояснения от пользователя ("русские, мужские" и т.д.)
    fields: list[str] = ["first_name", "last_name"]
    apply: bool = True         # сразу применить ко всем аккаунтам?


@router.post("/generate-names")
async def bulk_generate_names(body: GenerateNamesIn) -> dict:
    """Генерирует имена через Gemini и опционально применяет ко всем аккаунтам."""
    if not gemini.is_configured():
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY не задан")

    accounts = list(pool.clients.items())
    if not accounts:
        return {"generated": [], "applied": [], "message": "Нет активных аккаунтов"}

    try:
        generated = await gemini.generate_names(body.prompt, count=len(accounts), fields=body.fields)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"gemini: {e}")

    if not body.apply:
        return {"generated": generated, "applied": [], "accounts": [n for n, _ in accounts]}

    applied: list[dict] = []
    for (name, client), names in zip(accounts, generated):
        profile = ProfileIn(
            first_name=names.get("first_name"),
            last_name=names.get("last_name"),
        )
        try:
            await _apply_profile(client, profile)
            applied.append({"name": name, "ok": True, **names})
        except Exception as e:  # noqa: BLE001
            applied.append({"name": name, "ok": False, "error": str(e), **names})
    return {"generated": generated, "applied": applied, "total": len(applied)}
