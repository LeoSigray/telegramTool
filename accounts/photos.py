# -*- coding: utf-8 -*-
"""Подготовка фото под аватарки Telegram."""
import os
import random
import tempfile


def ensure_min_photo_size(img_path: str, min_side: int = 800) -> str:
    """
    Гарантирует минимум min_side×min_side и конвертирует в JPEG.
    Telegram принимает аватарки от 800×800 (меньше — 'Photo is too small').
    Всегда возвращает путь к JPEG-файлу (tmp).
    """
    from PIL import Image

    with Image.open(img_path) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if w < min_side or h < min_side:
            scale = min_side / min(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        img.save(tmp.name, "JPEG", quality=95)
        return tmp.name


def variant_image(img_path: str, rng: random.Random | None = None, min_side: int = 800) -> str:
    """
    Уникальная копия фото: лёгкий случайный кадр (до 4% с краёв), чуть другая
    яркость и качество JPEG. Глазом не отличить, но файл/хэш у каждого
    аккаунта свой — иначе на десятке аккаунтов лежит БАЙТ-В-БАЙТ одна и та же
    картинка, а это готовая связка «это одна ферма».
    Возвращает путь к временному JPEG (удалить после загрузки — на вызывающем).
    """
    from PIL import Image, ImageEnhance

    rng = rng or random.Random()
    with Image.open(img_path) as img:
        img = img.convert("RGB")
        w, h = img.size
        dx, dy = int(w * rng.uniform(0, 0.04)), int(h * rng.uniform(0, 0.04))
        l, t = rng.randint(0, dx), rng.randint(0, dy)
        img = img.crop((l, t, w - (dx - l), h - (dy - t)))
        w, h = img.size
        if w < min_side or h < min_side:
            scale = min_side / min(w, h)
            img = img.resize((int(w * scale) + 1, int(h * scale) + 1), Image.LANCZOS)
        img = ImageEnhance.Brightness(img).enhance(rng.uniform(0.96, 1.04))
        img = ImageEnhance.Contrast(img).enhance(rng.uniform(0.97, 1.03))
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        img.save(tmp.name, "JPEG", quality=rng.randint(88, 95))
        return tmp.name


def list_images(folder: str) -> list[str]:
    if not os.path.isdir(folder):
        return []
    return [os.path.join(folder, f) for f in sorted(os.listdir(folder))
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
