"""
api/llm.py — единый слой обращения к LLM.

Модель на КЛАСС задачи задаётся в .env строкой "провайдер:модель":

  LLM_FAST   массовые генерации — нейрокомментинг, имена
             (по умолчанию openrouter:nvidia/nemotron-3-super-120b-a12b:free)
  LLM_SMART  аналитик текстов рассылки + варианты (нужен ум, запускается редко)
             (по умолчанию openrouter:nvidia/nemotron-3-super-120b-a12b:free)

Провайдеры:
  claude                — anthropic SDK,           ключ ANTHROPIC_API_KEY
  groq / grok /         — OpenAI-совместимый HTTP, ключ <PROVIDER>_API_KEY
  openrouter / deepseek /
  cerebras / openai / ollama

Ключи в .env:
  ANTHROPIC_API_KEY, GROQ_API_KEY (бесплатно), XAI_API_KEY (Grok, платный),
  OPENROUTER_API_KEY, DEEPSEEK_API_KEY, CEREBRAS_API_KEY, OPENAI_API_KEY
  (+ OPENAI_BASE_URL)

Совместимость: если задан старый LLM_PROVIDER=claude — используется он для
обоих классов, с моделями из CLAUDE_MODEL_FAST/SMART.

Дефолт — OpenRouter (бесплатная модель, без карты). Groq пробовали раньше,
но с некоторых сетей до него не достучаться ("All connection attempts
failed") — OpenRouter на своём домене/CDN обычно доступнее. Gemini как
провайдер убран из кода (не было доступа); Grok (xAI) остался как опция, но
платный — не дефолт. Точка входа для смены провайдера в будущем не
меняется — `complete()`.
"""
import logging
import os

log = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass


# ── дефолты и справочник провайдеров ──────────────────────────────────────

DEFAULT_SPEC = {
    "fast": "openrouter:nvidia/nemotron-3-super-120b-a12b:free",
    "smart": "openrouter:nvidia/nemotron-3-super-120b-a12b:free",
}

