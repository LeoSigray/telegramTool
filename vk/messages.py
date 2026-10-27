# -*- coding: utf-8 -*-
"""
Тексты рассылки: привлечение курьеров Яндекс Еды по реферальной ссылке.

Несколько вариантов вместо одного: одинаковый текст всем людям VK замечает и режет сильнее, а по вариантам
видно, какой лучше отвечает (vk_sends.variant + vk_replies). Ссылка не зашита в код: её задаёт панель
(/link) или переменная VK_REF_LINK; пока ссылки нет, рассылка не стартует.

В тексте нет обещаний заработка и неправды: только то, что подтверждается условиями Яндекс Еды (график
выбирает сам курьер, можно совмещать с учёбой), и честная пометка, что ссылка реферальная.
"""
import json
import os
import random

from . import store

# {name} — имя получателя, {link} — реферальная ссылка. Лучше 4–6 вариантов, свои добавляются через панель.
DEFAULT_VARIANTS = [
    ("v1", "Привет, {name}! Подсказать подработку рядом с учёбой? Яндекс Еда набирает курьеров: график выбираешь "
           "сам. Регистрация по моей реферальной ссылке: {link}"),
    ("v2", "{name}, привет! Если ищешь, как подзаработать и не мешать учёбе, у Яндекс Еды есть работа курьером со "
           "свободным графиком. Оформиться можно здесь (моя реферальная ссылка): {link}"),
    ("v3", "Здравствуй, {name}. Делюсь вариантом подработки для студентов: курьер в Яндекс Еде, смены ставишь сам. "
           "Если интересно, вот ссылка на регистрацию, она реферальная: {link}"),
    ("v4", "Привет! Ты же учишься в Питере? Многие ребята подрабатывают курьерами в Яндекс Еде, удобно, когда график "
           "не привязан к парам. Если захочешь попробовать, регистрация по моей реферальной ссылке: {link}"),
    ("v5", "{name}, привет. Нужна подработка? Яндекс Еда ищет курьеров, можно выходить в удобное время. Ссылка на "
           "регистрацию (реферальная): {link}"),
]


def ref_link() -> str:
    return (store.get_setting("ref_link") or os.getenv("VK_REF_LINK", "")).strip()


def set_ref_link(url: str) -> None:
    store.set_setting("ref_link", url.strip())


def variants() -> list[tuple[str, str]]:
    """Активные варианты текста: свои из настроек (JSON) или встроенные."""
    raw = store.get_setting("variants")
    if raw:
        try:
            data = json.loads(raw)
            out = [(str(k), str(t)) for k, t in data if str(t).strip()]
            if out:
                return out
        except (ValueError, TypeError):
            pass
    return list(DEFAULT_VARIANTS)


def set_variants(items: list[tuple[str, str]]) -> None:
    store.set_setting("variants", json.dumps(items, ensure_ascii=False))


def reset_variants() -> None:
    store.set_setting("variants", "")


def render(text: str, target: dict, link: str = "") -> str:
    """{name} -> имя (нет имени — подстановка уходит вместе с пробелом и запятой), {link} -> ссылка."""
    name = (target.get("first_name") or "").strip()
    out = text.replace("{link}", link)
    if name:
        return out.replace("{name}", name)
    out = out.replace("{name}, ", "").replace("{name} ", "").replace(", {name}", "").replace("{name}", "")
    out = out.replace("  ", " ").strip()
    return out[:1].upper() + out[1:] if out else out


def pick(rng: random.Random | None = None, items: list[tuple[str, str]] | None = None) -> tuple[str, str]:
    return (rng or random).choice(items or variants())
