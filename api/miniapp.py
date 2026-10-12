"""
api/miniapp.py — Mini App «Клиент в один клик» поверх сервиса рассылок.

Покупатель открывает Mini App в Telegram, получает 10 сообщений в подарок, присылает
свой канал (сервис сам читает его и пишет текст), и дальше каждое нажатие большой
кнопки отправляет одно сообщение. Ответы и отправленные сообщения он видит в «Чатах»,
отвечает людям, правит и удаляет свои сообщения, пополняет баланс (Telegram Stars).
Всё «внутреннее» — аккаунты, лимиты, стоп-лист, база контактов — остаётся на стороне
сервиса и покупателю не показывается.

Маршруты (наружу через nginx открыт только префикс /miniapp/):
  GET  /miniapp/                       страница приложения (без авторизации)
  *    /miniapp/api/*                  API, авторизация — подпись Telegram initData
  POST /miniapp/bot/webhook            вебхук бота (секрет в заголовке)

Баланс: нажатие списывает 1 сообщение сразу, а если отправить не вышло, оно возвращается.
Очередь нажатий хранится в базе и переживает перезапуск сервиса (api/miniapp_send.py).
"""
import asyncio
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from data import miniapp_store as store
from . import miniapp_bot as bot
from . import miniapp_channel as channel
from . import miniapp_parse as finder
from . import miniapp_send as sender
from .inbox_service import EDIT_WINDOW_HOURS, InboxError, delete_message, edit_message, hydrate, send_reply

log = logging.getLogger(__name__)

router = APIRouter(prefix="/miniapp", tags=["miniapp"])

GIFT_MESSAGES = int(os.getenv("MINIAPP_GIFT", "10") or 10)
MAX_PENDING_TAPS = 50      # сколько нажатий может ждать отправки у одного покупателя
MIN_TEXT_LEN = 10
NOTIFY_INTERVAL_SEC = 15

# Пакеты сообщений. Цены — заглушки, настраиваются в .env (MINIAPP_PACKS, JSON-список).
DEFAULT_PACKS = [
    {"id": "p200", "messages": 200, "stars": 350},
    {"id": "p500", "messages": 500, "stars": 800},
    {"id": "p1000", "messages": 1000, "stars": 1400, "badge": "Выгоднее"},
]
METHODS = [
    {"id": "stars", "title": "Telegram Stars", "available": True},
    {"id": "card", "title": "Банковская карта", "available": False, "hidden": True},
    {"id": "crypto", "title": "Криптовалюта", "available": False},
    {"id": "sbp", "title": "СБП", "available": False, "hidden": True},
]


def visible_methods() -> list[dict]:
    """Способы оплаты для покупателя. Спрятанные (card, sbp) возвращает MINIAPP_SHOW_METHODS=card,sbp."""
    show = {x.strip() for x in os.getenv("MINIAPP_SHOW_METHODS", "").split(",") if x.strip()}
    return [{k: v for k, v in m.items() if k != "hidden"} for m in METHODS if not m.get("hidden") or m["id"] in show]


