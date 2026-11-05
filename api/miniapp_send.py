"""
api/miniapp_send.py — отправка по нажатию большой кнопки Mini App.

Одно нажатие = одно сообщение одному человеку. Нажатие сразу списывает 1 сообщение
с баланса и встаёт в очередь. Для него умный парсинг ищет одного человека под тему
покупателя (api/miniapp_parse.py); как только человек найден, воркер берёт свободный
аккаунт и отправляет. Если отправить не вышло (человека не нашли, аккаунты заняты слишком
долго), сообщение возвращается на баланс. Пауза между сообщениями с одного аккаунта —
MINIAPP_TAP_GAP секунд.

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
# Ожидание нажатия больше не истекает: покупатель сам решает, ждать или забрать попытку
# кнопкой отмены. Константа оставлена — её читает отчёт о «залежавшихся» нажатиях.
TAP_MAX_WAIT_SEC = 3 * 3600
MAX_TARGET_TRIES = 5                                            # сколько людей пробуем ради одного нажатия
MAX_ACCOUNT_TRIES = 3                   # сколько аккаунтов пробуем, пока лид не найден по @имени
RESOLVE_FAILS_TO_REST = 3               # столько подряд ненайденных имён — и аккаунт ослеп, а не лиды плохи
_last_used: dict[str, float] = {}       # аккаунт → когда последний раз писал (monotonic)
_resolve_fails: dict[str, int] = {}     # аккаунт → ненайденных @имён подряд


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

def _pick_account(skip: set | None = None) -> str | None:
    """Свободный аккаунт: в строю, не отдыхает, в пределах суток и выдержал паузу после прошлой отправки.

    skip — аккаунты, которые уже не нашли этого человека: предлагать им его снова
    бессмысленно, пусть попробует следующий.
    """
    now = time.monotonic()
    skip = skip or set()
    ready = []
    for name in pool.list_active():
        if name in skip or not health.is_available(name):
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
    """'sent' | 'not_found' (имя не нашлось) | 'bad_target' | 'retry_account' (сбой аккаунта)."""
    target = "@" + lead["username"]
    try:
        entity = await client.get_entity(_resolve(target))
    except FloodWaitError as e:
        health.on_error(account, "flood_wait", f"miniapp resolve {e.seconds}s")
        return "retry_account"
    except (AuthKeyUnregisteredError, UserDeactivatedBanError):
        health.on_error(account, "banned", "miniapp resolve")
        return "retry_account"
    except Exception as e:  # noqa: BLE001 — человека нет ИЛИ этот аккаунт его не видит
        # Ограниченный аккаунт получает «No user has … as username» на живые имена и
        # молча списывает лида за лидом. Отличить можно только по серии: один промах
        # — скорее человек удалился, три подряд — ослеп аккаунт.
        _resolve_fails[account] = _resolve_fails.get(account, 0) + 1
        an.record_send(job_id=job_id, account=account, target=target, peer_id=None, niche=None,
                       template_id=None, status="failed", error=f"resolve: {str(e)[:120]}")
        if _resolve_fails[account] >= RESOLVE_FAILS_TO_REST:
            log.warning("[miniapp] %s: %d @имени подряд не найдено — отправляю аккаунт отдыхать",
                        account, _resolve_fails[account])
            health.on_error(account, "resolve_blocked", "miniapp resolve")
            _resolve_fails[account] = 0
            return "retry_account"
        return "not_found"
    _resolve_fails[account] = 0             # нашёл человека — значит видит людей
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
    job_id = store.ensure_stream(tg)
    tried: set = set()
    for _ in range(MAX_TARGET_TRIES):
        lead = await asyncio.to_thread(_next_lead, tg)
        if lead is None:
            store.finish_tap(tid, status="failed", error="некому отправить")
            return False
        # Одного и того же человека предлагаем нескольким аккаунтам: имя может не
        # находиться не потому, что человека нет, а потому что этот аккаунт ослеп.
        # Менять аккаунт можно только здесь, до первого сообщения: дальше переписка
        # привязана к нему, и отвечать с другого — рвать контекст для собеседника.
        res, account = "retry_account", None
        for _ in range(MAX_ACCOUNT_TRIES):
            account = _pick_account(skip=tried)
            if account is None:
                # Человек найден, а отправить им некем: весь пул отдыхает или выбрал лимит.
                # Помечаем застрявшим — только такому нажатию покупатель увидит «Отменить».
                store.release_tap(tid, stalled=True)
                return True
            client = pool.get(account)
            if client is None:
                tried.add(account)
                continue
            res = await _send_one(account, client, lead, text, job_id)
            if res != "not_found":
                break
            tried.add(account)          # этот не нашёл — пусть попробует следующий
        if res == "retry_account":
            # Аккаунты сбоили, но пул не пуст: пробуем снова, человека не тревожим.
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
    last_sweep = 0.0
    while True:
        try:
            # Сессии мини-аппа, от которых давно нет пинга: приложение закрыли молча.
            # Закрываем раз в минуту, иначе в среднее время сессии попадут часы простоя.
            if time.monotonic() - last_sweep > 60:
                last_sweep = time.monotonic()
                closed = store.close_stale_sessions()
                if closed:
                    log.info("[miniapp] закрыли молча брошенных сессий: %d", closed)
            # Раньше нажатие, провисевшее дольше TAP_MAX_WAIT_SEC, закрывалось само с текстом
            # «нет свободного аккаунта»: покупатель видел непонятную ошибку и возврат, которого
            # не просил. Теперь ожидание не истекает — сообщение честно висит «в пути», а забрать
            # попытку человек может сам кнопкой отмены. Так он понимает, что происходит.
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
