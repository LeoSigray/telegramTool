"""Цепочка LLM с запасными провайдерами (поверх api/llm.py).

По умолчанию (стоимость 0):
  1. модель из LLM_FAST в .env (в шаблоне — бесплатная модель OpenRouter);
  2. бесплатная модель Groq (если задан GROQ_API_KEY).
Если провайдер упёрся в лимит, перегружен или вернул ошибку, запрос уходит
следующему. Провайдер, ошибившийся два раза подряд, отключается до конца запуска.

Платные провайдеры (xAI, DeepSeek, Anthropic, OpenAI) сами в цепочку НЕ попадают.
Чтобы их подключить, задайте цепочку явно: --llm-chain "openrouter:...,deepseek:deepseek-chat"
или SMART_LLM_CHAIN в .env.
"""
from __future__ import annotations

import asyncio
import os
import re

# У Groq лимит токенов считается отдельно по каждой модели: когда суточный лимит одной
# кончился, следующая работает на своём бесплатном лимите.
FREE_FALLBACKS = ["groq:openai/gpt-oss-120b", "groq:qwen/qwen3.8-27b", "groq:openai/gpt-oss-20b"]
MAX_FAILS = 2

# Если модель из настроек исчезла (404), берём из списка провайдера первую подходящую.
# Для OpenRouter подходят только бесплатные (суффикс :free).
MODEL_PREFS = {
    "groq": ["llama-3.3-70b", "llama-3.1-70b", "gpt-oss-120b", "llama-4", "qwen", "gpt-oss-20b",
             "llama-3.1-8b"],
    "openrouter": ["llama-3.3-70b", "gpt-oss-120b", "nemotron", "qwen", "gemma", "llama"],
}
_RETRY_IN = re.compile(r"try again in\s+((?:\d+(?:\.\d+)?\s*(?:ms|d|h|m|s)\s*)+)", re.IGNORECASE)
_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|d|h|m|s)", re.IGNORECASE)
_UNIT_SEC = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


class DailyLimit(RuntimeError):
    """Лимит провайдера надолго (суточный): ждать нет смысла, модель отключается на этот запуск."""
MAX_RATE_WAIT = 65.0   # дольше не ждём: идём к следующему провайдеру


def rate_limit_wait(err: str) -> float | None:
    """Сколько ждать по ошибке лимита (429 «try again in 3.4s» / «1m2.5s» / «250ms»)."""
    low = err.lower()
    if "429" not in low and "rate limit" not in low:
        return None
    m = _RETRY_IN.search(err)
    if not m:
        return None          # без подсказки не ждём: сразу к следующему провайдеру
    return sum(float(v) * _UNIT_SEC[u.lower()] for v, u in _UNIT.findall(m.group(1)))


_NOT_FOUND = ("404", "does not exist", "model_not_found", "no endpoints found", "not a valid model")


class Chain:
    def __init__(self, specs: list, log=None) -> None:
        self.specs = list(specs)
        self.log = log
        self.fails = {s: 0 for s in self.specs}
        self.used = {s: 0 for s in self.specs}

    def alive(self) -> list:
        return [s for s in self.specs if self.fails[s] < MAX_FAILS]

    def describe(self) -> str:
        return " → ".join(self.specs)

    def _replace(self, old: str, new: str) -> None:
        i = self.specs.index(old)
        self.specs[i] = new
        self.fails[new] = 0
        self.used[new] = self.used.pop(old, 0)
        self.fails.pop(old, None)

    async def _try_other_model(self, spec: str, prompt: str, max_tokens: int):
        """Модель пропала у провайдера: подбираем другую по его же списку и повторяем запрос."""
        alt = await discover_model(spec, self.log)
        if not alt or alt == spec:
            return None, spec
        text = await _call(alt, prompt, max_tokens)  # исключение уйдёт в общую обработку
        self._replace(spec, alt)
        if self.log:
            self.log.info("LLM", f"модель {spec} недоступна, взята {alt}. "
                                 f"Чтобы закрепить, поставьте в .env LLM_FAST={alt}")
        return text, alt

    async def complete(self, prompt: str, max_tokens: int) -> str:
        errors = []
        for spec in self.alive():
            try:
                try:
                    text = await self._call_waiting(spec, prompt, max_tokens)
                except Exception as e:  # noqa: BLE001
                    if not any(k in str(e).lower() for k in _NOT_FOUND):
                        raise
                    text, spec = await self._try_other_model(spec, prompt, max_tokens)
                    if text is None:
                        raise
            except Exception as e:  # noqa: BLE001 — лимит, перегрузка, ключ: идём дальше
                self.fails[spec] = MAX_FAILS if isinstance(e, DailyLimit) else self.fails[spec] + 1
                errors.append(f"{spec}: {str(e)[:140]}")
                if self.log:
                    nxt = [s for s in self.alive() if s != spec]
                    self.log.warn("LLM", f"{spec} не сработал ({str(e)[:140]})"
                                  + (f" → пробуем {nxt[0]}" if nxt else ""))
                    if self.fails[spec] >= MAX_FAILS:
                        self.log.warn("LLM", f"{spec} отключён до конца запуска")
                continue
            self.fails[spec] = 0
            self.used[spec] += 1
            return text
        raise RuntimeError("все LLM-провайдеры недоступны: " + " | ".join(errors or ["цепочка пуста"]))

    async def _call_waiting(self, spec: str, prompt: str, max_tokens: int, tries: int = 3) -> str:
        """Вызов с ожиданием по лимиту: провайдер сам пишет, через сколько повторить."""
        for attempt in range(tries):
            try:
                return await _call(spec, prompt, max_tokens)
            except Exception as e:  # noqa: BLE001
                wait = rate_limit_wait(str(e))
                if wait is not None and wait > MAX_RATE_WAIT:
                    raise DailyLimit(f"{spec}: лимит на {wait / 60:.0f} мин ({str(e)[:100]})") from e
                if wait is None or attempt == tries - 1:
                    raise
                if self.log:
                    self.log.info("LLM", f"{spec}: лимит бесплатного тарифа, жду {wait + 1:.0f} с и повторяю")
                await asyncio.sleep(wait + 1.0)
        raise RuntimeError("недостижимо")

    def usage(self) -> str:
        return ", ".join(f"{s.split(':')[0]} {n}" for s, n in self.used.items() if n) or "ни одного"


