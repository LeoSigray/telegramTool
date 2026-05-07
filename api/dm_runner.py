"""Async-runner для рассылки в личные сообщения. Использует существующий
messaging.dm_sender как образец, но с поддержкой отмены и событий."""
import asyncio
import os
import random

from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    UserPrivacyRestrictedError,
)
from datetime import datetime, timezone

from accounts.manager import create_client, get_session_files
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


def _mark(job: Job, t: TargetState, status: str, error: str | None, account: str) -> None:
    t.status = status
    t.error = error
    t.used_account = account
    t.processed_at = datetime.now(timezone.utc)
    if status == "sent":
        job.sent += 1
    elif status == "failed":
        job.failed += 1
    elif status == "skipped":
        job.skipped += 1
    job.add_log(event=status, target=t.target, account=account, error=error,
                sent=job.sent, failed=job.failed, skipped=job.skipped)


async def _account_worker(*, job: Job, session_path: str, queue: "asyncio.Queue[TargetState]") -> None:
    session_name = os.path.splitext(os.path.basename(session_path))[0]

    client = create_client(session_path)
    try:
        await client.start()
    except Exception as e:  # noqa: BLE001
        job.add_log(event="account_failed", account=session_name, error=str(e))
        return

    job.add_log(event="account_started", account=session_name)

    sent_in_account = 0
    try:
        while not job.cancel.is_set() and sent_in_account < DM_LIMIT_PER_ACCOUNT:
            try:
                target = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            try:
                resolved = _resolve(target.target)
                entity = await client.get_entity(resolved)
            except FloodWaitError as e:
                queue.put_nowait(target)
                job.add_log(event="account_paused", account=session_name, reason=f"FloodWait {e.seconds}s")
                break
            except Exception as e:  # noqa: BLE001
                _mark(job, target, "skipped", f"resolve: {e}", session_name)
                continue

            try:
                await client.send_message(entity, job.message)
                _mark(job, target, "sent", None, session_name)
                sent_in_account += 1
            except UserPrivacyRestrictedError:
                _mark(job, target, "skipped", "privacy_restricted", session_name)
            except FloodWaitError as e:
                queue.put_nowait(target)
                job.add_log(event="account_paused", account=session_name, reason=f"FloodWait {e.seconds}s")
                break
            except PeerFloodError:
                queue.put_nowait(target)
                job.add_log(event="account_paused", account=session_name, reason="PeerFlood")
                break
            except Exception as e:  # noqa: BLE001
                _mark(job, target, "skipped", str(e), session_name)

            await asyncio.sleep(random.uniform(DM_DELAY_MIN, DM_DELAY_MAX))
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    job.add_log(event="account_finished", account=session_name, sent_in_session=sent_in_account)


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
                parallel=job.parallel, kind="dm")

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

    job.status = "stopped" if job.cancel.is_set() else "done"
    job.finished_at = datetime.now(timezone.utc)
    job.add_log(event="job_finished", status=job.status,
                sent=job.sent, failed=job.failed, skipped=job.skipped)
