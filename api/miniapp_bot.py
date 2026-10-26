"""
api/miniapp_bot.py — бот Mini App «Клиент в один клик».

Здесь всё, что говорит с Telegram Bot API напрямую:
  • проверка подписи initData (кто открыл Mini App);
  • отправка сообщений покупателю (приветствие, ответы на рассылку, итоги, оплата);
  • счёт в Telegram Stars и разбор вебхука (pre_checkout_query, successful_payment);
  • установка вебхука и кнопки меню при старте сервиса.

Настройки — из окружения (.env подхватывает config.py):
  MINIAPP_BOT_TOKEN       токен бота от BotFather (обязателен);
  MINIAPP_PUBLIC_URL      внешний https-адрес сервера, например https://example.ru;
  MINIAPP_WEBHOOK_SECRET  секрет заголовка вебхука (если не задан — выводится из токена).
"""
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qsl

import httpx

import config  # noqa: F401  — подгружает .env до чтения переменных

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
INIT_DATA_MAX_AGE_SEC = 24 * 3600


def bot_token() -> str:
    return os.getenv("MINIAPP_BOT_TOKEN", "").strip()


def public_url() -> str:
    return os.getenv("MINIAPP_PUBLIC_URL", "").strip().rstrip("/")


def app_url(query: str = "") -> str:
    return f"{public_url()}/miniapp/" + (f"?{query}" if query else "")


def webhook_secret() -> str:
    explicit = os.getenv("MINIAPP_WEBHOOK_SECRET", "").strip()
    if explicit:
        return explicit
    # Вебхук должен быть недоступен для подделки, а отдельный секрет — лишняя настройка.
    # Берём производное от токена: знает его только тот, кто знает токен.
    return hashlib.sha256(("webhook:" + bot_token()).encode()).hexdigest()[:48]


# ──────────────────────────────────────────────────────────────────────────
#  Подпись initData
# ──────────────────────────────────────────────────────────────────────────

class InitDataError(ValueError):
    pass


def validate_init_data(init_data: str, token: str | None = None,
                       max_age: int = INIT_DATA_MAX_AGE_SEC, now: float | None = None) -> dict:
    """
    Проверяет Telegram.WebApp.initData по документации Mini Apps: строка проверки —
    все поля, кроме hash, отсортированные по ключу, через перевод строки; ключ —
    HMAC-SHA256("WebAppData", токен бота). Возвращает {"user": {...}, "start_param": ...}.
    """
    token = token if token is not None else bot_token()
    if not token:
        raise InitDataError("бот не настроен")
    if not init_data:
        raise InitDataError("нет initData")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received:
        raise InitDataError("нет подписи")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataError("подпись не сходится")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise InitDataError("плохая дата") from None
    if (now if now is not None else time.time()) - auth_date > max_age:
        raise InitDataError("сессия устарела, откройте приложение заново")
    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        raise InitDataError("плохой user") from None
    if not isinstance(user, dict) or not user.get("id"):
        raise InitDataError("нет пользователя")
    return {"user": user, "start_param": pairs.get("start_param")}


# ──────────────────────────────────────────────────────────────────────────
#  Bot API
# ──────────────────────────────────────────────────────────────────────────

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=20)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def call(method: str, **params) -> dict:
    """Вызов Bot API. Бросает RuntimeError с описанием Telegram, если ok=false."""
    token = bot_token()
    if not token:
        raise RuntimeError("MINIAPP_BOT_TOKEN не задан")
    r = await _http().post(f"{API}/bot{token}/{method}", json=params)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description', r.text[:200])}")
    return data.get("result")


def open_app_keyboard(text: str = "Открыть", query: str = "") -> dict:
    return {"inline_keyboard": [[{"text": text, "web_app": {"url": app_url(query)}}]]}


async def send(chat_id: int, text: str, *, button: str | None = None, query: str = "",
               keyboard: dict | None = None) -> None:
    """Сообщение покупателю. Ошибки только логируем: уведомление не важнее основной работы."""
    params: dict = {"chat_id": chat_id, "text": text}
    if keyboard:
        params["reply_markup"] = keyboard
    elif button and public_url():
        params["reply_markup"] = open_app_keyboard(button, query)
    try:
        await call("sendMessage", **params)
    except Exception as e:  # noqa: BLE001
        log.warning("[miniapp-bot] не отправил сообщение %s: %s", chat_id, e)