async def _call(spec: str, prompt: str, max_tokens: int) -> str:
    """Один вызов конкретного провайдера через внутренние функции api/llm.py."""
    from api import llm

    prov, _, model = spec.partition(":")
    effort = llm._TASK_EFFORT.get("fast")  # noqa: SLF001
    if prov == "claude":
        return await llm._claude_complete(model, prompt, system=None,  # noqa: SLF001
                                          max_tokens=max_tokens, effort=effort)
    if prov in llm._OPENAI_COMPAT:  # noqa: SLF001
        return await llm._openai_compat_complete(prov, model, prompt, system=None,  # noqa: SLF001
                                                 max_tokens=max_tokens, effort=effort)
    raise RuntimeError(f"неизвестный провайдер {prov}")


async def list_models(prov: str) -> list:
    """Список id моделей провайдера (GET /models). Только для OpenAI-совместимых."""
    import httpx
    from api import llm

    base, key_env = llm._OPENAI_COMPAT[prov]  # noqa: SLF001
    headers = {"Authorization": f"Bearer {os.getenv(key_env, '').strip()}"} if key_env else {}
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(f"{base}/models", headers=headers)
    r.raise_for_status()
    data = r.json().get("data") or []
    return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]


async def discover_model(spec: str, log=None) -> str:
    """Подбирает рабочую модель того же провайдера. Пустая строка, если не вышло."""
    prov = spec.partition(":")[0]
    if prov not in MODEL_PREFS:
        return ""
    try:
        ids = await list_models(prov)
    except Exception as e:  # noqa: BLE001
        if log:
            log.warn("LLM", f"список моделей {prov} не получен: {str(e)[:100]}")
        return ""
    if prov == "openrouter":
        ids = [i for i in ids if i.endswith(":free")]
    skip = ("whisper", "guard", "tts", "embed", "vision", "distil", "prompt-guard", "safeguard", "audio")
    ids = [i for i in ids if not any(k in i.lower() for k in skip)]
    for pref in MODEL_PREFS[prov]:
        for i in ids:
            if pref in i.lower():
                return f"{prov}:{i}"
    return ""


def build_chain(override: str = "", log=None) -> tuple[Chain | None, str]:
    """Собирает цепочку из доступных (с ключом) провайдеров. Возвращает (цепочка, описание/причина)."""
    try:
        from api import llm
    except Exception as e:  # noqa: BLE001
        return None, f"api.llm не загрузился: {e}"

    override = (override or os.getenv("SMART_LLM_CHAIN", "")).strip()
    if override:
        wanted = [x.strip() for x in override.split(",") if x.strip()]
        source = "заданная вручную"
    else:
        main = os.getenv("LLM_FAST", "").strip() or llm.DEFAULT_SPEC["fast"]
        wanted = [main] + FREE_FALLBACKS
        source = "бесплатная"

    specs, skipped = [], []
    for spec in wanted:
        prov = spec.partition(":")[0].strip().lower()
        if prov != "claude" and prov not in llm._OPENAI_COMPAT:  # noqa: SLF001
            skipped.append(f"{spec} (неизвестный провайдер)")
        elif not llm._has_key(prov):  # noqa: SLF001
            skipped.append(f"{prov} (нет ключа в .env)")
        elif spec not in specs:
            specs.append(spec)
    if not specs:
        return None, ("нет ни одного ключа LLM в .env: " + "; ".join(skipped)
                      if skipped else "цепочка LLM пуста")
    info = f"{source} цепочка: " + " → ".join(specs)
    if skipped:
        info += "; пропущены: " + ", ".join(skipped)
    return Chain(specs, log), info
