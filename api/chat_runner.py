"""
Async-runner для рассылки по чатам — повторяет логику messaging/chat_sender.py,
но с поддержкой отмены и событий вместо print().
"""
import asyncio
import os
import random
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChannelsTooMuchError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    PeerFloodError,
    SlowModeWaitError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
    UserNotParticipantError,
)
from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from accounts.manager import get_session_files
from .client_pool import pool
from config import (
    CHAT_DELAY_MAX,
    CHAT_DELAY_MIN,
    JOIN_DELAY_MAX,
    JOIN_DELAY_MIN,
)
from data.daily_limits import (
    DAILY_JOIN_LIMIT,
    DAILY_SEND_LIMIT,
    get_account_daily_stats,
    get_daily_joins_left,
    get_daily_sends_left,
    record_join,
    record_send,
)

from .jobs import Job, TargetState


async def _is_member(client: TelegramClient, entity) -> bool:
    try:
        me = await client.get_me()
        await client(GetParticipantRequest(entity, me.id))
        return True
    except (UserNotParticipantError, ChatAdminRequiredError):
        return False
    except Exception:  # noqa: BLE001
        return False


async def _join(client: TelegramClient, raw: str) -> tuple[bool, str]:
    try:
        if raw.startswith("+"):
            await client(ImportChatInviteRequest(raw[1:]))
        else:
            entity = await client.get_entity(f"@{raw}")
            await client(JoinChannelRequest(entity))
        return True, ""
    except UserAlreadyParticipantError:
        return True, ""
    except (InviteHashExpiredError, InviteHashInvalidError):
        return False, "invite_expired"
    except ChannelPrivateError:
        return False, "private"
    except ChannelsTooMuchError:
        return False, "too_many_channels"
    except UserBannedInChannelError:
        return False, "banned"
    except FloodWaitError as e:
        return False, f"FloodWait {e.seconds}s"
    except PeerFloodError:
        return False, "PeerFlood"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


async def _send(client: TelegramClient, entity, message: str) -> tuple[bool, str]:
    try:
        await client.send_message(entity, message)
        return True, ""
    except FloodWaitError as e:
        return False, f"FloodWait {e.seconds}s"
    except SlowModeWaitError as e:
        return False, f"SlowMode {e.seconds}s"
    except ChatWriteForbiddenError:
        return False, "write_forbidden"
    except ChannelPrivateError:
        return False, "private"
    except PeerFloodError:
        return False, "PeerFlood"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _is_fatal(error: str) -> bool:
    return "FloodWait" in error or error in ("PeerFlood", "too_many_channels", "banned")


def _normalize(raw: str) -> str:
    """t.me/foo → foo, @foo → foo, +invite → +invite"""
    s = raw.strip()
    if s.startswith("https://t.me/"):
        s = s.removeprefix("https://t.me/")
    elif s.startswith("http://t.me/"):
        s = s.removeprefix("http://t.me/")
    elif s.startswith("t.me/"):
        s = s.removeprefix("t.me/")
    s = s.lstrip("@")
    return s


def _mark(job: Job, target: TargetState, status: str, error: str | None, account: str) -> None:
    target.status = status
    target.error = error
    target.used_account = account
    target.processed_at = datetime.now(timezone.utc)
    if status == "sent":
        job.sent += 1
    elif status == "failed":
        job.failed += 1
    elif status == "skipped":
        job.skipped += 1
    job.add_log(
        event=status,
        target=target.target,
        account=account,
        error=error,
        sent=job.sent,
        failed=job.failed,
        skipped=job.skipped,
    )


