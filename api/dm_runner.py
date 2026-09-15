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

try:  # есть не во всех версиях telethon
    from telethon.errors import UserIsBlockedError
except ImportError:  # pragma: no cover
    class UserIsBlockedError(Exception):
        pass
from datetime import datetime, timezone

from accounts.manager import get_session_files
from data import analytics as an
from data import users_manager as um
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

    # кому реально написали — убираем из базы Users, чтобы не написать повторно
    if status == "sent":
        try:
            um.remove_user(t.target)
        except Exception as e:  # noqa: BLE001 — сбой очистки не должен ронять рассылку
            job.add_log(event="users_cleanup_error", target=t.target, error=str(e))

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
    """
    Сколько сообщений с аккаунта можно отправить в этом запуске.
    Не путать с is_available() ниже — тот уже отсеивает отдыхающие/мёртвые
    аккаунты безусловно; здесь — просто дневная цифра для активных.
    В режиме без ниши (optimize=False) — статичный DM_LIMIT_PER_ACCOUNT,
    без учёта возрастного ramp'а из optimizer.health (он рассчитан на
    работу вместе с бандитом/планировщиком; сам по себе, без них, может
    неожиданно урезать лимит только что купленным аккаунтам без метаданных).
    """
    if not job.optimize:
        return DM_LIMIT_PER_ACCOUNT
    return health.remaining_today(account)


