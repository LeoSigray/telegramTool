"""Найти рабочую модель Gemini через OpenAI-совместимый эндпоинт (его умеет api/llm.py)."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

KEY = os.getenv("GEMINI_API_KEY", "").strip()
OAI = "https://generativelanguage.googleapis.com/v1beta/openai"

CANDIDATES = ["gemini-3.5-flash", "gemini-flash-latest", "gemini-3-flash-preview",
              "gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-2.5-flash-lite",
              "gemini-3.1-flash-lite", "gemini-pro-latest", "gemini-2.5-pro"]


def try_model(model: str) -> tuple[bool, str, float]:
    payload = {"model": model, "max_tokens": 120,
               "messages": [{"role": "user", "content":
                             'Дай ровно 3 коротких поисковых запроса про обучение нейросетям. '
                             'Верни СТРОГО JSON-массив строк без markdown.'}]}
    req = urllib.request.Request(f"{OAI}/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {KEY}"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
        txt = body["choices"][0]["message"]["content"].strip()
        return True, txt[:110].replace("\n", " "), time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        msg = e.read().decode()
        try:
            msg = json.loads(msg[1:] if msg.startswith("[") else msg)
            msg = (msg.get("error") or {}).get("message", "")[:80]
        except Exception:  # noqa: BLE001
            msg = msg[:80]
        return False, f"HTTP {e.code}: {msg}", time.perf_counter() - t0
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:70]}", time.perf_counter() - t0


def main() -> None:
    working = []
    for m in CANDIDATES:
        ok, info, dt = try_model(m)
        print(f"  {'OK  ' if ok else 'нет '} {m:28s} {dt:5.1f}s  {info}")
        if ok:
            working.append((m, dt))
    if not working:
        print("\nни одна модель не ответила"); return
    best = min(working, key=lambda x: x[1])
    print(f"\nрабочих моделей: {len(working)}; самая быстрая — {best[0]} ({best[1]:.1f} c)")
    print("\nстроки для .env:")
    print(f"  OPENAI_BASE_URL={OAI}")
    print(f"  OPENAI_API_KEY={KEY[:6]}…(ключ Gemini)")
    print(f"  LLM_FAST=openai:{best[0]}")
    print(f"  LLM_SMART=openai:{best[0]}")


if __name__ == "__main__":
    main()