def packs() -> list[dict]:
    raw = os.getenv("MINIAPP_PACKS", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                return data
        except json.JSONDecodeError:
            log.warning("[miniapp] MINIAPP_PACKS — не JSON, беру пакеты по умолчанию")
    return DEFAULT_PACKS


# ──────────────────────────────────────────────────────────────────────────
#  Авторизация
# ──────────────────────────────────────────────────────────────────────────

def current_user(x_telegram_init_data: str = Header(default="")) -> dict:
    try:
        data = bot.validate_init_data(x_telegram_init_data)
    except bot.InitDataError as e:
        raise HTTPException(status_code=401, detail=str(e))
    u = data["user"]
    return store.upsert_user(int(u["id"]), username=u.get("username"),
                             first_name=u.get("first_name"), last_name=u.get("last_name"))


# ──────────────────────────────────────────────────────────────────────────
#  Страница
# ──────────────────────────────────────────────────────────────────────────

@router.get("/", include_in_schema=False)
def page() -> FileResponse:
    path = os.path.join(os.path.dirname(__file__), "miniapp.html")
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-cache"})


# Шрифт Inter лежит рядом с приложением: свой шрифт одинаково рисует цифры на любом устройстве.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_STATIC_FILES = {"inter-cyrillic.woff2": "font/woff2", "inter-latin.woff2": "font/woff2"}


@router.get("/static/{name}", include_in_schema=False)
def static_file(name: str) -> FileResponse:
    ctype = _STATIC_FILES.get(name)
    if ctype is None:
        raise HTTPException(status_code=404, detail="Не найдено")
    return FileResponse(os.path.join(_STATIC_DIR, name), media_type=ctype,
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ──────────────────────────────────────────────────────────────────────────
#  API
# ──────────────────────────────────────────────────────────────────────────

def _leads_view(user: dict) -> dict:
    """Клиенты покупателя, найденные умным парсингом: сколько ждёт сообщения и идёт ли поиск."""
    return {"new": store.leads_new(user["tg_id"]),
            "searching": (user.get("parse_status") or "") in ("queued", "running")}


def _channel_view(user: dict | None) -> dict | None:
    if not user or not user.get("channel"):
        return None
    return {"handle": user["channel"], "kind": user.get("channel_kind") or "channel",
            "status": user.get("channel_status") or "building",
            "text": user.get("offer_text") or "", "error": user.get("channel_error")}


@router.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict:
    tg = user["tg_id"]
    return {
        "user": {"id": tg, "first_name": user["first_name"], "username": user["username"]},
        "balance": user["balance"],
        "gift": {"amount": GIFT_MESSAGES, "claimed": bool(user["gift_claimed"])},
        "channel": _channel_view(user),
        "leads": _leads_view(user),
        "pending": store.pending_taps(tg),
        "sent": store.sent_total(tg),
        "unread": store.unread_total(tg),
        "support_unread": store.support_unread(tg),
        "has_paid": store.has_paid(tg),
    }


@router.post("/api/gift")
def claim_gift(user: dict = Depends(current_user)) -> dict:
    claimed = store.claim_gift(user["tg_id"], GIFT_MESSAGES)
    fresh = store.get_user(user["tg_id"])
    return {"claimed": claimed, "balance": fresh["balance"]}


class ChannelIn(BaseModel):
    channel: str = Field(min_length=1, max_length=400)


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@router.get("/api/channel")
def get_channel(user: dict = Depends(current_user)) -> dict:
    return {"channel": _channel_view(store.get_user(user["tg_id"]))}


@router.post("/api/channel")
async def set_channel(body: ChannelIn, user: dict = Depends(current_user)) -> dict:
    """Покупатель говорит, что продвигает (канал, продукт или нишу); сервис в фоне пишет текст (building → ready)."""
    try:
        channel.start_build(user["tg_id"], body.channel)
    except channel.ChannelError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"channel": _channel_view(store.get_user(user["tg_id"]))}


@router.put("/api/channel/text")
def set_text(body: TextIn, user: dict = Depends(current_user)) -> dict:
    """Покупатель поправил текст, который написала нейронка."""
    tg = user["tg_id"]
    if (store.get_user(tg) or {}).get("channel_status") != "ready":
        raise HTTPException(status_code=409, detail="Сначала подключите канал")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", body.text).strip()
    if len(text) < MIN_TEXT_LEN:
        raise HTTPException(status_code=400, detail=f"Напишите сообщение подлиннее (от {MIN_TEXT_LEN} символов)")
    store.set_offer_text(tg, text)
    return {"channel": _channel_view(store.get_user(tg))}


@router.post("/api/tap")
async def tap(user: dict = Depends(current_user)) -> dict:
    """Одно нажатие большой кнопки: списываем 1 сообщение и ставим его в очередь отправки."""
    tg = user["tg_id"]
    fresh = store.get_user(tg) or {}
    if fresh.get("channel_status") != "ready" or not (fresh.get("offer_text") or "").strip():
        raise HTTPException(status_code=409, detail="Сначала подключите канал")
    if store.leads_new(tg) - store.pending_taps(tg) <= 0:
        if (fresh.get("parse_status") or "") in ("queued", "running"):
            raise HTTPException(status_code=409, detail="Ищем ваших клиентов. Когда найдём, сообщим.")
        if int(fresh.get("parse_runs") or 0) < finder.MAX_RUNS:
            store.parse_enqueue(tg)             # поиск закончился, а клиентов нет: ищем ещё
        raise HTTPException(status_code=409, detail="Пока не нашли людей под вашу тему. Продолжаем искать, сообщим.")
    if store.pending_taps(tg) >= MAX_PENDING_TAPS:
        raise HTTPException(status_code=429, detail="Подождите: предыдущие сообщения ещё отправляются")
    if not store.reserve(tg, 1):
        return JSONResponse(status_code=402, content={
            "detail": "Сообщения закончились. Пополните баланс.", "balance": 0})
    store.ensure_stream(tg)
    store.enqueue_tap(tg)
    return {"balance": store.get_user(tg)["balance"], "pending": store.pending_taps(tg),
            "sent": store.sent_total(tg)}


@router.get("/api/chats")
def chats(user: dict = Depends(current_user)) -> dict:
    return {"chats": store.user_conversations(user["tg_id"])}


def _own_conversation(conv_id: int, user: dict) -> None:
    if store.conversation_owner(conv_id) != user["tg_id"]:
        raise HTTPException(status_code=404, detail="Диалог не найден")


@router.get("/api/chats/{conv_id}")
async def chat(conv_id: int, user: dict = Depends(current_user)) -> dict:
    from data import inbox
    _own_conversation(conv_id, user)
    if inbox.needs_hydration(conv_id):
        try:
            await hydrate(conv_id)
        except InboxError:
            pass
    inbox.mark_read(conv_id)
    conv = inbox.get_conversation(conv_id) or {}
    edited = store.edited_ids(conv_id)
    messages = []
    for m in inbox.get_messages(conv_id):
        if m["status"] == "deleted":
            continue
        mine = m["direction"] == "out" and m["status"] == "ok" and bool(m["tg_id"])
        messages.append({"id": m["id"], "out": m["direction"] == "out", "text": m["text"],
                         "at": m["sent_at"], "status": m["status"], "via": m["via"],
                         "edited": m["tg_id"] in edited,
                         "can_delete": mine, "can_edit": mine and _editable_age(m["sent_at"])})
    return {"chat": {"id": conv_id, "name": conv.get("display_name") or conv.get("username"),
                     "username": conv.get("username")},
            "messages": messages}


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


@router.post("/api/chats/{conv_id}/reply")
async def reply(conv_id: int, body: ReplyIn, user: dict = Depends(current_user)) -> dict:
    _own_conversation(conv_id, user)
    try:
        await send_reply(conv_id, body.text)
    except InboxError as e:
        raise HTTPException(status_code=409, detail=f"Не удалось отправить: {e}")
    return {"ok": True}


def _editable_age(sent_at: str | None) -> bool:
    """Telegram позволяет править сообщение только первые двое суток."""
    try:
        d = datetime.fromisoformat(str(sent_at))
    except ValueError:
        return True                       # не смогли разобрать время: пусть решает Telegram
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - d <= timedelta(hours=EDIT_WINDOW_HOURS)


@router.patch("/api/chats/{conv_id}/messages/{msg_id}")
async def edit_msg(conv_id: int, msg_id: int, body: ReplyIn, user: dict = Depends(current_user)) -> dict:
    _own_conversation(conv_id, user)
    try:
        res = await edit_message(conv_id, msg_id, body.text)
    except InboxError as e:
        raise HTTPException(status_code=409, detail=f"Не удалось изменить: {e}")
    store.mark_edited(conv_id, res["tg_id"])
    store.refresh_last_message(conv_id)
    return {"ok": True}


@router.delete("/api/chats/{conv_id}/messages/{msg_id}")
async def delete_msg(conv_id: int, msg_id: int, user: dict = Depends(current_user)) -> dict:
    _own_conversation(conv_id, user)
    try:
        await delete_message(conv_id, msg_id)
    except InboxError as e:
        raise HTTPException(status_code=409, detail=f"Не удалось удалить: {e}")
    store.refresh_last_message(conv_id)
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────
#  Поддержка: покупатель пишет «поддержке», отвечает оператор из Telegram через бота
# ──────────────────────────────────────────────────────────────────────────
#
# Покупатель не видит, кто отвечает: в приложении и в уведомлениях бота это просто «Поддержка».
# Оператор (MINIAPP_OPERATORS, имена без @) один раз открывает бота и нажимает Start, после чего
# сообщения покупателей приходят ему в бот, а ответ (Reply на сообщение) попадает в чат покупателя.

MAX_SUPPORT_LEN = 2000


def _operator_usernames() -> set[str]:
    return {x.strip().lstrip("@").lower() for x in os.getenv("MINIAPP_OPERATORS", "").split(",") if x.strip()}


def _support_view(rows: list[dict]) -> list[dict]:
    return [{"id": r["id"], "out": r["direction"] == "in", "text": r["text"], "at": r["created_at"]}
            for r in rows]


@router.get("/api/support")
def get_support(user: dict = Depends(current_user)) -> dict:
    tg = user["tg_id"]
    rows = store.support_messages(tg)
    store.support_mark_read(tg)
    return {"messages": _support_view(rows)}


class SupportIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_SUPPORT_LEN)


