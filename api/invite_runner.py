"""Async invite-runner: пригласить пользователей в группу. Использует пул клиентов."""
import asyncio
import os
import random
from datetime import datetime, timezone

from telethon.errors import (
    ChatAdminRequiredError,
    FloodWaitError,
    PeerFloodError,
    UserAlreadyParticipantError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest

from accounts.manager import get_session_files
from config import INVITE_DELAY_MAX, INVITE_DELAY_MIN, INVITE_LIMIT_PER_ACCOUNT

from .client_pool import pool
from .jobs import Job, TargetState


def _resolve_user(raw: str):
    s = raw.strip().lstrip("@")
    if s.startswith("+") and s[1:].isdigit():
        return s
    if s.isdigit():
        return int(s)
    return f"@{s}"


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


async def _account_worker(*, job: Job, group: str, session_path: str,
                          queue: "asyncio.Queue[TargetState]") -> None:
    session_name = os.path.splitext(os.path.basename(session_path))[0]
    client = pool.get(session_name)
    if client is None:
        job.add_log(event="account_failed", account=session_name, error="not in pool")
        return

    job.add_log(event="account_started", account=session_name)

    # Резолвим группу
    try:
        group_entity = await client.get_entity(_resolve_user(group))
        is_megagroup = bool(getattr(group_entity, "megagroup", False))
    except Exception as e:  # noqa: BLE001
        job.add_log(event="account_failed", account=session_name, error=f"resolve group: {e}")
        return

    invited_in_account = 0
    try:
        while not job.cancel.is_set() and invited_in_account < INVITE_LIMIT_PER_ACCOUNT:
            try:
                target = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            try:
                user_entity = await client.get_entity(_resolve_user(target.target))
            except FloodWaitError as e:
                queue.put_nowait(target)
                job.add_log(event="account_paused", account=session_name, reason=f"FloodWait {e.seconds}s")
                break
            except Exception as e:  # noqa: BLE001
                _mark(job, target, "skipped", f"resolve: {e}", session_name)
                continue

            try:
                if is_megagroup:
                    await client(InviteToChannelRequest(group_entity, [user_entity]))
                else:
                    await client(AddChatUserRequest(group_entity, user_entity, fwd_limit=0))
                _mark(job, target, "sent", None, session_name)
                invited_in_account += 1
            except UserAlreadyParticipantError:
                _mark(job, target, "skipped", "already_participant", session_name)
            except UserNotMutualContactError:
                _mark(job, target, "skipped", "not_mutual_contact", session_name)
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
            except ChatAdminRequiredError:
                queue.put_nowait(target)
                job.add_log(event="account_paused", account=session_name, reason="admin_required")
                break
            except Exception as e:  # noqa: BLE001
                _mark(job, target, "skipped", str(e), session_name)

            await asyncio.sleep(random.uniform(INVITE_DELAY_MIN, INVITE_DELAY_MAX))
    finally:
        pass

    job.add_log(event="account_finished", account=session_name, invited=invited_in_account)


async def run_invite_job(job: Job, group: str) -> None:
    sessions = get_session_files()
    if not sessions:
        job.status = "failed"
        job.error = "Нет аккаунтов в sessions/"
        job.finished_at = datetime.now(timezone.utc)
        return

    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    job.add_log(event="job_started", total=job.total, accounts=len(sessions),
                parallel=job.parallel, kind="invite", group=group)

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
            await _account_worker(job=job, group=group, session_path=sp, queue=queue)

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
    job.add_log(event="job_finished", status=job.status, sent=job.sent,
                failed=job.failed, skipped=job.skipped)
