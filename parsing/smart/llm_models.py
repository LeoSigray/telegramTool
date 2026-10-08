"""Показывает, какие модели доступны вашим ключам LLM (названия, без ключей).

    python -m parsing.smart.llm_models

Нужно, если модель из настроек пропала (ошибка 404 «model does not exist»):
выберите id из списка и впишите в .env, например LLM_FAST=groq:<id>.
"""
import asyncio
import os
import sys


async def _main() -> int:
    from api import llm
    from parsing.smart.llm_chain import list_models

    shown = False
    for prov, (_, key_env) in llm._OPENAI_COMPAT.items():  # noqa: SLF001
        if not key_env or not os.getenv(key_env, "").strip():
            continue
        shown = True
        try:
            ids = await list_models(prov)
        except Exception as e:  # noqa: BLE001
            print(f"\n{prov}: не удалось получить список ({str(e)[:100]})")
            continue
        if prov == "openrouter":
            ids = [i for i in ids if i.endswith(":free")]
            print(f"\n{prov}: бесплатных моделей {len(ids)} (показаны первые 25)")
        else:
            print(f"\n{prov}: моделей {len(ids)}")
        for i in sorted(ids)[:25 if prov == "openrouter" else 60]:
            print(f"  {prov}:{i}")
    if not shown:
        print("В .env нет ключей OpenAI-совместимых провайдеров (OPENROUTER, GROQ, XAI, DEEPSEEK…).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
