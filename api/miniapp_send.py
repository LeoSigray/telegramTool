"""
api/miniapp_send.py — отправка по нажатию большой кнопки Mini App.

Одно нажатие = одно сообщение одному человеку. Нажатие сразу списывает 1 сообщение
с баланса и встаёт в очередь; фоновый воркер по одному берёт нажатия, подбирает
свободный аккаунт, самого горячего клиента покупателя (его нашёл умный парсинг,
api/miniapp_parse.py) и отправляет. Если отправить не
вышло (все аккаунты заняты слишком долго, некому писать), сообщение возвращается
на баланс. Пауза между сообщениями с одного аккаунта — MINIAPP_TAP_GAP секунд, так
что быстрые нажатия просто чуть растягиваются по времени и не жгут аккаунты.

Каждая отправка пишется в sends с job_id tap-<id покупателя> (по нему диалог
принадлежит покупателю) и сразу заводит диалог в «Чатах», не дожидаясь ответа.
"""
import asyncio
import logging
import os
import time

from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PeerFloodError,
    UserDeactivatedBanError,
    UserPrivacyRestrictedError,
)

try:  # есть не во всех версиях telethon
    from telethon.errors import UserIsBlockedError
except ImportError:  # pragma: no cover
    class UserIsBlockedError(Exception):
        pass

import config as cfg
from data import analytics as an
from data import inbox
from data import miniapp_store as store
from optimizer import health
from .client_pool import pool
from .dm_runner import _resolve

log = logging.getLogger(__name__)

TAP_GAP_SEC = float(os.getenv("MINIAPP_TAP_GAP", "25") or 25)   # пауза между сообщениями с одного аккаунта
TAP_MAX_WAIT_SEC = 900                                          # дольше ждать свободный аккаунт не будем
MAX_TARGET_TRIES = 5                                            # сколько людей пробуем ради одного нажатия
_last_used: dict[str, float] = {}       # аккаунт → когда последний раз писал (monotonic)


# ── получатели ──────────────────────────────────────────────────────────────

def _next_lead(tg_id: int) -> dict | None:
    """Самый горячий клиент покупателя, которому ещё не писали ни мы, ни другие покупатели."""
    while True:
        lead = store.next_lead(tg_id)
        if lead is None:
            return None
        if lead["username"] in an.already_contacted([lead["username"]]) or an.is_suppressed(lead["username"]):
            store.mark_lead(lead["id"], "failed")        # уже писали или в стоп-листе: пропускаем
            continue
        return lead


# ── аккаунты ────────────────────────────────────────────────────────────────

def _pick_account() -> str | None:
    """Свободный аккаунт: в строю, не отдыхает, в пределах суток и выдержал паузу после прошлой отправки."""
    now = time.monotonic()
    ready = []
    for name in pool.list_active():
        if not health.is_available(name):
            continue
        used = an.sends_last_hours(name, 24)
        if used >= cfg.DM_DAILY_LIMIT_PER_ACCOUNT:
            continue
        if now - _last_used.get(name, 0.0) < TAP_GAP_SEC:
            continue
        ready.append((_last_used.get(name, 0.0), used, name))
    return min(ready)[2] if ready else None


def _display(entity) -> str | None:
    name = " ".join(x for x in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if x)
    return name or None


