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
from config import COMMENT_DELAY_MIN, COMMENT_DELAY_MAX, COMMENT_LIMIT_PER_ACCOUNT, COMMENT_ROUND_DELAY
from parsing.channel_searcher import search_channels_multi
from .client_pool import pool
from .gemini import generate_comment, is_configured
from .jobs import Job, TargetState

log = logging.getLogger(__name__)


async def _interruptible_sleep(job: Job, seconds: int) -> None:
    """Ждёт seconds секунд, но прерывается если job.cancel установлен (проверка каждые 5с)."""
    elapsed = 0
    while elapsed < seconds:
        if job.cancel.is_set():
            return
        chunk = min(5, seconds - elapsed)
        await asyncio.sleep(chunk)
        elapsed += chunk


async def _send_comment(client, job, ch, post, comment_text: str, acc_name: str):
    """
    Пробует отправить комментарий. Если Telegram говорит "join discussion group" —
    делает повторный join + ретрай.
    Возвращает:
      True  — успешно отправлено
      False — пост пропущен (нет треда, join не помог и т.п.)
      None  — FloodWait / PeerFlood (нужно прервать обработку канала)
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
                print(f"  Повторный join группы @{ch.username}, ждём 5с...", flush=True)
                await asyncio.sleep(5)
                await _do_send()
                return True
            except Exception as retry_err:
                job.add_log(event="comment_error", channel=ch.username,
                            post_id=post.id, error=f"retry: {retry_err}")
                return False

        # Пост слишком старый — нет треда, молча пропускаем
        if "GetDiscussionMessageRequest" in err_str or "message ID" in err_str.lower():
            job.add_log(event="post_no_thread", channel=ch.username, post_id=post.id)
            return False

        # FloodWait — возвращаем None чтобы прервать обработку канала
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
    continuous: bool = False,
) -> None:
    """
    Запускает нейрокомментинг.
    job.message = style_prompt (инструкция для Gemini).
    continuous=True — крутится бесконечно: ищет новые каналы, ждёт COMMENT_ROUND_DELAY,
    повторяет. Уже прокомментированные каналы пропускает, новые добавляет.
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

    account_cycle = [os.path.splitext(os.path.basename(sp))[0] for sp in sessions]
    seen_channel_ids: set[int] = set()   # ID каналов, уже добавленных как targets

    # ═══ ОСНОВНОЙ ЦИКЛ (1 итерация = 1 раунд) ═══════════════════════════
    while True:
        if job.cancel.is_set():
            break

        job.current_round += 1
        round_label = f"Раунд {job.current_round}" if continuous else "Поиск"
        print(f"\n=== {round_label} ===", flush=True)

        # ── Поиск аккаунта для поиска ───────────────────────────────────
        search_client = None
        for sp in sessions:
            name = os.path.splitext(os.path.basename(sp))[0]
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
            # В continuous режиме просто ждём и пробуем снова
            await _interruptible_sleep(job, 120)
            continue

        # ── Поиск каналов ───────────────────────────────────────────────
        job.add_log(event="searching_channels", keywords=keywords, min_subs=min_subs,
                    round=job.current_round)
        all_channels = await search_channels_multi(
            search_client, keywords, min_subs=min_subs, require_comments=True
        )

        # Отделяем новые каналы от уже обработанных
        new_channels = [c for c in all_channels if c.channel_id not in seen_channel_ids]

        job.add_log(
            event="channels_found",
            total=len(all_channels),
            new=len(new_channels),
            round=job.current_round,
            channels=[f"@{c.username} ({c.subscribers})" for c in new_channels],
        )
        print(f"  Найдено каналов: {len(all_channels)} всего, {len(new_channels)} новых", flush=True)

        if not new_channels:
            if not continuous:
                job.status = "done"
                job.error = "Каналы с включёнными комментариями не найдены"
                job.finished_at = datetime.now(timezone.utc)
                return
            # В continuous режиме — ждём и ищем снова
            print(f"  Нет новых каналов, ждём {COMMENT_ROUND_DELAY // 60} мин...", flush=True)
            job.add_log(event="no_new_channels_waiting", seconds=COMMENT_ROUND_DELAY,
                        round=job.current_round)
            await _interruptible_sleep(job, COMMENT_ROUND_DELAY)
            continue

        # Добавляем новые каналы как targets и помечаем увиденными
        target_offset = len(job.targets)
        for ch in new_channels:
            seen_channel_ids.add(ch.channel_id)
            job.targets.append(TargetState(target=f"@{ch.username}"))

        if job.cancel.is_set():
            break

        # ── Комментируем новые каналы ────────────────────────────────────
        # Лимиты сбрасываются каждый раунд (дневные лимиты TG не зависят от нас)
        acc_index = 0
        account_comment_count: dict[str, int] = {}

        for i, ch in enumerate(new_channels):
            if job.cancel.is_set():
                break

            target = job.targets[target_offset + i]

            # Выбираем аккаунт round-robin, пропускаем перегруженные
            acc_name = None
            for _ in range(len(account_cycle)):
                candidate = account_cycle[acc_index % len(account_cycle)]
                acc_index += 1
                if account_comment_count.get(candidate, 0) < COMMENT_LIMIT_PER_ACCOUNT:
                    if pool.get(candidate) is not None:
                        acc_name = candidate
                        break

            if acc_name is None:
                job.add_log(event="all_accounts_exhausted", channel=ch.username,
                            round=job.current_round)
                _mark(job, target, "skipped", "все аккаунты исчерпали лимит", "—")
                continue

            client = pool.get(acc_name)
            job.add_log(event="processing_channel", channel=ch.username, account=acc_name,
                        subscribers=ch.subscribers, round=job.current_round)

            # Вступаем в linked discussion group
            if ch.linked_chat_id:
                try:
                    disc_entity = await client.get_entity(ch.linked_chat_id)
                    await client(JoinChannelRequest(disc_entity))
                    print(f"  Вступили в группу обсуждений @{ch.username}", flush=True)
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

            # Комментируем посты
            commented_this_channel = 0
            for post in posts:
                if job.cancel.is_set():
                    break
                if account_comment_count.get(acc_name, 0) >= COMMENT_LIMIT_PER_ACCOUNT:
                    break

                try:
                    comment_text = await generate_comment(post.text, style_prompt)
                except Exception as e:
                    job.add_log(event="gemini_error", channel=ch.username,
                                post_id=post.id, error=str(e))
                    continue

                sent_ok = await _send_comment(
                    client, job, ch, post, comment_text, acc_name
                )
                if sent_ok:
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
                        round=job.current_round,
                    )
                    await asyncio.sleep(random.uniform(COMMENT_DELAY_MIN, COMMENT_DELAY_MAX))
                elif sent_ok is None:
                    # FloodWait / PeerFlood — прерываем этот канал
                    break

            # Итог по каналу
            if commented_this_channel > 0:
                target.status = "sent"
                target.used_account = acc_name
                target.processed_at = datetime.now(timezone.utc)
            else:
                _mark(job, target, "skipped", "не удалось оставить ни одного комментария", acc_name)

            # Задержка между каналами
            if i < len(new_channels) - 1 and not job.cancel.is_set():
                await asyncio.sleep(random.uniform(5, 12))

        # ── Конец раунда ────────────────────────────────────────────────
        job.add_log(
            event="round_complete",
            round=job.current_round,
            sent=job.sent,
            channels_this_round=len(new_channels),
        )

        if not continuous or job.cancel.is_set():
            break

        # Ждём до следующего раунда (с возможностью прервать)
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
