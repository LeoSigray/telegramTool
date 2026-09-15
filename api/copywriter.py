"""
api/copywriter.py — генерация текста под задачи проекта поверх api/llm.py.

  generate_comment      — живой комментарий под пост (нейрокомментинг)
  generate_names        — имена/фамилии для профилей (bulk)
  generate_dm_variants  — РАЗНЫЕ по подходу первые сообщения для рассылки (A/B)

Провайдер и модель — из .env (см. api/llm.py). Контракт ошибок прежний:
любая проблема → RuntimeError.
"""
import json
import re

from .llm import complete, config_hint, is_configured, provider  # noqa: F401 re-export

__all__ = ["generate_comment", "generate_names", "generate_dm_variants",
           "is_configured", "config_hint", "provider"]


def _strip_code_fence(s: str) -> str:
    """Модель иногда оборачивает JSON в ```json ... ```. Убираем."""
    s = (s or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_json(text: str):
    try:
        return json.loads(_strip_code_fence(text))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"модель вернула не-JSON: {e}; raw[:200]={text[:200]!r}")


# ──────────────────────────────────────────────────────────────────────────
#  Нейрокомментинг
# ──────────────────────────────────────────────────────────────────────────

async def generate_comment(post_text: str, style_prompt: str) -> str:
    if not post_text or not post_text.strip():
        raise RuntimeError("Пустой текст поста")

    post_preview = post_text.strip()[:1500]
    prompt = (
        "Ты пишешь комментарий в Telegram под пост в канале.\n\n"
        f"Инструкция по стилю и задаче:\n{style_prompt}\n\n"
        f"Текст поста:\n{post_preview}\n\n"
        "Требования к комментарию:\n"
        "- Ответь ТОЛЬКО текстом комментария, без кавычек, без пояснений\n"
        "- Осмысленный и релевантный посту\n"
        "- 1-3 предложения, живой разговорный язык\n"
        "- Без хэштегов, без ссылок\n"
        "- Не раскрывай, что ты ИИ\n"
        "- Соответствуй указанному стилю и задаче"
    )
    text = await complete(prompt, task="fast", max_tokens=512)
    # снимаем обрамляющие кавычки, если модель всё же их поставила
    return text.strip().strip('"').strip("«»").strip()


# ──────────────────────────────────────────────────────────────────────────
#  Имена/фамилии
# ──────────────────────────────────────────────────────────────────────────

async def generate_names(prompt: str, count: int, fields: list[str]) -> list[dict]:
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
        f"Дополнительные требования: {prompt or '(нет)'}\n\n"
        "Верни СТРОГО JSON-массив без пояснений и без markdown-обёртки. "
        'Пример: [{"first_name": "Иван", "last_name": "Петров"}, ...].\n'
        "Включай только запрошенные поля. Без nicknames и username."
    )
    data = _parse_json(await complete(full_prompt, task="fast",
                                      max_tokens=min(8000, 60 + count * 40)))
    if not isinstance(data, list):
        raise RuntimeError(f"ожидался JSON-массив, пришло: {type(data).__name__}")

    out: list[dict] = []
    for item in data[:count]:
        if not isinstance(item, dict):
            continue
        cleaned = {f: item[f].strip() for f in fields
                   if isinstance(item.get(f), str) and item[f].strip()}
        if cleaned:
            out.append(cleaned)
    if not out:
        raise RuntimeError("модель не вернула валидных записей")
    return out


# ──────────────────────────────────────────────────────────────────────────
#  Варианты первого сообщения для рассылки
# ──────────────────────────────────────────────────────────────────────────

async def generate_dm_variants(niche: str, info: str, count: int = 2) -> list[str]:
    """
    N РАЗНЫХ по формулировке первых сообщений в ЛС под нишу. Варианты должны
    реально отличаться заходом/структурой, иначе A/B-тест бандита меряет шум.
    """
    if not niche.strip():
        raise RuntimeError("Пустая ниша")
    count = max(1, min(count, 5))

    full_prompt = (
        "Ты пишешь ПЕРВОЕ сообщение в личку в Telegram для холодного контакта.\n\n"
        f"Ниша: {niche}\n"
        f"Доп. информация: {info.strip() or '—'}\n\n"
        f"Сгенерируй {count} РАЗНЫХ по подходу вариантов (разная структура/заход, "
        "не перефразировки друг друга).\n\n"
        "Требования к каждому:\n"
        "- 2-4 предложения, живой разговорный язык, без канцелярита\n"
        "- Без хэштегов, ссылок, эмодзи через слово\n"
        "- Не звучать как массовая рассылка — обращение к конкретному человеку\n"
        "- Не раскрывать, что текст сгенерирован ИИ\n\n"
        'Верни СТРОГО JSON-массив строк, без пояснений и markdown:\n'
        '["вариант 1", "вариант 2", ...]'
    )
    data = _parse_json(await complete(full_prompt, task="smart", max_tokens=2000))
    variants = [v.strip() for v in data if isinstance(v, str) and v.strip()]
    if not variants:
        raise RuntimeError("модель не вернула ни одного варианта")
    return variants