async def _send_one(account: str, client, lead: dict, text: str, job_id: str) -> str:
    """'sent' | 'bad_target' (пробуем другого клиента) | 'retry_account' (сбой аккаунта, нажатие остаётся)."""
    target = "@" + lead["username"]
    try:
        entity = await client.get_entity(_resolve(target))
    except FloodWaitError as e:
        health.on_error(account, "flood_wait", f"miniapp resolve {e.seconds}s")
        return "retry_account"
    except (AuthKeyUnregisteredError, UserDeactivatedBanError):
        health.on_error(account, "banned", "miniapp resolve")
        return "retry_account"
    except Exception as e:  # noqa: BLE001 — человека нет, закрыл профиль и т. п.
        an.record_send(job_id=job_id, account=account, target=target, peer_id=None, niche=None,
                       template_id=None, status="failed", error=f"resolve: {str(e)[:120]}")
        return "bad_target"
    try:
        sent = await client.send_message(entity, text)
    except FloodWaitError as e:
        health.on_error(account, "flood_wait", f"miniapp {e.seconds}s")
        return "retry_account"
    except PeerFloodError:
        health.on_error(account, "peer_flood", "miniapp")
        return "retry_account"
    except (AuthKeyUnregisteredError, UserDeactivatedBanError):
        health.on_error(account, "banned", "miniapp send")
        return "retry_account"
    except (UserPrivacyRestrictedError, UserIsBlockedError) as e:
        an.record_send(job_id=job_id, account=account, target=target, peer_id=str(getattr(entity, "id", "")),
                       niche=None, template_id=None, status="failed", error=type(e).__name__)
        return "bad_target"
    except Exception as e:  # noqa: BLE001
        an.record_send(job_id=job_id, account=account, target=target, peer_id=str(getattr(entity, "id", "")),
                       niche=None, template_id=None, status="failed", error=str(e)[:120])
        return "bad_target"

    peer_id = str(entity.id)
    _last_used[account] = time.monotonic()
    send_id = an.record_send(job_id=job_id, account=account, target=target, peer_id=peer_id, niche=None,
                             template_id=None, status="sent")
    conv_id, _ = inbox.get_or_create_conversation(
        peer_id, account, username=getattr(entity, "username", None),
        display_name=_display(entity) or lead.get("name"), send_id=send_id)
    inbox.record_outbound(conv_id, account=account, text=text, tg_id=getattr(sent, "id", None),
                          sent_at=(sent.date.isoformat() if getattr(sent, "date", None) else None),
                          via="platform")
    return "sent"


async def _process(tap: dict) -> bool:
    """Обрабатывает одно нажатие. True — аккаунтов нет, подождать и вернуться к очереди."""
    tid, tg = tap["id"], tap["tg_id"]
    user = store.get_user(tg)
    text = ((user or {}).get("offer_text") or "").strip()
    if not user or user.get("channel_status") != "ready" or not text:
        store.finish_tap(tid, status="failed", error="не подключён канал")
        return False
    account = _pick_account()
    if account is None:
        store.release_tap(tid)
        return True
    client = pool.get(account)
    if client is None:
        store.release_tap(tid)
        return True
    job_id = store.ensure_stream(tg)
    for _ in range(MAX_TARGET_TRIES):
        lead = await asyncio.to_thread(_next_lead, tg)
        if lead is None:
            store.finish_tap(tid, status="failed", error="некому отправить")
            return False
        res = await _send_one(account, client, lead, text, job_id)
        if res == "retry_account":
            store.release_tap(tid)
            return True
        store.mark_lead(lead["id"], "sent" if res == "sent" else "failed")
        if res == "sent":
            store.finish_tap(tid, status="sent", target="@" + lead["username"], account=account)
            return False
    store.finish_tap(tid, status="failed", error="не получилось отправить")
    return False


async def worker_loop() -> None:
    """Раз в секунду берёт следующее нажатие из очереди. Живёт, пока работает сервис."""
    recovered = store.recover_taps()
    if recovered:
        log.info("[miniapp] вернули в очередь нажатия после перезапуска: %d", recovered)
    while True:
        try:
            for tap in store.expired_taps(TAP_MAX_WAIT_SEC):
                store.finish_tap(tap["id"], status="failed", error="нет свободного аккаунта")
            tap = store.claim_next_tap()
            if tap is None:
                await asyncio.sleep(1.0)
                continue
            if await _process(tap):
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("[miniapp] сбой воркера нажатий")
            await asyncio.sleep(3.0)