async def _account_worker(
    *,
    job: Job,
    session_path: str,
    queue: "asyncio.Queue[TargetState]",
) -> None:
    session_name = os.path.splitext(os.path.basename(session_path))[0]

    daily = get_account_daily_stats(session_name)
    if daily["joins_left"] <= 0 and daily["sends_left"] <= 0:
        job.add_log(event="account_skip", account=session_name, reason="daily_limit")
        return

    client = pool.get(session_name)
    if client is None:
        job.add_log(event="account_failed", account=session_name, error="not in pool (unauthorized?)")
        return

    job.add_log(event="account_started", account=session_name,
                joins=daily["joins"], sends=daily["sends"])

    try:
        while not job.cancel.is_set():
            try:
                target = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            handle = _normalize(target.target)

            if get_daily_sends_left(session_name) <= 0:
                queue.put_nowait(target)
                job.add_log(event="account_paused", account=session_name, reason="daily_send_limit")
                break

            # Резолвим (для приватных ссылок entity не нужен — сразу join)
            entity = None
            if not handle.startswith("+"):
                try:
                    entity = await client.get_entity(f"@{handle}")
                except FloodWaitError as e:
                    queue.put_nowait(target)
                    job.add_log(event="account_paused", account=session_name, reason=f"FloodWait {e.seconds}s")
                    break
                except ChannelPrivateError:
                    _mark(job, target, "skipped", "private", session_name)
                    continue
                except Exception as e:  # noqa: BLE001
                    _mark(job, target, "skipped", str(e), session_name)
                    continue

            # Проверяем членство
            need_join = entity is None or not await _is_member(client, entity)

            if need_join:
                if get_daily_joins_left(session_name) <= 0:
                    queue.put_nowait(target)
                    job.add_log(event="account_paused", account=session_name, reason="daily_join_limit")
                    break

                ok, err = await _join(client, handle)
                if not ok:
                    if _is_fatal(err):
                        _mark(job, target, "failed", f"join: {err}", session_name)
                        queue.put_nowait(target)
                        job.add_log(event="account_paused", account=session_name, reason=err)
                        break
                    _mark(job, target, "skipped", f"join: {err}", session_name)
                    continue

                record_join(session_name)

                # Перезапросим entity после вступления (для приватных сразу не сможем — пропустим)
                if entity is None:
                    if handle.startswith("+"):
                        _mark(job, target, "skipped", "private_post_join", session_name)
                        continue
                    try:
                        entity = await client.get_entity(f"@{handle}")
                    except Exception as e:  # noqa: BLE001
                        _mark(job, target, "skipped", f"resolve: {e}", session_name)
                        continue

                await asyncio.sleep(random.uniform(JOIN_DELAY_MIN, JOIN_DELAY_MAX))
                if job.cancel.is_set():
                    queue.put_nowait(target)
                    return

            ok, err = await _send(client, entity, message=job.message)
            if ok:
                record_send(session_name)
                _mark(job, target, "sent", None, session_name)
            else:
                if _is_fatal(err):
                    _mark(job, target, "failed", err, session_name)
                    queue.put_nowait(target)
                    job.add_log(event="account_paused", account=session_name, reason=err)
                    break
                _mark(job, target, "skipped", err, session_name)

            await asyncio.sleep(random.uniform(CHAT_DELAY_MIN, CHAT_DELAY_MAX))
    finally:
        # клиент НЕ отключаем — он принадлежит пулу и продолжает слушать
        pass

    final = get_account_daily_stats(session_name)
    job.add_log(event="account_finished", account=session_name,
                joins=final["joins"], sends=final["sends"])


async def run_chat_job(job: Job) -> None:
    """Запускает рассылку. Помечает статусы job. Уважает job.cancel."""
    sessions = get_session_files()
    if not sessions:
        job.status = "failed"
        job.error = "Нет аккаунтов в sessions/"
        job.finished_at = datetime.now(timezone.utc)
        return

    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    job.add_log(event="job_started", total=job.total, accounts=len(sessions),
                parallel=job.parallel)

    queue: asyncio.Queue = asyncio.Queue()
    for t in job.targets:
        if t.status == "pending":
            await queue.put(t)

    parallel = max(1, min(job.parallel, len(sessions)))
    session_idx = 0
    idx_lock = asyncio.Lock()

    async def next_session() -> str | None:
        nonlocal session_idx
        async with idx_lock:
            if session_idx >= len(sessions):
                return None
            s = sessions[session_idx]
            session_idx += 1
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