async def deliver(chat_id: int, text: str, *, keyboard: dict | None = None) -> int | None:
    """Сообщение, номер которого нужен вызывающему (копия для оператора поддержки). None — не дошло."""
    params: dict = {"chat_id": chat_id, "text": text}
    if keyboard:
        params["reply_markup"] = keyboard
    try:
        res = await call("sendMessage", **params)
        return int(res["message_id"])
    except Exception as e:  # noqa: BLE001
        log.warning("[miniapp-bot] не доставил %s: %s", chat_id, e)
        return None


async def create_stars_invoice(*, title: str, description: str, payload: str,
                               label: str, stars: int) -> str:
    """Ссылка на счёт в Telegram Stars (валюта XTR, без провайдера)."""
    return await call("createInvoiceLink", title=title, description=description,
                      payload=payload, currency="XTR",
                      prices=[{"label": label, "amount": int(stars)}])


async def setup() -> None:
    """Вебхук и кнопка меню. Без публичного адреса бот не настраивается (локальная разработка)."""
    if not bot_token() or not public_url():
        log.info("[miniapp-bot] токен или MINIAPP_PUBLIC_URL не заданы — бот не настроен")
        return
    try:
        await call("setWebhook", url=f"{public_url()}/miniapp/bot/webhook",
                   secret_token=webhook_secret(),
                   allowed_updates=["message", "callback_query", "pre_checkout_query"])
        await call("setChatMenuButton",
                   menu_button={"type": "web_app", "text": "Open · Открыть",
                                "web_app": {"url": app_url()}})
        log.info("[miniapp-bot] вебхук и кнопка меню настроены")
    except Exception as e:  # noqa: BLE001
        log.warning("[miniapp-bot] настройка бота не удалась: %s", e)


# ──────────────────────────────────────────────────────────────────────────
#  Тексты бота
# ──────────────────────────────────────────────────────────────────────────

LANGS = ("ru", "en")
LANG_PROMPT = "Выберите язык / Choose your language"

_T = {
    "welcome": {
        "ru": ("Здравствуйте! Это «Клиент в один клик».\n\n"
               "Скажите, что вы продвигаете, и нажимайте на большую кнопку: каждое нажатие отправляет "
               "одно сообщение. Отвечайте людям прямо здесь. Первые 10 сообщений — в подарок."),
        "en": ("Hello! This is “Client in One Click”.\n\n"
               "Tell us what you promote and tap the big button: every tap sends one message. "
               "Reply to people right here. Your first 10 messages are a gift."),
    },
    "open": {"ru": "Открыть", "en": "Open"},
    "open_app": {"ru": "Открыть приложение", "en": "Open the app"},
    "reply_button": {"ru": "Ответить", "en": "Reply"},
    "person": {"ru": "Человек", "en": "Someone"},
    "stale_invoice": {"ru": "Счёт устарел, создайте новый в приложении",
                      "en": "This invoice has expired, create a new one in the app"},
}


def lang_of(user: dict | None) -> str:
    lang = (user or {}).get("lang")
    return lang if lang in LANGS else "ru"


def tx(key: str, lang: str = "ru") -> str:
    return _T[key].get(lang) or _T[key]["ru"]


def lang_keyboard() -> dict:
    """Кнопки выбора языка: 1 Русский, 2 English."""
    return {"inline_keyboard": [[{"text": "1  Русский", "callback_data": "lang:ru"},
                                 {"text": "2  English", "callback_data": "lang:en"}]]}


def reply_text(name: str | None, text: str | None, lang: str = "ru") -> str:
    who = name or tx("person", lang)
    body = (text or "").strip()
    if len(body) > 160:
        body = body[:157] + "…"
    head = "💬 Новое сообщение от" if lang != "en" else "💬 New message from"
    return f"{head} {who}" + (f":\n«{body}»" if body else "")


def support_text(text: str, lang: str = "ru") -> str:
    body = (text or "").strip()
    if len(body) > 300:
        body = body[:297] + "…"
    head = "💬 Ответ поддержки:" if lang != "en" else "💬 Support replied:"
    return f"{head}\n«{body}»"


def paid_text(messages: int, lang: str = "ru") -> str:
    if lang == "en":
        return f"Payment received: +{messages} messages. You can send now."
    return f"Оплата получена: +{messages} сообщений. Можно отправлять."