# провайдер → (base_url, имя env-переменной с ключом | None если ключ не нужен)
_OPENAI_COMPAT = {
    "groq":       ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "grok":       ("https://api.x.ai/v1",            "XAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1",   "OPENROUTER_API_KEY"),
    "deepseek":   ("https://api.deepseek.com",       "DEEPSEEK_API_KEY"),
    "cerebras":   ("https://api.cerebras.ai/v1",     "CEREBRAS_API_KEY"),
    "ollama":     ("http://localhost:11434/v1",      None),
    "openai":     (os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"), "OPENAI_API_KEY"),
}

_KEY_ENV = {"claude": "ANTHROPIC_API_KEY",
            **{p: e for p, (_, e) in _OPENAI_COMPAT.items() if e}}

# effort/reasoning на класс задачи (применяется там, где провайдер поддерживает)
_TASK_EFFORT = {"fast": "low", "smart": "high"}

_LEGACY_PROVIDER = os.getenv("LLM_PROVIDER", "").strip().lower()

_anthropic_client = None


# ── разбор конфига ────────────────────────────────────────────────────────

def _spec(task: str) -> str:
    env_name = "LLM_FAST" if task == "fast" else "LLM_SMART"
    raw = os.getenv(env_name, "").strip()
    if raw:
        return raw
    if _LEGACY_PROVIDER == "claude":
        m = os.getenv("CLAUDE_MODEL_FAST" if task == "fast" else "CLAUDE_MODEL_SMART",
                      "claude-haiku-4-5" if task == "fast" else "claude-sonnet-5")
        return f"claude:{m}"
    return DEFAULT_SPEC[task]


def resolve(task: str) -> tuple[str, str]:
    """(provider, model) для класса задачи."""
    spec = _spec(task)
    provider, _, model = spec.partition(":")
    provider = provider.strip().lower()
    model = model.strip() or spec
    return provider, model


def provider(task: str = "fast") -> str:
    return resolve(task)[0]


def model_for(task: str) -> str:
    return resolve(task)[1]


def _has_key(provider_name: str) -> bool:
    if provider_name == "ollama":
        return True
    env = _KEY_ENV.get(provider_name)
    return bool(env and os.getenv(env, "").strip())


def is_configured() -> bool:
    """Готов ли хотя бы FAST-класс (на нём завязаны нейрокомментинг и имена)."""
    return _has_key(resolve("fast")[0])


def config_hint() -> str:
    p = resolve("fast")[0]
    urls = {
        "grok": "https://console.x.ai",
        "groq": "https://console.groq.com/keys",
        "openrouter": "https://openrouter.ai/keys",
        "deepseek": "https://platform.deepseek.com/api_keys",
        "claude": "https://console.anthropic.com/ → API Keys",
        "cerebras": "https://cloud.cerebras.ai",
    }
    env = _KEY_ENV.get(p, "<PROVIDER>_API_KEY")
    return f"{env} не задан в .env (провайдер {p}). Ключ: {urls.get(p, '—')}"


# ── бэкенды ──────────────────────────────────────────────────────────────

def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            raise RuntimeError("установи anthropic: pip install anthropic")
        _anthropic_client = AsyncAnthropic()
    return _anthropic_client


async def _claude_complete(model: str, prompt: str, *, system: str | None,
                           max_tokens: int, effort: str | None) -> str:
    from anthropic import APIStatusError, APIConnectionError
    kwargs: dict = {"model": model, "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}]}
    if system:
        kwargs["system"] = system
    if effort and not model.startswith("claude-haiku"):
        kwargs["output_config"] = {"effort": effort}
    try:
        resp = await _anthropic().messages.create(**kwargs)
    except APIConnectionError as e:
        raise RuntimeError(f"сеть/Anthropic: {e}")
    except APIStatusError as e:
        raise RuntimeError(f"Anthropic {e.status_code}: {getattr(e, 'message', e)}")
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude отклонил запрос (refusal)")
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    if not text:
        raise RuntimeError("Claude вернул пустой ответ")
    return text


# Сколько раз повторить при временной перегрузке апстрима (не при 4xx —
# та ошибка сама себя не починит повторным запросом) и с какой паузой.
_RETRY_ATTEMPTS = 3
_RETRY_DELAY_SEC = 3.0


def _is_retryable(status_code: int, data: dict | None) -> bool:
    """5xx от самого гейтвея, либо embedded error (иногда OpenRouter/… шлёт
    200 с {"error": {...}} внутри тела вместо HTTP-статуса — как раз тот
    случай "Upstream error from Nvidia: Service temporarily overloaded")."""
    if status_code >= 500:
        return True
    err = (data or {}).get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return False
    code = err.get("code")
    if isinstance(code, int) and code >= 500:
        return True
    reason = f"{err.get('type', '')} {err.get('message', '')}".lower()
    meta_type = str((err.get("metadata") or {}).get("error_type", "")).lower()
    return any(kw in reason or kw in meta_type
               for kw in ("overload", "unavailable", "temporarily", "timeout"))


async def _openai_compat_complete(prov: str, model: str, prompt: str, *,
                                  system: str | None, max_tokens: int,
                                  effort: str | None) -> str:
    import asyncio
    import httpx
    base, key_env = _OPENAI_COMPAT[prov]
    key = os.getenv(key_env, "").strip() if key_env else "ollama"
    if key_env and not key:
        raise RuntimeError(f"{key_env} не задан в .env")

    messages = ([{"role": "system", "content": system}] if system else [])
    messages.append({"role": "user", "content": prompt})
    body: dict = {"model": model, "messages": messages}
    # OpenAI-родословные модели (gpt-oss, gpt-*, o1/o3/o4) требуют
    # max_completion_tokens; остальные (llama, kimi, deepseek, qwen) — max_tokens
    ml = model.lower()
    if any(t in ml for t in ("gpt-oss", "gpt-", "/o1", "/o3", "/o4")) or ml.startswith(("o1", "o3", "o4")):
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens
    # reasoning-модели (gpt-oss на Groq/OpenRouter) — режим усилия
    if effort and "gpt-oss" in ml:
        body["reasoning_effort"] = effort

    headers = {"Content-Type": "application/json"}
    if key_env:
        headers["Authorization"] = f"Bearer {key}"
    if prov == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/telegramtool"
        headers["X-Title"] = "telegramTool"

    last_error: Exception | None = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=120.0) as c:
                r = await c.post(f"{base}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as e:
            last_error = RuntimeError(f"сеть/{prov}: {e}")
            if attempt < _RETRY_ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAY_SEC)
                continue
            raise last_error

        try:
            data = r.json()
        except ValueError:
            data = None

        if r.status_code >= 400 or _is_retryable(r.status_code, data):
            last_error = RuntimeError(
                f"{prov} {r.status_code}: {(data if data is not None else r.text)!s:.250}")
            if _is_retryable(r.status_code, data) and attempt < _RETRY_ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAY_SEC)
                continue
            raise last_error
        break  # успех — data готов, выходим из цикла попыток

    try:
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"{prov}: неожиданный ответ: {str(data)[:250]}")
    if not text:
        raise RuntimeError(f"{prov} вернул пустой ответ")
    return text


# ── точка входа ──────────────────────────────────────────────────────────

async def complete(prompt: str, *, task: str = "smart", system: str | None = None,
                   max_tokens: int = 4096) -> str:
    """
    Один вызов LLM → строка. RuntimeError при любой проблеме.
    task: 'fast' (комментарии/имена) | 'smart' (аналитика/варианты текста).
    """
    if not prompt or not prompt.strip():
        raise RuntimeError("пустой промт")

    prov, model = resolve(task)
    effort = _TASK_EFFORT.get(task)

    if prov == "claude":
        return await _claude_complete(model, prompt, system=system,
                                      max_tokens=max_tokens, effort=effort)
    if prov in _OPENAI_COMPAT:
        return await _openai_compat_complete(prov, model, prompt, system=system,
                                             max_tokens=max_tokens, effort=effort)
    raise RuntimeError(f"неизвестный провайдер LLM: {prov} "
                       f"(допустимо: claude, {', '.join(_OPENAI_COMPAT)})")
