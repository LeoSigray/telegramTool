"""
Async-runner для нейрокомментинга.

Алгоритм:
  1. Ищем каналы по ключевым словам (SearchRequest)
  2. Отсеиваем каналы без linked discussion group
  3. Берём свежие посты из каждого канала
  4. Для каждого поста — Gemini генерирует осмысленный комментарий
  5. Отправляем через send_message(channel, text, comment_to=post_id)
  6. Ротация аккаунтов, задержки, лимиты
"""
import asyncio
import logging
import os
import random
from datetime import datetime, timezone

from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    PeerFloodError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
)
from telethon.tl.functions.channels import JoinChannelRequest

from accounts.manager import get_session_files
from config import COMMENT_DELAY_MIN, COMMENT_DELAY_MAX, COMMENT_LIMIT_PER_ACCOUNT
from parsing.channel_searcher import search_channels_multi
from .client_pool import pool
from .gemini import generate_comment, is_configured
from .jobs import Job, TargetState

log = logging.getLogger(__name__)


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


async def run_comment_job(
    job: Job,
    keywords: list[str],
    min_subs: int,
    posts_per_channel: int,
) -> None:
    """
    Запускает нейрокомментинг.
    job.message = style_prompt (инструкция для Gemini).
    """
    sessions = get_session_files()
    if not sessions:
        job.status = "failed"
        job.error = "Нет аккаунтов в sessions/"
        job.finished_at = datetime.now(timezone.utc)
        return

    if not is_configured():
        job.status = "failed"
        job.error = "GEMINI_API_KEY не задан в .env"
        job.finished_at = datetime.now(timezone.utc)
        return

    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    style_prompt = job.message

    # ── Шаг 1: поиск каналов ────────────────────────────────────────────
    job.add_log(event="searching_channels", keywords=keywords, min_subs=min_subs)

    # Берём первый доступный аккаунт для поиска
    search_client = None
    for sp in sessions:
        name = os.path.splitext(os.path.basename(sp))[0]
        c = pool.get(name)
        if c:
            search_client = c
            search_session = name
            break

    if search_client is None:
        job.status = "failed"
        job.error = "Ни один аккаунт не доступен в пуле"
        job.finished_at = datetime.now(timezone.utc)
        return

    channels = await search_channels_multi(
        search_client, keywords, min_subs=min_subs, require_comments=True
    )

    if not channels:
        job.status = "done"
        job.error = "Каналы с включёнными комментариями не найдены"
        job.finished_at = datetime.now(timezone.utc)
        return

    job.add_log(
        event="channels_found",
        count=len(channels),
        channels=[f"@{c.username} ({c.subscribers})" for c in channels],
    )

    # Добавляем найденные каналы как targets
    for ch in channels:
        job.targets.append(TargetState(target=f"@{ch.username}"))

    if job.cancel.is_set():
        job.status = "stopped"
        job.finished_at = datetime.now(timezone.utc)
        return

    # ── Шаг 2: комментируем ─────────────────────────────────────────────
    account_cycle = [
        os.path.splitext(os.path.basename(sp))[0] for sp in sessions
    ]
    acc_index = 0
    account_comment_count: dict[str, int] = {}

    for i, (ch, target) in enumerate(zip(channels, job.targets)):
        if job.cancel.is_set():
            break

        # Выбираем аккаунт (round-robin, пропускаем перегруженные)
        acc_name = None
        for _ in range(len(account_cycle)):
            candidate = account_cycle[acc_index % len(account_cycle)]
            acc_index += 1
            if account_comment_count.get(candidate, 0) < COMMENT_LIMIT_PER_ACCOUNT:
                if pool.get(candidate) is not None:
                    acc_name = candidate
                    break

        if acc_name is None:
            job.add_log(event="all_accounts_exhausted", channel=ch.username)
            _mark(job, target, "skipped", "все аккаунты исчерпали лимит", "—")
            continue

        client = pool.get(acc_name)
        job.add_log(event="processing_channel", channel=ch.username, account=acc_name,
                    subscribers=ch.subscribers)

        # Вступаем в linked discussion group (нужно для комментирования)
        if ch.linked_chat_id:
            try:
                await client(JoinChannelRequest(ch.linked_chat_id))
                print(f"  Вступили в группу обсуждений @{ch.username}", flush=True)
                await asyncio.sleep(2)
            except UserAlreadyParticipantError:
                pass
            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds, 60))
            except Exception as e:
                job.add_log(event="join_discussion_error", channel=ch.username, error=str(e))

        # Собираем свежие посты с текстом
        try:
            posts = []
            async for msg in client.iter_messages(f"@{ch.username}", limit=posts_per_channel * 3):
                if job.cancel.is_set():
                    break
                if msg.text and len(msg.text.strip()) > 30:
                    posts.append(msg)
                if len(posts) >= posts_per_channel:
                    break

            if not posts:
                _mark(job, target, "skipped", "нет текстовых постов", acc_name)
                continue

        except (ChannelPrivateError, UserBannedInChannelError) as e:
            _mark(job, target, "skipped", str(e), acc_name)
            continue
        except FloodWaitError as e:
            job.add_log(event="flood_wait", account=acc_name, seconds=e.seconds)
            await asyncio.sleep(e.seconds)
            _mark(job, target, "skipped", f"FloodWait {e.seconds}s", acc_name)
            continue
        except Exception as e:
            _mark(job, target, "skipped", f"iter_messages: {e}", acc_name)
            continue

        # Комментируем каждый пост
        commented_this_channel = 0
        for post in posts:
            if job.cancel.is_set():
                break
            if account_comment_count.get(acc_name, 0) >= COMMENT_LIMIT_PER_ACCOUNT:
                break

            # Генерируем комментарий через Gemini
            try:
                comment_text = await generate_comment(post.text, style_prompt)
            except Exception as e:
                job.add_log(event="gemini_error", channel=ch.username,
                            post_id=post.id, error=str(e))
                continue

            # Отправляем комментарий (Telethon автоматически находит linked group)
            try:
                await client.send_message(
                    entity=f"@{ch.username}",
                    message=comment_text,
                    comment_to=post.id,
                )
                commented_this_channel += 1
                account_comment_count[acc_name] = account_comment_count.get(acc_name, 0) + 1
                job.sent += 1
                print(f"  ✓ Комментарий в @{ch.username}: «{comment_text[:60]}»", flush=True)
                job.add_log(
                    event="commented",
                    channel=ch.username,
                    post_id=post.id,
                    comment_preview=comment_text[:100],
                    account=acc_name,
                    sent=job.sent,
                )
            except FloodWaitError as e:
                job.add_log(event="flood_wait", account=acc_name,
                            channel=ch.username, seconds=e.seconds)
                await asyncio.sleep(min(e.seconds, 300))
                break
            except PeerFloodError:
                job.add_log(event="peer_flood", account=acc_name, channel=ch.username)
                break
            except (ChannelPrivateError, UserBannedInChannelError) as e:
                job.add_log(event="comment_forbidden", channel=ch.username, error=str(e))
                break
            except Exception as e:
                err = str(e)
                # Пост слишком старый — нет треда в discussion group, просто пропускаем
                if "GetDiscussionMessageRequest" in err or "message ID" in err.lower():
                    job.add_log(event="post_no_thread", channel=ch.username, post_id=post.id)
                    continue
                job.add_log(event="comment_error", channel=ch.username,
                            post_id=post.id, error=err)
                continue

            # Задержка между комментариями
            await asyncio.sleep(random.uniform(COMMENT_DELAY_MIN, COMMENT_DELAY_MAX))

        # Итог по каналу
        if commented_this_channel > 0:
            target.status = "sent"
            target.used_account = acc_name
            target.processed_at = datetime.now(timezone.utc)
        else:
            _mark(job, target, "skipped", "не удалось оставить ни одного комментария", acc_name)

        # Задержка между каналами
        if i < len(channels) - 1 and not job.cancel.is_set():
            await asyncio.sleep(random.uniform(5, 12))

    job.status = "stopped" if job.cancel.is_set() else "done"
    job.finished_at = datetime.now(timezone.utc)
    job.add_log(
        event="job_finished",
        status=job.status,
        sent=job.sent,
        failed=job.failed,
        skipped=job.skipped,
        channels_total=len(channels),
    )
