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
from datetime import datetime, timedelta, timezone

from accounts.manager import get_session_files
from data import analytics as an
from data import users_manager as um
from optimizer import bandit, health
from .client_pool import pool
import config as _cfg
from config import DM_DELAY_MAX, DM_DELAY_MIN, DM_LIMIT_PER_ACCOUNT
from . import text_variants

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


def _say(text: str) -> None:
    """Строка консольного отчёта со временем (время — на машине, где идёт рассылка)."""
    print(f"[{datetime.now():%H:%M:%S}] {text}", flush=True)


_REASONS = {"suppressed (стоп-лист)": "в стоп-листе", "blocked": "человек заблокировал аккаунт",
            "privacy_restricted": "у человека закрыты личные сообщения",
            "busy in another process": "занят другим процессом (прогрев/другая рассылка)",
            "not in pool (unauthorized?)": "не в пуле (не авторизован)"}


def _nice_reason(error: str | None) -> str:
    e = (error or "").strip()
    if e in _REASONS:
        return _REASONS[e]
    if e.startswith("resolve:"):
        return "не удалось найти пользователя (" + e[8:].strip()[:60] + ")"
    return e[:100] or "причина не указана"


def _accounts_progress(job: Job) -> tuple[int, int]:
    """(сколько аккаунтов уже отработали или отпали, сколько их всего в этой рассылке) — из журнала рассылки."""
    total = 0
    done: set[str] = set()
    for e in job.log:
        ev = e.get("event")
        if ev == "job_started":
            total = e.get("accounts") or total
        elif ev in ("account_finished", "account_skipped", "account_failed") and e.get("account"):
            done.add(e["account"])
    return len(done), total


def _job_totals(job: Job, queue=None) -> str:
    left = queue.qsize() if queue is not None else job.total - (job.sent + job.failed + job.skipped)
    return (f"отправлено {job.sent} · пропущено {job.skipped} · ошибок {job.failed} "
            f"· осталось в очереди {left} из {job.total}")


def _mark(job: Job, t: TargetState, status: str, error: str | None, account: str,
          *, peer_id: str | None = None, template_id: int | None = None, progress: str | None = None) -> None:
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

    # Живая строка в консоль на КАЖДЫЙ исход: кому, с какого аккаунта, чем кончилось и общий счёт рассылки
    if status == "sent":
        _say(f"✓ [{account}] отправлено → {t.target}"
             + (f"  · с аккаунта {progress}" if progress else "") + f"  · всего {job.sent} из {job.total}")
    elif status == "failed":
        _say(f"✗ [{account}] ошибка → {t.target}: {_nice_reason(error)}")
    else:
        _say(f"· [{account}] пропущен {t.target}: {_nice_reason(error)}")


def _pick_message(job: Job) -> tuple[str, int | None]:
    """Текст для очередной отправки: бандит по нише либо статичный job.message."""
    if not (job.optimize and job.niche):
        return job.message, None
    tpl = bandit.pick_template(job.niche)
    if tpl is None:
        return job.message, None
    return tpl["text"], tpl["id"]


# ── время, окно активности, «человеческая» отправка ─────────────────────────

_sleep = asyncio.sleep                      # подменяется в тестах
_now = lambda: datetime.now(timezone.utc)   # noqa: E731


def _account_tz(account: str) -> float:
    """Часовой пояс аккаунта: из прогрева (если он на нём был), иначе по умолчанию."""
    try:
        from warmup import store
        a = store.get_account(account)
        if a and a.get("tz_offset_h") is not None:
            return float(a["tz_offset_h"])
    except Exception:  # noqa: BLE001
        pass
    return float(_cfg.DM_DEFAULT_TZ_H)


def seconds_until_window(now_utc: datetime, tz_h: float, window=None) -> float:
    """0, если окно открыто; иначе сколько секунд до его открытия (по локальному времени аккаунта)."""
    lo, hi = window or _cfg.DM_WINDOW_H
    local = now_utc + timedelta(hours=tz_h)
    start = local.replace(hour=lo, minute=0, second=0, microsecond=0)
    if local < start:
        return (start - local).total_seconds()
    if local.hour >= hi:
        return (start + timedelta(days=1) - local).total_seconds()
    return 0.0