@router.post("/api/support")
async def write_support(body: SupportIn, user: dict = Depends(current_user)) -> dict:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", body.text).strip()
    if not text:
        raise HTTPException(status_code=400, detail="Напишите сообщение")
    store.support_add(user["tg_id"], "in", text)
    await _deliver_support()
    return {"messages": _support_view(store.support_messages(user["tg_id"]))}


async def _deliver_support() -> None:
    """Передаёт операторам новые сообщения покупателей. Что не дошло, уйдёт при следующей попытке."""
    ops = store.operator_ids()
    if not ops:
        return
    for m in store.support_undelivered():
        name = " ".join(x for x in (m["first_name"], m["last_name"]) if x) or "Клиент"
        who = f"@{m['username']}" if m["username"] else f"id {m['tg_id']}"
        copies = []
        for op in ops:
            mid = await bot.deliver(op, f"Поддержка · {name} ({who})\n\n{m['text']}")
            if mid:
                copies.append((op, mid))
        if copies:
            store.support_mark_delivered(m["id"], copies)


async def _handle_operator(msg: dict) -> bool:
    """Сообщение от оператора поддержки. True — обработано, покупателю приветствие не нужно."""
    frm = msg.get("from") or {}
    uid, chat_id = frm.get("id"), (msg.get("chat") or {}).get("id")
    if not uid or not chat_id:
        return False
    uname = (frm.get("username") or "").lower()
    first_contact = uname in _operator_usernames() and not store.state_get(f"operator:{uname}")
    if uid not in store.operator_ids() and not first_contact:
        return False
    if first_contact:           # аккаунт запоминаем по id: дальше имя в Telegram уже ничего не решает
        store.state_set(f"operator:{uname}", str(uid))
        await bot.send(chat_id, "Вы подключены как поддержка. Сообщения клиентов будут приходить сюда. "
                                "Чтобы ответить, ответьте (Reply) на сообщение клиента.")
        await _deliver_support()
        return True
    text = (msg.get("text") or "").strip()
    reply = msg.get("reply_to_message") or {}
    if not text:
        return True
    row = store.support_find_by_operator_msg(chat_id, reply["message_id"]) if reply.get("message_id") else None
    if row is None:
        await bot.send(chat_id, "Чтобы ответить клиенту, ответьте (Reply) на его сообщение.")
        return True
    store.support_add(row["tg_id"], "out", text[:MAX_SUPPORT_LEN])
    await bot.send(row["tg_id"], bot.support_text(text), button="Открыть", query="support=1")
    await bot.send(chat_id, "✓ Ответ отправлен")
    return True