async def _account_worker(*, job: Job, session_path: str, queue: "asyncio.Queue[TargetState]") -> None:
    session_name = os.path.splitext(os.path.basename(session_path))[0]

    client = pool.get(session_name)
    if client is None:
        job.add_log(event="account_failed", account=session_name, error="not in pool (unauthorized?)")
        print(f"  ⏭ [{session_name}] не в пуле (не авторизован) — пропущен")
        return

    an.ensure_account(session_name)

    # Проверка "отдыхает/выбыл" — всегда, а не только в режиме оптимизатора.
    # Иначе аккаунт, который только что словил PeerFlood (48ч отдыха, см.
    # optimizer/health.REST_HOURS), тут же берётся заново в следующем запуске.
    if not health.is_available(session_name):
        job.add_log(event="account_skipped", account=session_name,
                    reason="отдыхает или выбыл (optimizer.health)")
        print(f"  ⏭ [{session_name}] отдыхает/выбыл — пропущен")
        return

    limit = _account_limit(job, session_name)
    if limit <= 0:
        job.add_log(event="account_skipped", account=session_name,
                    reason="дневной лимит исчерпан")
        print(f"  ⏭ [{session_name}] дневной лимит уже исчерпан — пропущен")
        return

    job.add_log(event="account_started", account=session_name, limit_today=limit)
    print(f"  ▶ [{session_name}] начинает (лимит на сегодня: {limit})")

    # Локальная статистика ИМЕННО этого аккаунта (job.sent/skipped/failed —
    # общие на всю рассылку, для одного аккаунта не годятся). stop_reason —
    # для итоговой строки, что вывела его из работы.
    stats = {"sent": 0, "skipped": 0, "failed": 0}
    stop_reason = "не осталось целей в очереди"

    while not job.cancel.is_set() and stats["sent"] < limit:
        try:
            target = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        key = target.target.lower().lstrip("@")
        if an.is_suppressed(key):
            _mark(job, target, "skipped", "suppressed (стоп-лист)", session_name)
            stats["skipped"] += 1
            continue

        message, template_id = _pick_message(job)

        # --- резолв получателя ---
        try:
            entity = await client.get_entity(_resolve(target.target))
        except FloodWaitError as e:
            queue.put_nowait(target)
            health.on_error(session_name, "flood_wait", f"resolve {e.seconds}s")
            job.add_log(event="account_paused", account=session_name, reason=f"FloodWait {e.seconds}s")
            stop_reason = f"FloodWait при резолве получателя ({e.seconds} сек)"
            break
        except (UserDeactivatedBanError, AuthKeyUnregisteredError) as e:
            queue.put_nowait(target)
            health.on_error(session_name, "banned", str(e))
            job.add_log(event="account_dead", account=session_name, error=str(e))
            stop_reason = f"аккаунт мёртв/забанен ({e})"
            break
        except Exception as e:  # noqa: BLE001
            # ВАЖНО: пауза нужна и здесь. Раньше тут стоял голый continue —
            # он перепрыгивал через sleep в конце цикла, и аккаунт выпаливал
            # проваленные резолвы очередью по ~1 в секунду. Для антиспама
            # Telegram это подпись бота-скрапера и главная причина мгновенного
            # PeerFlood: неудачный резолв — это тоже запрос к Telegram.
            _mark(job, target, "skipped", f"resolve: {e}", session_name, template_id=template_id)
            stats["skipped"] += 1
            await asyncio.sleep(random.uniform(DM_DELAY_MIN, DM_DELAY_MAX))
            continue

        peer_id = str(getattr(entity, "id", "") or "") or None
        if peer_id and an.is_suppressed(peer_id):
            # Стоп-лист — локальная проверка, к Telegram не ходили, пауза не нужна.
            _mark(job, target, "skipped", "suppressed (стоп-лист)", session_name, peer_id=peer_id)
            stats["skipped"] += 1
            continue

        # --- отправка ---
        try:
            await client.send_message(entity, message)
            _mark(job, target, "sent", None, session_name,
                  peer_id=peer_id, template_id=template_id)
            stats["sent"] += 1
        except (UserIsBlockedError, UserPrivacyRestrictedError) as e:
            # первое сообщение не дошло: заблокировали / закрытая приватность —
            # это исход «blocked» для дашборда ответов
            reason = "blocked" if isinstance(e, UserIsBlockedError) else "privacy_restricted"
            _mark(job, target, "skipped", reason, session_name,
                  peer_id=peer_id, template_id=template_id)
            stats["skipped"] += 1
            if target.send_id:
                try:
                    an.mark_send_blocked(target.send_id, reason)
                except Exception:  # noqa: BLE001
                    pass
        except FloodWaitError as e:
            queue.put_nowait(target)
            health.on_error(session_name, "flood_wait", f"send {e.seconds}s")
            job.add_log(event="account_paused", account=session_name, reason=f"FloodWait {e.seconds}s")
            stop_reason = f"FloodWait при отправке ({e.seconds} сек)"
            break
        except PeerFloodError:
            queue.put_nowait(target)
            health.on_error(session_name, "peer_flood", "send")
            job.add_log(event="account_paused", account=session_name, reason="PeerFlood")
            stop_reason = "PeerFlood"
            break
        except (UserDeactivatedBanError, AuthKeyUnregisteredError) as e:
            queue.put_nowait(target)
            health.on_error(session_name, "banned", str(e))
            job.add_log(event="account_dead", account=session_name, error=str(e))
            stop_reason = f"аккаунт мёртв/забанен ({e})"
            break
        except UserBannedInChannelError as e:
            _mark(job, target, "failed", str(e), session_name, peer_id=peer_id,
                  template_id=template_id)
            stats["failed"] += 1
        except Exception as e:  # noqa: BLE001
            _mark(job, target, "skipped", str(e), session_name, peer_id=peer_id,
                  template_id=template_id)
            stats["skipped"] += 1

        await asyncio.sleep(random.uniform(DM_DELAY_MIN, DM_DELAY_MAX))
    else:
        # while закончился САМ (не через break) — либо упёрлись в лимит, либо отменили
        if job.cancel.is_set():
            stop_reason = "остановлено пользователем (Ctrl+C)"
        elif stats["sent"] >= limit:
            stop_reason = f"дневной лимит аккаунта исчерпан ({limit})"

    job.add_log(event="account_finished", account=session_name,
                sent_in_session=stats["sent"], limit_today=limit)

    print(f"  ⏹ [{session_name}] готово — отправлено: {stats['sent']}, "
          f"пропущено: {stats['skipped']}, ошибок: {stats['failed']} "
          f"| причина остановки: {stop_reason}")


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
        # Пауза при смене аккаунта убрана: на практике не спасала от PeerFlood
        # (аккаунты флудятся почти мгновенно независимо от неё — см. общий IP,
        # README/чат) и только зря удлиняла прогон. Пауза МЕЖДУ сообщениями
        # одного аккаунта (DM_DELAY_MIN/MAX) остаётся — она в _account_worker.
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
