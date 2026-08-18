"""Тонкая обёртка над Gemini API. Используется для bulk-генерации имён/фамилий профилей."""
import json
import logging
import os
import re

log = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass

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


async def generate_comment(post_text: str, style_prompt: str) -> str:
    """
    Генерирует осмысленный живой комментарий под пост на основе стиль-промта.
    Возвращает строку — текст комментария.
    """
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY не задан в .env")
    if not post_text or not post_text.strip():
        raise RuntimeError("Пустой текст поста")

    # Обрезаем очень длинные посты чтобы не тратить токены
    post_preview = post_text.strip()[:1500]

    full_prompt = (
        "Ты пишешь комментарий в Telegram под пост в канале.\n\n"
        f"Инструкция по стилю и задаче:\n{style_prompt}\n\n"
        f"Текст поста:\n{post_preview}\n\n"
        "Требования к комментарию:\n"
        "- Отвечай ТОЛЬКО текстом комментария, без кавычек, без пояснений\n"
        "- Комментарий должен быть осмысленным и релевантным посту\n"
        "- 1-3 предложения, живой разговорный язык\n"
        "- Без хэштегов, без ссылок\n"
        "- Не раскрывай что ты ИИ\n"
        "- Соответствуй указанному стилю и задаче"
    )

    try:
        from google import genai as _genai
    except ImportError:
        raise RuntimeError("установи google-genai: pip install google-genai")

    client = _genai.Client(api_key=API_KEY)
    response = client.models.generate_content(model=MODEL, contents=full_prompt)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini вернул пустой ответ")
    return text


async def generate_dm_variants(niche: str, info: str, count: int = 2) -> list[str]:
    """
    Генерирует N РАЗНЫХ по формулировке первых сообщений в ЛС под нишу.

    Варианты должны реально отличаться (разный заход/структура), а не быть
    перефразировкой одного и того же — иначе A/B-тест бандита бессмысленен:
    он будет измерять шум, а не разницу в подходе.
    """
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY не задан в .env")
    if not niche.strip():
        raise RuntimeError("Пустая ниша")
    count = max(1, min(count, 5))

    full_prompt = (
        "Ты пишешь ПЕРВОЕ сообщение в личку в Telegram для холодного контакта.\n\n"
        f"Ниша: {niche}\n"
        f"Доп. информация: {info.strip() or '—'}\n\n"
        f"Сгенерируй {count} РАЗНЫХ по подходу вариантов первого сообщения "
        "(разная структура/заход, не перефразировки друг друга).\n\n"
        "Требования к каждому варианту:\n"
        "- 2-4 предложения, живой разговорный язык, без канцелярита\n"
        "- Без хэштегов, без ссылок, без эмодзи через одно слово\n"
        "- Не звучать как массовая рассылка — обращение к конкретному человеку\n"
        "- Не раскрывать что текст сгенерирован ИИ\n\n"
        "Верни СТРОГО JSON-массив строк, без пояснений и без markdown-обёртки:\n"
        '["вариант 1", "вариант 2", ...]'
    )

    try:
        from google import genai as _genai
    except ImportError:
        raise RuntimeError("установи google-genai: pip install google-genai")

    client = _genai.Client(api_key=API_KEY)
    response = client.models.generate_content(model=MODEL, contents=full_prompt)
    text = _strip_code_fence(response.text or "")
    if not text:
        raise RuntimeError("Gemini вернул пустой ответ")

    try:
        variants = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"Gemini вернул не-JSON: {text[:200]}")

    variants = [v.strip() for v in variants if isinstance(v, str) and v.strip()]
    if not variants:
        raise RuntimeError("Gemini не вернул ни одного варианта")
    return variants


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
