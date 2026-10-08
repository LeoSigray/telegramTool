# -*- coding: utf-8 -*-
"""
Проверка нейросети: основная модель и запасные (LLM_FALLBACK). Ключи не печатает.

    python check_llm.py
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")
import config  # noqa: F401  (подхватывает .env)
from api import llm

SYSTEM = ("Ты — Никита. Ты переписываешься в Telegram со знакомым Иваном, вы приятели. Тема: кино. "
          "Пиши как живой человек: коротко (2–12 слов). Верни ТОЛЬКО текст одного сообщения.")


async def main() -> None:
    chain = [("основная", *llm.resolve("fast"))] + [("запасная", p, m) for p, m in llm.fallbacks()]
    print(f"Моделей в цепочке: {len(chain)}\n")
    for role, prov, model in chain:
        start = time.time()
        try:
            text = await llm._complete_one(prov, model, "Начни разговор.", system=SYSTEM,
                                           max_tokens=80, effort="low")
            print(f"✓ {role}: {prov}:{model} — {time.time() - start:.1f} с: {text!r}")
        except Exception as e:  # noqa: BLE001
            print(f"✗ {role}: {prov}:{model} — {str(e)[:200]}")


asyncio.run(main())
