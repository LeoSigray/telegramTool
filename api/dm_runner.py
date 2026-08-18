"""Async-runner для рассылки в личные сообщения.

Поверх базовой отправки подключён optimizer/:
  • лимит на аккаунт — динамический (optimizer.health), а не константа
  • текст сообщения — выбирается бандитом из вариантов ниши (optimizer.bandit)
  • каждая отправка пишется в data/analytics.sends (иначе аналитики нет)
  • FloodWait/PeerFlood/бан — сажают аккаунт отдыхать и режут его лимит

Если job.optimize=False или job.niche=None — работает по-старому:
статичный DM_LIMIT_PER_ACCOUNT и единый текст job.message.
"""
import asyncio
import os
import random

from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PeerFloodError,
    UserBannedInChannelError,
    UserDeactivatedBanError,
    UserPrivacyRestrictedError,
)
from datetime import datetime, timezone

from accounts.manager import get_session_files
from data import analytics as an
from optimizer import bandit, health
from .client_pool import pool
from config import DM_DELAY_MAX, DM_DELAY_MIN, DM_LIMIT_PER_ACCOUNT

from .jobs import Job, TargetState


def _normalize(raw: str) -> str:
    """Подготовка к get_entity: @user → user, t.me/foo → foo, +7999... → +7999... (телефон)."""
    s = raw.strip()
    for pfx in ("https://t.me/", "http://t.me/", "t.me/"):
        if s.startswith(pfx):
            s = s.removeprefix(pfx)
            break
    return s


def _resolve(raw: str):
    """Возвращает аргумент для get_entity. Если строка цифр без + — user_id, иначе username."""
    h = _normalize(raw).lstrip("@")
    if h.startswith("+") and h[1:].isdigit():
        return h  # телефон
    if h.isdigit():
        return int(h)  # user_id
    return f"@{h}"


def _mark(job: Job, t: TargetState, status: str, error: str | None, account: str,
          *, peer_id: str | None = None, template_id: int | None = None) -> None:
    t.status = status
    t.error = error
    t.used_account = account
    t.template_id = template_id
    t.processed_at = datetime.now(timezone.utc)
    if status == "sent":
        job.sent += 1
    elif status == "failed":
        job.failed += 1
    elif status == "skipped":
        job.skipped += 1

    # пишем в аналитику — это единственный источник данных для графиков и оптимизатора
    try:
        t.send_id = an.record_send(
            job_id=job.id, account=account, target=t.target, peer_id=peer_id,
            niche=job.niche, template_id=template_id, status=status, error=error,
            channel=job.target_channel,
        )
    except Exception as e:  # noqa: BLE001 — аналитика не должна ронять рассылку
        job.add_log(event="analytics_error", error=str(e))

    job.add_log(event=status, target=t.target, account=account, error=error,
                template_id=template_id,
                sent=job.sent, failed=job.failed, skipped=job.skipped)


def _pick_message(job: Job) -> tuple[str, int | None]:
    """Текст для очередной отправки: бандит по нише либо статичный job.message."""
    if not (job.optimize and job.niche):
        return job.message, None
    tpl = bandit.pick_template(job.niche)
    if tpl is None:
        return job.message, None
    return tpl["text"], tpl["id"]


def _account_limit(job: Job, account: str) -> int:
    if not job.optimize:
        return DM_LIMIT_PER_ACCOUNT
    return health.remaining_today(account)