async def _wait_for_window(job: Job, account: str) -> bool:
    """Ждёт открытия окна активности. False — если рассылку остановили."""
    if not _cfg.DM_WINDOW_ENABLED:
        return True
    tz = _account_tz(account)
    secs = seconds_until_window(_now(), tz)
    if secs <= 0:
        return True
    local = _now() + timedelta(hours=tz)
    lo, hi = _cfg.DM_WINDOW_H
    print(f"  ⏳ [{account}] сейчас {local:%H:%M} по его времени — вне окна {lo}:00–{hi}:00. "
          f"Жду открытия (~{int(secs // 60)} мин + случайная задержка).")
    job.add_log(event="account_waiting_window", account=account, wait_min=int(secs // 60))
    # к моменту открытия добавляем случайный сдвиг, чтобы аккаунты не стартовали секунда в секунду
    deadline = secs + random.uniform(60, 900)
    waited = 0.0
    while waited < deadline:
        if job.cancel.is_set():
            return False
        step = min(300.0, deadline - waited)
        await _sleep(step)
        waited += step
    return not job.cancel.is_set()


def _typing_seconds(text: str) -> float:
    cps = random.uniform(*_cfg.DM_TYPING_CPS)
    return max(_cfg.DM_TYPING_MIN_S, min(_cfg.DM_TYPING_MAX_S, len(text) / cps))


async def _human_send(client, entity, text: str):
    """Пауза -> «печатает…» -> отправка. Сбой самой имитации не мешает отправить сообщение."""
    if _cfg.DM_TYPING:
        await _sleep(random.uniform(*_cfg.DM_PRE_TYPING_S))
        try:
            async with client.action(entity, "typing"):
                await _sleep(_typing_seconds(text))
        except (FloodWaitError, PeerFloodError, UserDeactivatedBanError, AuthKeyUnregisteredError):
            raise
        except Exception:  # noqa: BLE001 — «печатает» это украшение, а не причина не отправлять
            pass
    return await client.send_message(entity, text)


async def _confirm_delivered(client, entity, msg, waits=None) -> str | None:
    """Проверка, что отправленное сообщение реально лежит в переписке (а не «ушло» и тут же пропало).

    Telegram может принять сообщение без ошибки и потом молча удалить его (антиспам у новых/ограниченных
    аккаунтов) — send_message при этом уже вернул успех. Перечитываем сообщение по id через паузы.
    None — на месте (или проверить не удалось: сбой проверки отправкой не считается); строка — причина.
    """
    mid = getattr(msg, "id", None)
    if not mid:
        return None
    for w in (waits if waits is not None else _cfg.DM_VERIFY_WAITS_S):
        await _sleep(w)
        try:
            got = await client.get_messages(entity, ids=mid)
        except Exception:  # noqa: BLE001 — не смогли проверить: не выдаём это за недоставку
            return None
        if not got:
            return "не доставлено: сообщение пропало из переписки (Telegram его удалил)"
    return None


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
    # Суточный лимит (скользящие 24 ч) действует в ОБОИХ режимах: раньше без ниши лимит был
    # «20 за запуск», и повторный запуск отправлял ещё 20 с того же аккаунта.
    daily_left = max(0, _cfg.DM_DAILY_LIMIT_PER_ACCOUNT - an.sends_last_hours(account, 24))
    if not job.optimize:
        return min(DM_LIMIT_PER_ACCOUNT, daily_left)
    return min(health.remaining_today(account), daily_left)


async def _account_worker(*, job: Job, session_path: str, queue: "asyncio.Queue[TargetState]") -> None:
    session_name = os.path.splitext(os.path.basename(session_path))[0]

    client = pool.get(session_name)
    if client is None:
        busy = session_name in getattr(pool, "busy", set())
        job.add_log(event="account_failed", account=session_name,
                    error="busy in another process" if busy else "not in pool (unauthorized?)")
        _say(f"⏭ [{session_name}] " + ("занят другим процессом (прогрев/другая рассылка) — пропущен"
                                 if busy else "не в пуле (не авторизован) — пропущен"))
        return

    an.ensure_account(session_name)

    # Проверка "отдыхает/выбыл" — всегда, а не только в режиме оптимизатора.
    # Иначе аккаунт, который только что словил PeerFlood (48ч отдыха, см.
    # optimizer/health.REST_HOURS), тут же берётся заново в следующем запуске.
    if not health.is_available(session_name):
        job.add_log(event="account_skipped", account=session_name,
                    reason="отдыхает или выбыл (optimizer.health)")
        _say(f"⏭ [{session_name}] отдыхает/выбыл — пропущен")
        return

    limit = _account_limit(job, session_name)
    if limit <= 0:
        used, wait = an.daily_window(session_name, _cfg.DM_DAILY_LIMIT_PER_ACCOUNT)
        when = f"снова сможет писать через {an.fmt_wait(wait)}" if wait > 0 else "лимит на сегодня по настройкам оптимизатора"
        job.add_log(event="account_skipped", account=session_name,
                    reason=f"дневной лимит исчерпан ({used}/{_cfg.DM_DAILY_LIMIT_PER_ACCOUNT} за 24 ч), {when}")
        _say(f"⏭ [{session_name}] дневной лимит исчерпан ({used}/{_cfg.DM_DAILY_LIMIT_PER_ACCOUNT} за 24 ч) "
             f"— пропущен, {when}")
        return

    job.add_log(event="account_started", account=session_name, limit_today=limit)
    _say(f"▶ [{session_name}] начинает работу (может отправить сегодня: {limit})")

    # Локальная статистика ИМЕННО этого аккаунта (job.sent/skipped/failed —
    # общие на всю рассылку, для одного аккаунта не годятся). stop_reason —
    # для итоговой строки, что вывела его из работы.
    stats = {"sent": 0, "skipped": 0, "failed": 0}
    stop_reason = "не осталось целей в очереди"

    while not job.cancel.is_set() and stats["sent"] < limit:
        # Окно активности: берём цель из очереди только когда можно отправлять,
        # иначе другой аккаунт, которому сейчас можно, остался бы без неё.
        if not await _wait_for_window(job, session_name):
            break
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
            await _sleep(random.uniform(DM_DELAY_MIN, DM_DELAY_MAX))
            continue

        peer_id = str(getattr(entity, "id", "") or "") or None
        if peer_id and an.is_suppressed(peer_id):
            # Стоп-лист — локальная проверка, к Telegram не ходили, пауза не нужна.
            _mark(job, target, "skipped", "suppressed (стоп-лист)", session_name, peer_id=peer_id)
            stats["skipped"] += 1
            continue

        # --- отправка ---
        if _cfg.DM_VARY_TEXT:
            message = text_variants.vary(message, session_name)
        try:
            await _human_send(client, entity, message)
            _mark(job, target, "sent", None, session_name,
                  peer_id=peer_id, template_id=template_id, progress=f"{stats['sent'] + 1}/{limit}")
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

        await _sleep(random.uniform(DM_DELAY_MIN, DM_DELAY_MAX))
    else:
        # while закончился САМ (не через break) — либо упёрлись в лимит, либо отменили
        if job.cancel.is_set():
            stop_reason = "остановлено пользователем (Ctrl+C)"
        elif stats["sent"] >= limit:
            stop_reason = f"дневной лимит аккаунта исчерпан ({limit})"
            _used, _wait = an.daily_window(session_name, _cfg.DM_DAILY_LIMIT_PER_ACCOUNT)
            if _wait > 0:
                stop_reason += f", снова сможет писать через {an.fmt_wait(_wait)}"

    job.add_log(event="account_finished", account=session_name,
                sent_in_session=stats["sent"], skipped_in_session=stats["skipped"],
                failed_in_session=stats["failed"], limit_today=limit,
                stop_reason=stop_reason)

    _used, _ = an.daily_window(session_name, _cfg.DM_DAILY_LIMIT_PER_ACCOUNT)
    _done, _all = _accounts_progress(job)
    print(chr(10).join([
        "",
        f"┌─ [{datetime.now():%H:%M:%S}] ИТОГ ПО АККАУНТУ {session_name} " + "─" * 12,
        f"│ отправлено {stats['sent']} · пропущено {stats['skipped']} · ошибок {stats['failed']}",
        f"│ причина остановки: {stop_reason}",
        f"│ за последние 24 ч с этого аккаунта: {_used}/{_cfg.DM_DAILY_LIMIT_PER_ACCOUNT}",
        f"│ ВСЕГО ПО РАССЫЛКЕ СЕЙЧАС: {_job_totals(job, queue)}",
        f"│ аккаунтов обработано: {_done} из {_all}",
        "└" + "─" * 52,
        "",
    ]), flush=True)


async def run_dm_job(job: Job, only_accounts: set[str] | None = None) -> None:
    """
    only_accounts — если задан, рассылка идёт ТОЛЬКО с этих аккаунтов
    (режим тестирования конкретных аккаунтов, меню «Рассылка → Тестирование»).
    """
    sessions = get_session_files()
    if only_accounts is not None:
        sessions = [s for s in sessions
                    if os.path.splitext(os.path.basename(s))[0] in only_accounts]
    if not sessions:
        job.status = "failed"
        job.error = ("Нет аккаунтов в sessions/" if only_accounts is None
                     else "Ни один из выбранных аккаунтов не найден в sessions/")
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


async def run_single_target_job(job: Job, accounts: list[str], target: str) -> None:
    """
    Проверка доставки: ОДИН и тот же получатель, по очереди с каждого аккаунта из списка —
    сперва отправляет первый аккаунт, потом (после обычной паузы) второй, и так по списку.
    Это не рассылка (очередь РАЗНЫХ получателей на много аккаунтов) — здесь ровно один
    получатель, нужен только затем, чтобы посмотреть, какие из аккаунтов реально доходят и
    с какой ошибкой падают остальные. Предполагается тестовый контакт (свой второй номер,
    коллега, тестовый бот) — тот же стоп-лист и дневной лимit действуют как в обычной рассылке,
    так что на настоящего человека, который попросил не писать, сообщение всё равно не уйдёт.
    """
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    job.targets = [TargetState(target=target) for _ in accounts]
    job.add_log(event="job_started", total=job.total, accounts=len(accounts),
                parallel=1, kind="dm-single-target")
    key = target.lower().lstrip("@")
    delivered: list = []   # (индекс, аккаунт, клиент, получатель, сообщение) — на перепроверку в конце

    for i, name in enumerate(accounts):
        if job.cancel.is_set():
            break
        t = job.targets[i]
        client = pool.get(name)
        if client is None:
            busy = name in getattr(pool, "busy", set())
            reason = "busy in another process" if busy else "not in pool (unauthorized?)"
            _mark(job, t, "skipped", reason, name)
            job.add_log(event="account_finished", account=name,
                        stop_reason="занят другим процессом" if busy else "не в пуле (не авторизован)")
            continue

        an.ensure_account(name)
        if not health.is_available(name):
            _mark(job, t, "skipped", "отдыхает или выбыл", name)
            job.add_log(event="account_finished", account=name, stop_reason="отдыхает/выбыл")
            continue

        limit = _account_limit(job, name)
        if limit <= 0:
            used, wait = an.daily_window(name, _cfg.DM_DAILY_LIMIT_PER_ACCOUNT)
            reason = (f"дневной лимит исчерпан ({used}/{_cfg.DM_DAILY_LIMIT_PER_ACCOUNT} за 24 ч)"
                      + (f", снова сможет через {an.fmt_wait(wait)}" if wait > 0 else ""))
            _mark(job, t, "skipped", reason, name)
            job.add_log(event="account_finished", account=name, stop_reason=reason)
            continue

        if an.is_suppressed(key):
            _mark(job, t, "skipped", "suppressed (стоп-лист)", name)
            job.add_log(event="account_finished", account=name, stop_reason="в стоп-листе")
            continue

        try:
            entity = await client.get_entity(_resolve(target))
        except Exception as e:  # noqa: BLE001
            _mark(job, t, "skipped", f"resolve: {e}", name)
            job.add_log(event="account_finished", account=name, stop_reason=f"не нашёл получателя: {str(e)[:80]}")
            if i < len(accounts) - 1:
                await _sleep(random.uniform(DM_DELAY_MIN, DM_DELAY_MAX))
            continue

        peer_id = str(getattr(entity, "id", "") or "") or None
        if peer_id and an.is_suppressed(peer_id):
            _mark(job, t, "skipped", "suppressed (стоп-лист)", name, peer_id=peer_id)
            job.add_log(event="account_finished", account=name, stop_reason="в стоп-листе")
            continue

        message = text_variants.vary(job.message, name) if _cfg.DM_VARY_TEXT else job.message
        # метка в конце текста: по ней получатель видит, с какого по счёту аккаунта пришло сообщение
        message = f"{message}\n\n[тест {i + 1}/{len(accounts)} · акк …{name[-4:]}]"
        stop_reason = "отправлено"
        try:
            sent_msg = await _human_send(client, entity, message)
            bad = await _confirm_delivered(client, entity, sent_msg)
            if bad:
                _mark(job, t, "failed", bad, name, peer_id=peer_id)
                if t.send_id:
                    an.mark_send_blocked(t.send_id, bad)
                stop_reason = bad
            else:
                _mark(job, t, "sent", None, name, peer_id=peer_id)
                delivered.append((i, name, client, entity, sent_msg))
        except (UserIsBlockedError, UserPrivacyRestrictedError) as e:
            reason = "blocked" if isinstance(e, UserIsBlockedError) else "privacy_restricted"
            _mark(job, t, "skipped", reason, name, peer_id=peer_id)
            stop_reason = "заблокировал" if reason == "blocked" else "закрыты личные сообщения"
        except FloodWaitError as e:
            health.on_error(name, "flood_wait", f"send {e.seconds}s")
            _mark(job, t, "failed", f"FloodWait {e.seconds} сек", name, peer_id=peer_id)
            stop_reason = f"FloodWait {e.seconds} сек"
        except PeerFloodError:
            health.on_error(name, "peer_flood", "send")
            _mark(job, t, "failed", "PeerFlood", name, peer_id=peer_id)
            stop_reason = "PeerFlood"
        except (UserDeactivatedBanError, AuthKeyUnregisteredError) as e:
            health.on_error(name, "banned", str(e))
            _mark(job, t, "failed", f"мёртв/забанен ({e})", name, peer_id=peer_id)
            stop_reason = "мёртв/забанен"
        except Exception as e:  # noqa: BLE001
            _mark(job, t, "failed", str(e), name, peer_id=peer_id)
            stop_reason = str(e)[:80]

        job.add_log(event="account_finished", account=name, stop_reason=stop_reason)
        if i < len(accounts) - 1:
            await _sleep(random.uniform(DM_DELAY_MIN, DM_DELAY_MAX))

    # Итоговая перепроверка: Telegram мог убрать сообщения уже после первой проверки
    if delivered and not job.cancel.is_set():
        _say(f"Перепроверяю доставку через {_cfg.DM_VERIFY_FINAL_S} сек...")
        await _sleep(_cfg.DM_VERIFY_FINAL_S)
        for i, name, client, entity, msg in delivered:
            bad = await _confirm_delivered(client, entity, msg, waits=(0,))
            if bad:
                t = job.targets[i]
                t.status, t.error = "failed", bad
                job.sent -= 1
                job.failed += 1
                if t.send_id:
                    an.mark_send_blocked(t.send_id, bad)
                _say(f"✗ [{name}] {bad}")
        ok = [f"…{n[-4:]} (тест {i + 1}/{len(accounts)})"
              for i, n, _c, _e, _m in delivered if job.targets[i].status == "sent"]
        _say(f"ИТОГ: у отправителей на месте {len(ok)} из {len(delivered)} принятых сообщений. "
             f"Получатель {target} должен увидеть ровно {len(ok)}: " + (", ".join(ok) or "—"))
        _say("Если у получателя меньше — смотрите в нём «Архив», папку со спамом и настройки приватности "
             "(Конфиденциальность → Сообщения).")

    job.status = "stopped" if job.cancel.is_set() else "done"
    job.finished_at = datetime.now(timezone.utc)
    job.add_log(event="job_finished", status=job.status, backlog=0,
                sent=job.sent, failed=job.failed, skipped=job.skipped)
