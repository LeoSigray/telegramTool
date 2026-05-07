"""Тонкая обёртка над Gemini API. Используется для bulk-генерации имён/фамилий профилей."""
import json
import logging
import os
import re

log = logging.getLogger(__name__)

API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def is_configured() -> bool:
    return bool(API_KEY)


def _strip_code_fence(s: str) -> str:
    """Gemini иногда оборачивает JSON в ```json ... ```. Убираем."""
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


async def generate_names(prompt: str, count: int, fields: list[str]) -> list[dict]:
    """
    Возвращает список из count словарей с ключами из fields (first_name/last_name).
    Поднимает RuntimeError если что-то не так.
    """
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY не задан в .env")
    if count <= 0 or count > 200:
        raise RuntimeError("count должен быть 1..200")

    valid = {"first_name", "last_name"}
    bad = set(fields) - valid
    if bad:
        raise RuntimeError(f"unknown fields: {bad}; allowed: {valid}")
    if not fields:
        raise RuntimeError("fields пуст")

    field_desc = ", ".join(fields)
    full_prompt = (
        f"Сгенерируй {count} вариантов профилей. Поля: {field_desc}.\n"
        f"Дополнительные требования от пользователя: {prompt or '(нет)'}\n\n"
        f"Верни СТРОГО JSON-массив без пояснений, без markdown, без ```. "
        f"Пример формата: [{{\"first_name\": \"Иван\", \"last_name\": \"Петров\"}}, ...].\n"
        f"Включай только запрошенные поля. Не добавляй nicknames или username'ы."
    )

    # google-genai (новый SDK) — основной клиент
    try:
        from google import genai
    except ImportError:
        raise RuntimeError("установи google-genai: pip install google-genai")

    client = genai.Client(api_key=API_KEY)
    response = client.models.generate_content(model=MODEL, contents=full_prompt)
    text = response.text or ""
    text = _strip_code_fence(text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini вернул не-JSON: {e}; raw[:200]={text[:200]!r}")

    if not isinstance(data, list):
        raise RuntimeError(f"Ожидался JSON-массив, пришло: {type(data).__name__}")

    out: list[dict] = []
    for item in data[:count]:
        if not isinstance(item, dict):
            continue
        cleaned: dict = {}
        for f in fields:
            v = item.get(f)
            if isinstance(v, str) and v.strip():
                cleaned[f] = v.strip()
        if cleaned:
            out.append(cleaned)
    if not out:
        raise RuntimeError(f"Gemini не вернул валидных записей; raw[:200]={text[:200]!r}")
    return out