async def _account_worker(*, job: Job, session_path: str, queue: "asyncio.Queue[TargetState]") -> None:
    session_name = os.path.splitext(os.path.basename(session_path))[0]

    client = pool.get(session_name)
    if client is None:
        job.add_log(event="account_failed", account=session_name, error="not in pool (unauthorized?)")
        return

    an.ensure_account(session_name)

    if job.optimize and not health.is_available(session_name):
        job.add_log(event="account_skipped", account=session_name,
                    reason="отдыхает или выбыл (optimizer.health)")
        return

    limit = _account_limit(job, session_name)
    if limit <= 0:
        job.add_log(event="account_skipped", account=session_name,
                    reason="дневной лимит исчерпан")
        return

    job.add_log(event="account_started", account=session_name, limit_today=limit)

    sent_in_account = 0
    while not job.cancel.is_set() and sent_in_account < limit:
        try:
            target = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        key = target.target.lower().lstrip("@")
        if an.is_suppressed(key):
            _mark(job, target, "skipped", "suppressed (стоп-лист)", session_name)
            continue

        message, template_id = _pick_message(job)

        # --- резолв получателя ---
        try:
            entity = await client.get_entity(_resolve(target.target))
        except FloodWaitError as e:
            queue.put_nowait(target)
            health.on_error(session_name, "flood_wait", f"resolve {e.seconds}s")
            job.add_log(event="account_paused", account=session_name, reason=f"FloodWait {e.seconds}s")
            break
        except (UserDeactivatedBanError, AuthKeyUnregisteredError) as e:
            queue.put_nowait(target)
            health.on_error(session_name, "banned", str(e))
            job.add_log(event="account_dead", account=session_name, error=str(e))
            break
        except Exception as e:  # noqa: BLE001
            _mark(job, target, "skipped", f"resolve: {e}", session_name, template_id=template_id)
            continue

        peer_id = str(getattr(entity, "id", "") or "") or None
        if peer_id and an.is_suppressed(peer_id):
            _mark(job, target, "skipped", "suppressed (стоп-лист)", session_name, peer_id=peer_id)
            continue

        # --- отправка ---
        try:
            await client.send_message(entity, message)
            _mark(job, target, "sent", None, session_name,
                  peer_id=peer_id, template_id=template_id)
            sent_in_account += 1
        except UserPrivacyRestrictedError:
            _mark(job, target, "skipped", "privacy_restricted", session_name,
                  peer_id=peer_id, template_id=template_id)
        except FloodWaitError as e:
            queue.put_nowait(target)
            health.on_error(session_name, "flood_wait", f"send {e.seconds}s")
            job.add_log(event="account_paused", account=session_name, reason=f"FloodWait {e.seconds}s")
            break
        except PeerFloodError:
            queue.put_nowait(target)
            health.on_error(session_name, "peer_flood", "send")
            job.add_log(event="account_paused", account=session_name, reason="PeerFlood")
            break
        except (UserDeactivatedBanError, AuthKeyUnregisteredError) as e:
            queue.put_nowait(target)
            health.on_error(session_name, "banned", str(e))
            job.add_log(event="account_dead", account=session_name, error=str(e))
            break
        except UserBannedInChannelError as e:
            _mark(job, target, "failed", str(e), session_name, peer_id=peer_id,
                  template_id=template_id)
        except Exception as e:  # noqa: BLE001
            _mark(job, target, "skipped", str(e), session_name, peer_id=peer_id,
                  template_id=template_id)

        await asyncio.sleep(random.uniform(DM_DELAY_MIN, DM_DELAY_MAX))

    job.add_log(event="account_finished", account=session_name,
                sent_in_session=sent_in_account, limit_today=limit)


async def run_dm_job(job: Job) -> None:
    sessions = get_session_files()
    if not sessions:
        job.status = "failed"
        job.error = "Нет аккаунтов в sessions/"
        job.finished_at = datetime.now(timezone.utc)
        return

    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    job.add_log(event="job_started", total=job.total, accounts=len(sessions),
                parallel=job.parallel, kind="dm", niche=job.niche, optimize=job.optimize)

    queue: asyncio.Queue = asyncio.Queue()
    for t in job.targets:
        if t.status == "pending":
            await queue.put(t)

    parallel = max(1, min(job.parallel, len(sessions)))
    idx = 0
    idx_lock = asyncio.Lock()

    async def next_session() -> str | None:
        nonlocal idx
        async with idx_lock:
            if idx >= len(sessions):
                return None
            s = sessions[idx]
            idx += 1
            return s

    async def worker():
        while not queue.empty() and not job.cancel.is_set():
            sp = await next_session()
            if sp is None:
                return
            await _account_worker(job=job, session_path=sp, queue=queue)

    try:
        await asyncio.gather(*[asyncio.create_task(worker()) for _ in range(parallel)])
    except Exception as e:  # noqa: BLE001
        job.status = "failed"
        job.error = str(e)
        job.add_log(event="job_failed", error=str(e))
        job.finished_at = datetime.now(timezone.utc)
        return

    # цели, оставшиеся в очереди (не хватило дневной ёмкости) — вернём в pending
    leftover = queue.qsize()
    job.status = "stopped" if job.cancel.is_set() else "done"
    job.finished_at = datetime.now(timezone.utc)
    job.add_log(event="job_finished", status=job.status, backlog=leftover,
                sent=job.sent, failed=job.failed, skipped=job.skipped)
