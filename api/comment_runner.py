"""
Async-runner для нейрокомментинга.

Алгоритм:
  1. Ищем каналы по ключевым словам (SearchRequest)
  2. Отсеиваем каналы без linked discussion group
  3. Параллельные воркеры (N = job.parallel) разбирают каналы из очереди
  4. Каждый воркер = один аккаунт: join → собрать посты → Gemini → send_message
  5. Ротация по лимиту COMMENT_LIMIT_PER_ACCOUNT, FloodWait-защита
  6. Continuous: ищет новые каналы каждые COMMENT_ROUND_DELAY секунд
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
from config import COMMENT_DELAY_MIN, COMMENT_DELAY_MAX, COMMENT_LIMIT_PER_ACCOUNT, COMMENT_ROUND_DELAY
from parsing.channel_searcher import search_channels_multi
from .client_pool import pool
from .gemini import generate_comment, is_configured
from .jobs import Job, TargetState

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
#  Вспомогательные функции
# ──────────────────────────────────────────────────────────────────────────

async def _interruptible_sleep(job: Job, seconds: int) -> None:
    """Ждёт seconds секунд, прерывается если job.cancel установлен (проверка каждые 5с)."""
    elapsed = 0
    while elapsed < seconds:
        if job.cancel.is_set():
            return
        chunk = min(5, seconds - elapsed)
        await asyncio.sleep(chunk)
        elapsed += chunk


async def _send_comment(client, job: Job, ch, post, comment_text: str, acc_name: str):
    """
    Пробует отправить комментарий.
    Возвращает:
      True  — успешно
      False — пропускаем (нет треда и т.п.)
      None  — FloodWait / PeerFlood (нужно прервать аккаунт)
    """
    async def _do_send():
        await client.send_message(
            entity=f"@{ch.username}",
            message=comment_text,
            comment_to=post.id,
        )

    try:
        await _do_send()
        return True
    except Exception as err:
        err_str = str(err)

        # Нужно вступить в discussion group — join и retry
        if "join" in err_str.lower() and "discussion" in err_str.lower() and ch.linked_chat_id:
            try:
                disc_entity = await client.get_entity(ch.linked_chat_id)
                await client(JoinChannelRequest(disc_entity))
                log.info("re-join discussion @%s, waiting 5s", ch.username)
                await asyncio.sleep(5)
                await _do_send()
                return True
            except Exception as retry_err:
                job.add_log(event="comment_error", channel=ch.username,
                            post_id=post.id, error=f"retry: {retry_err}")
                return False

        # Пост слишком старый — нет треда
        if "GetDiscussionMessageRequest" in err_str or "message ID" in err_str.lower():
            return False

        if isinstance(err, FloodWaitError):
            job.add_log(event="flood_wait", account=acc_name,
                        channel=ch.username, seconds=err.seconds)
            await asyncio.sleep(min(err.seconds, 300))
            return None

        if isinstance(err, PeerFloodError):
            job.add_log(event="peer_flood", account=acc_name, channel=ch.username)
            return None

        if isinstance(err, (ChannelPrivateError, UserBannedInChannelError)):
            job.add_log(event="comment_forbidden", channel=ch.username, error=err_str)
            return None

        job.add_log(event="comment_error", channel=ch.username,
                    post_id=post.id, error=err_str)
        return False


# ──────────────────────────────────────────────────────────────────────────
#  Воркер одного аккаунта
# ──────────────────────────────────────────────────────────────────────────

async def _account_worker(
    *,
    job: Job,
    acc_name: str,
    channel_queue: "asyncio.Queue[tuple]",
    style_prompt: str,
    posts_per_channel: int,
    round_num: int,
) -> None:
    """
    Один воркер = один аккаунт.
    Разбирает каналы из channel_queue пока не исчерпает лимит или очередь.
    """
    client = pool.get(acc_name)
    if client is None:
        job.add_log(event="account_unavailable", account=acc_name, round=round_num)
        return

    job.add_log(event="account_started", account=acc_name, round=round_num)
    comment_count = 0

    while not job.cancel.is_set() and comment_count < COMMENT_LIMIT_PER_ACCOUNT:
        # Берём следующий канал из очереди
        try:
            ch, target = channel_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        job.add_log(event="processing_channel", channel=ch.username, account=acc_name,
                    subscribers=ch.subscribers, round=round_num)
        print(f"  [{acc_name}] → @{ch.username} ({ch.subscribers} подп.)", flush=True)

        # Вступаем в linked discussion group
        if ch.linked_chat_id:
            try:
                disc_entity = await client.get_entity(ch.linked_chat_id)
                await client(JoinChannelRequest(disc_entity))
                print(f"  [{acc_name}] Вступили в группу обсуждений", flush=True)
                await asyncio.sleep(3)
            except UserAlreadyParticipantError:
                pass
            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds, 60))
            except Exception as e:
                job.add_log(event="join_discussion_error", channel=ch.username, error=str(e))

        # Собираем свежие посты
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
                target.status = "skipped"
                target.error = "нет текстовых постов"
                target.used_account = acc_name
                target.processed_at = datetime.now(timezone.utc)
                job.skipped += 1
                channel_queue.task_done()
                continue

        except (ChannelPrivateError, UserBannedInChannelError) as e:
            target.status = "skipped"
            target.error = str(e)
            target.used_account = acc_name
            target.processed_at = datetime.now(timezone.utc)
            job.skipped += 1
            channel_queue.task_done()
            continue
        except FloodWaitError as e:
            job.add_log(event="flood_wait", account=acc_name, seconds=e.seconds)
            await asyncio.sleep(e.seconds)
            target.status = "skipped"
            target.error = f"FloodWait {e.seconds}s"
            target.used_account = acc_name
            target.processed_at = datetime.now(timezone.utc)
            job.skipped += 1
            channel_queue.task_done()
            break  # аккаунт перегружен, прекращаем
        except Exception as e:
            target.status = "skipped"
            target.error = f"iter_messages: {e}"
            target.used_account = acc_name
            target.processed_at = datetime.now(timezone.utc)
            job.skipped += 1
            channel_queue.task_done()
            continue

        # Комментируем посты канала
        commented_this_channel = 0
        for post in posts:
            if job.cancel.is_set():
                break
            if comment_count >= COMMENT_LIMIT_PER_ACCOUNT:
                break

            try:
                comment_text = await generate_comment(post.text, style_prompt)
            except Exception as e:
                job.add_log(event="gemini_error", channel=ch.username,
                            post_id=post.id, error=str(e))
                continue

            sent_ok = await _send_comment(client, job, ch, post, comment_text, acc_name)
            if sent_ok is True:
                commented_this_channel += 1
                comment_count += 1
                job.sent += 1
                print(f"  [{acc_name}] ✓ @{ch.username}: «{comment_text[:55]}…»", flush=True)
                job.add_log(
                    event="commented",
                    channel=ch.username,
                    post_id=post.id,
                    comment_preview=comment_text[:100],
                    account=acc_name,
                    sent=job.sent,
                    round=round_num,
                )
                await asyncio.sleep(random.uniform(COMMENT_DELAY_MIN, COMMENT_DELAY_MAX))
            elif sent_ok is None:
                # FloodWait / PeerFlood — прекращаем работу этого аккаунта
                channel_queue.task_done()
                job.add_log(event="account_paused", account=acc_name,
                            reason="flood", round=round_num)
                return

        # Итог по каналу
        target.used_account = acc_name
        target.processed_at = datetime.now(timezone.utc)
        if commented_this_channel > 0:
            target.status = "sent"
        else:
            target.status = "skipped"
            target.error = "не удалось оставить ни одного комментария"
            job.skipped += 1

        channel_queue.task_done()

        # Небольшая пауза между каналами
        if not job.cancel.is_set() and not channel_queue.empty():
            await asyncio.sleep(random.uniform(5, 12))

    job.add_log(event="account_finished", account=acc_name,
                comments=comment_count, round=round_num)


# ──────────────────────────────────────────────────────────────────────────
#  Главный runner
# ──────────────────────────────────────────────────────────────────────────

async def run_comment_job(
    job: Job,
    keywords: list[str],
    min_subs: int,
    posts_per_channel: int,
    continuous: bool = False,
) -> None:
    """
    Запускает нейрокомментинг с поддержкой параллельных аккаунтов.

    job.parallel — сколько аккаунтов работают одновременно.
    job.message  — style_prompt для Gemini.
    continuous   — бесконечный режим: ищет новые каналы каждые COMMENT_ROUND_DELAY сек.
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

    # Все доступные имена аккаунтов (из sessions/ и пула)
    all_accounts = [os.path.splitext(os.path.basename(sp))[0] for sp in sessions]
    seen_channel_ids: set[int] = set()  # ID каналов, уже добавленных как targets

    # ═══ ОСНОВНОЙ ЦИКЛ (1 итерация = 1 раунд) ═══════════════════════════
    while True:
        if job.cancel.is_set():
            break

        job.current_round += 1
        round_label = f"Раунд {job.current_round}" if continuous else "Поиск"
        print(f"\n=== {round_label} ===", flush=True)

        # ── Получаем клиент для поиска каналов ──────────────────────────
        search_client = None
        for name in all_accounts:
            c = pool.get(name)
            if c:
                search_client = c
                break

        if search_client is None:
            job.add_log(event="no_clients_available", round=job.current_round)
            if not continuous:
                job.status = "failed"
                job.error = "Ни один аккаунт не доступен в пуле"
                job.finished_at = datetime.now(timezone.utc)
                return
            await _interruptible_sleep(job, 120)
            continue

        # ── Поиск каналов ────────────────────────────────────────────────
        job.add_log(event="searching_channels", keywords=keywords, min_subs=min_subs,
                    round=job.current_round)
        all_channels = await search_channels_multi(
            search_client, keywords, min_subs=min_subs, require_comments=True
        )

        new_channels = [c for c in all_channels if c.channel_id not in seen_channel_ids]

        job.add_log(
            event="channels_found",
            total=len(all_channels),
            new=len(new_channels),
            round=job.current_round,
            channels=[f"@{c.username} ({c.subscribers})" for c in new_channels],
        )
        print(f"  Найдено: {len(all_channels)} каналов, {len(new_channels)} новых", flush=True)

        if not new_channels:
            if not continuous:
                job.status = "done"
                job.error = "Каналы с включёнными комментариями не найдены"
                job.finished_at = datetime.now(timezone.utc)
                return
            print(f"  Нет новых каналов, ждём {COMMENT_ROUND_DELAY // 60} мин...", flush=True)
            job.add_log(event="no_new_channels_waiting", seconds=COMMENT_ROUND_DELAY,
                        round=job.current_round)
            await _interruptible_sleep(job, COMMENT_ROUND_DELAY)
            continue

        if job.cancel.is_set():
            break

        # ── Добавляем каналы как targets и строим очередь ────────────────
        channel_queue: asyncio.Queue = asyncio.Queue()
        for ch in new_channels:
            seen_channel_ids.add(ch.channel_id)
            target = TargetState(target=f"@{ch.username}")
            job.targets.append(target)
            channel_queue.put_nowait((ch, target))

        # ── Выбираем аккаунты для параллельной работы ────────────────────
        available = [name for name in all_accounts if pool.get(name) is not None]
        parallel = max(1, min(job.parallel, len(available)))

        print(
            f"  Комментируем {len(new_channels)} каналов "
            f"| {parallel} аккаунт(ов) параллельно",
            flush=True,
        )
        job.add_log(
            event="round_started",
            round=job.current_round,
            channels=len(new_channels),
            parallel=parallel,
            accounts=available[:parallel],
        )

        # ── Запускаем параллельных воркеров ──────────────────────────────
        workers = [
            _account_worker(
                job=job,
                acc_name=acc_name,
                channel_queue=channel_queue,
                style_prompt=style_prompt,
                posts_per_channel=posts_per_channel,
                round_num=job.current_round,
            )
            for acc_name in available[:parallel]
        ]
        await asyncio.gather(*[asyncio.create_task(w) for w in workers])

        # ── Конец раунда ─────────────────────────────────────────────────
        job.add_log(
            event="round_complete",
            round=job.current_round,
            sent=job.sent,
            channels_this_round=len(new_channels),
        )

        if not continuous or job.cancel.is_set():
            break

        mins = COMMENT_ROUND_DELAY // 60
        print(f"\n  ✓ Раунд {job.current_round} завершён. Следующий через {mins} мин.", flush=True)
        job.add_log(event="waiting_next_round", seconds=COMMENT_ROUND_DELAY,
                    round=job.current_round)
        await _interruptible_sleep(job, COMMENT_ROUND_DELAY)

    # ═══ ЗАВЕРШЕНИЕ ══════════════════════════════════════════════════════
    job.status = "stopped" if job.cancel.is_set() else "done"
    job.finished_at = datetime.now(timezone.utc)
    job.add_log(
        event="job_finished",
        status=job.status,
        sent=job.sent,
        failed=job.failed,
        skipped=job.skipped,
        rounds_total=job.current_round,
    )
