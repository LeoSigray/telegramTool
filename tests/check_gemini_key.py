"""Проверка ключа Gemini: живой ли, какие модели доступны, работает ли OpenAI-совместимый режим."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

KEY = os.getenv("GEMINI_API_KEY", "").strip()
OAI = "https://generativelanguage.googleapis.com/v1beta/openai"


def post(url: str, payload: dict, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def main() -> None:
    print(f"GEMINI_MODEL в .env: {os.getenv('GEMINI_MODEL')}")
    if not KEY:
        print("ключа нет"); return
    print(f"ключ: {KEY[:6]}…{KEY[-4:]} ({len(KEY)} симв.)\n")

    # 1) список моделей
    try:
        with urllib.request.urlopen(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={KEY}", timeout=30) as r:
            data = json.load(r)
        names = [m["name"].split("/")[-1] for m in data.get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        print(f"ключ РАБОЧИЙ, моделей доступно: {len(names)}")
        good = [m for m in names if ("flash" in m or "pro" in m)
                and not any(x in m for x in ("vision", "embedding", "tts", "image", "live"))]
        for m in good[:14]:
            print(f"   {m}")
    except urllib.error.HTTPError as e:
        print(f"ключ НЕ работает: HTTP {e.code} {e.read().decode()[:300]}")
        return
    except Exception as e:  # noqa: BLE001
        print(f"не удалось: {type(e).__name__}: {e}")
        return

    # 2) OpenAI-совместимый режим — именно им пользуется api/llm.py через провайдер openai
    for model in ("gemini-2.5-flash", "gemini-2.0-flash", os.getenv("GEMINI_MODEL", "").strip()):
        if not model:
            continue
        code, body = post(f"{OAI}/chat/completions",
                          {"model": model, "max_tokens": 50,
                           "messages": [{"role": "user",
                                         "content": "Верни СТРОГО JSON-массив из 2 строк: [\"а\",\"б\"]"}]},
                          {"Authorization": f"Bearer {KEY}"})
        if code == 200:
            try:
                txt = json.loads(body)["choices"][0]["message"]["content"].strip()
            except Exception:  # noqa: BLE001
                txt = body[:120]
            print(f"\nOpenAI-режим, {model}: РАБОТАЕТ → {txt[:90]}")
            print(f"\nзначит в .env можно прописать:")
            print(f"  OPENAI_BASE_URL={OAI}")
            print(f"  OPENAI_API_KEY=<тот же ключ Gemini>")
            print(f"  LLM_FAST=openai:{model}")
            print(f"  LLM_SMART=openai:{model}")
            return
        print(f"\nOpenAI-режим, {model}: HTTP {code} {body[:160]}")


if __name__ == "__main__":
    main()