@router.get("/api/packs")
def get_packs(user: dict = Depends(current_user)) -> dict:
    return {"packs": packs(), "methods": visible_methods()}


class InvoiceIn(BaseModel):
    pack_id: str
    method: str = "stars"


@router.post("/api/invoice")
async def invoice(body: InvoiceIn, user: dict = Depends(current_user)) -> dict:
    pack = next((p for p in packs() if p["id"] == body.pack_id), None)
    if pack is None:
        raise HTTPException(status_code=404, detail="Пакет не найден")
    if body.method != "stars":
        raise HTTPException(status_code=400, detail="Этот способ оплаты пока недоступен")
    payload = "mini-" + secrets.token_hex(12)
    store.create_payment(user["tg_id"], pack_id=pack["id"], messages=int(pack["messages"]),
                         amount=int(pack["stars"]), currency="XTR", method="stars", payload=payload)
    try:
        link = await bot.create_stars_invoice(
            title=f"{pack['messages']} сообщений",
            description="Пополнение баланса «Клиент в один клик»",
            payload=payload, label=f"{pack['messages']} сообщений", stars=int(pack["stars"]))
    except Exception as e:  # noqa: BLE001
        log.warning("[miniapp] счёт не создан: %s", e)
        raise HTTPException(status_code=502, detail="Не удалось создать счёт, попробуйте ещё раз")
    return {"link": link, "payload": payload}


@router.get("/api/payments")
def payments(user: dict = Depends(current_user)) -> dict:
    return {"payments": store.list_payments(user["tg_id"])}


# ──────────────────────────────────────────────────────────────────────────
#  Вебхук бота
# ──────────────────────────────────────────────────────────────────────────

@router.post("/bot/webhook", include_in_schema=False)
async def webhook(request: Request,
                  x_telegram_bot_api_secret_token: str = Header(default="")) -> dict:
    if not secrets.compare_digest(x_telegram_bot_api_secret_token, bot.webhook_secret()):
        raise HTTPException(status_code=403, detail="forbidden")
    update = await request.json()
    try:
        await _handle_update(update)
    except Exception:  # noqa: BLE001 — Telegram повторит апдейт при ошибке, нам это не нужно
        log.exception("[miniapp-bot] апдейт не обработан")
    return {"ok": True}


async def _handle_update(update: dict) -> None:
    pcq = update.get("pre_checkout_query")
    if pcq:
        pay = store.get_payment_by_payload(pcq.get("invoice_payload", ""))
        ok = pay is not None and pay["status"] == "pending"
        params = {"pre_checkout_query_id": pcq["id"], "ok": ok}
        if not ok:
            params["error_message"] = "Счёт устарел, создайте новый в приложении"
        await bot.call("answerPreCheckoutQuery", **params)
        return

    msg = update.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if not chat_id:
        return
    if not msg.get("successful_payment") and await _handle_operator(msg):
        return
    paid = msg.get("successful_payment")
    if paid:
        res = store.mark_paid(paid.get("invoice_payload", ""),
                              charge_id=paid.get("telegram_payment_charge_id"))
        if res:
            await bot.send(chat_id, bot.paid_text(res["messages"]), button="Открыть")
        return
    # Любое сообщение боту (в том числе /start) — приветствие с кнопкой приложения:
    # всё общение с покупателем идёт в самом Mini App.
    if (msg.get("text") or "").strip():
        await bot.send(chat_id, bot.WELCOME, button="Открыть приложение")


# ──────────────────────────────────────────────────────────────────────────
#  Уведомления об ответах
# ──────────────────────────────────────────────────────────────────────────

async def _notify_loop() -> None:
    cursor_raw = store.state_get("notify_cursor")
    cursor = int(cursor_raw) if cursor_raw and cursor_raw.isdigit() else store.max_inbound_id()
    store.state_set("notify_cursor", str(cursor))
    while True:
        try:
            await asyncio.sleep(NOTIFY_INTERVAL_SEC)
            rows = store.inbound_since(cursor)
            notified: set[tuple[int, int]] = set()
            for r in rows:
                cursor = max(cursor, r["msg_id"])
                key = (r["tg_id"], r["conv_id"])
                if key in notified:
                    continue
                notified.add(key)
                await bot.send(r["tg_id"], bot.reply_text(r["display_name"] or r["username"], r["text"]),
                               button="Ответить", query=f"chat={r['conv_id']}")
            if rows:
                store.state_set("notify_cursor", str(cursor))
            await _deliver_support()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("[miniapp] уведомления: итерация не удалась")


_notify_task: asyncio.Task | None = None
_tap_task: asyncio.Task | None = None
_parse_task: asyncio.Task | None = None


async def startup() -> None:
    global _notify_task, _tap_task, _parse_task
    store.init_store()
    if not bot.bot_token():
        log.info("[miniapp] MINIAPP_BOT_TOKEN не задан — Mini App выключено")
        return
    await bot.setup()
    _notify_task = asyncio.create_task(_notify_loop())
    _tap_task = asyncio.create_task(sender.worker_loop())
    _parse_task = asyncio.create_task(finder.worker_loop())
    resumed = channel.resume_pending()
    if resumed:
        log.info("[miniapp] заново читаем каналы после перезапуска: %d", resumed)


async def shutdown() -> None:
    for t in (_notify_task, _tap_task, _parse_task):
        if t is not None:
            t.cancel()
    await bot.close()
