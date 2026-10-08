# -*- coding: utf-8 -*-
"""
Исполнитель прогрева.

Каждый «тик»:
  1. plan_missing()  — для каждого аккаунта на сегодняшний день создаёт задачи
                       (если их ещё нет): профиль, подписки/чтение, диалоги;
  2. run_due()       — берёт созревшие задачи из БД и выполняет их через Telethon,
                       аккаунты параллельно, задачи одного аккаунта — по порядку.

Все зависимости (время, sleep, нейросеть, генератор случайных чисел, клиенты)
передаются снаружи — поэтому в тестах весь многодневный прогрев гоняется
за секунды на «часах», которые мы двигаем руками.
"""
import asyncio
import os
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from telethon.errors import (
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    FloodWaitError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UsernameInvalidError,
    UsernameNotModifiedError,
    UsernameOccupiedError,
)

from accounts import photos
from . import config as cfg
from . import planner, store
from .content import Content, LLMUnavailable

DEAD_ERRORS = (AuthKeyUnregisteredError, UserDeactivatedBanError, UserDeactivatedError)


class SkipTask(Exception):
    """Задачу выполнить нельзя и не нужно (с понятной причиной) — не ошибка."""


class NotReady(Exception):
    """Нужно подождать (предыдущая реплика партнёра ещё не ушла)."""


def read_channels(path: str = cfg.CHANNELS_FILE) -> list[str]:
    """Каналы для подписки: по одному на строку (@name или t.me/name), # — комментарий."""
    if not os.path.exists(path):
        return []
    out: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.split("#", 1)[0].strip()
            if not s:
                continue
            s = s.replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "")
            s = "@" + s.lstrip("@").strip("/")
            if len(s) > 1 and s not in out:
                out.append(s)
    return out


class WarmupRunner:
    def __init__(self, clients: dict, *, rng: random.Random | None = None,
                 content=None, now=None, sleep=None, channels=None, on_event=None):
        self.clients = clients                       # {account: TelegramClient}
        self.rng = rng or random.Random()
        self.content = content or Content(self.rng)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or asyncio.sleep
        self._channels = channels                    # None -> читать из файла каждый раз
        self.on_event = on_event or (lambda msg: None)
        self.identity: dict[str, dict] = {}          # {account: {phone, user_id, first, last}}

    # ── вспомогательное ─────────────────────────────────────────────────────
    def channels(self) -> list[str]:
        return list(self._channels) if self._channels is not None else read_channels()

    def _emit(self, account: str | None, text: str, ok: bool = True) -> None:
        store.log(account, text.split(":", 1)[0][:40], ok, text, self.now())
        self.on_event(f"  {'✓' if ok else '✗'} [{account or '—'}] {text}")

    async def refresh_identity(self) -> None:
        """Узнать у Telegram номер/id/имя каждого аккаунта — нужно для контактов и диалогов."""
        for acc, client in self.clients.items():
            try:
                me = await client.get_me()
            except DEAD_ERRORS as e:
                self._mark_dead(acc, e)
                continue
            self.identity[acc] = {
                "phone": getattr(me, "phone", None), "user_id": getattr(me, "id", None),
                "first": getattr(me, "first_name", "") or "", "last": getattr(me, "last_name", "") or "",
            }
            store.set_account(acc, phone=self.identity[acc]["phone"], user_id=self.identity[acc]["user_id"])

    def _mark_dead(self, acc: str, err: Exception) -> None:
        store.set_account(acc, state="dead", note=f"{type(err).__name__}")
        n = store.skip_pending(acc, f"аккаунт мёртв: {type(err).__name__}", self.now())
        try:  # тот же учёт, что у рассылки — аккаунт не должен попасть в неё
            import optimizer.health as health
            health.on_error(acc, "banned", str(err))
        except Exception:  # noqa: BLE001
            pass
        self._emit(acc, f"аккаунт мёртв ({type(err).__name__}), снято задач: {n}", ok=False)

    # ── планирование ────────────────────────────────────────────────────────
    def plan_missing(self) -> None:
        now = self.now()
        in_dialog: list[tuple[dict, int, datetime, int]] = []
        for a in store.list_accounts(("active",)):
            acc = a["account"]
            if acc not in self.clients:
                continue
            started = store.parse_ts(a["started_at"])
            tz = a["tz_offset_h"]
            day = planner.day_index(started, now, tz)
            first = planner.first_day(started, tz)
            phase = planner.phase_for_day(day, bool(a["dialogs"]), first)

            if phase == planner.LIFE:
                # «жизнь» (подписки/чтение) и диалоги идут в одни и те же дни.
                # В день запуска начинаем после профиля, а не поверх него.
                eff = now
                if day == 0:
                    md = store.max_due(acc, 0)
                    if md:
                        eff = max(now, store.parse_ts(md) + timedelta(minutes=10))
                if not store.has_tasks(acc, day, ("subscribe", "read", "saved")):
                    store.add_tasks(planner.plan_life(acc, day, started, tz, eff, self.rng))
                if planner.dialog_active(day, bool(a["dialogs"]), first):
                    in_dialog.append((a, day, eff, first))
            elif phase == planner.DONE and store.pending_total(acc) == 0:
                store.set_account(acc, state="done")
                self._emit(acc, "прогрев завершён")
        self._plan_dialogs(in_dialog)

    def _plan_dialogs(self, in_dialog: list[tuple[dict, int, datetime, int]]) -> None:
        if not in_dialog:
            return
        info = {a["account"]: (a, day, eff, first) for a, day, eff, first in in_dialog}

        # 1) пары: те, кто ещё ни с кем не в паре
        paired = store.paired_accounts()
        free = [acc for acc in info if acc not in paired]
        if len(free) >= 2:
            store.set_pairs(planner.make_pairs(free, self.rng))
        elif len(free) == 1:  # нечётный остаток — к случайному уже занятому партнёру
            mates = [x for x in info if x in paired and x != free[0]]
            if mates:
                store.set_pairs([(free[0], self.rng.choice(mates))])

        # 2) задачи дня по каждой паре, где оба сейчас в фазе диалогов
        all_pairs = store.list_pairs()
        partners: dict[str, int] = defaultdict(int)
        for pa, pb in all_pairs:
            partners[pa] += 1
            partners[pb] += 1
        for a, b in all_pairs:
            if a not in info or b not in info:
                continue
            (ra, da, ea, fa), (rb, db, eb, fb) = info[a], info[b]
            day = max(da, db)
            now = max(ea, eb)
            dialog_day = max(da - fa, db - fb) + 1
            if (store.has_tasks(a, day, ("dialog_turn", "contact_add"))
                    and store.has_tasks(b, day, ("dialog_turn", "contact_add"))):
                continue
            key = store.pair_key(a, b)
            first_day = not store.has_group(f"contact|{key}")
            started = store.parse_ts(ra["started_at"])
            n_partners = max(partners[a], partners[b])
            store.add_tasks(planner.plan_dialog_pair(
                a, b, day, started, ra["tz_offset_h"], now, self.rng, first_day,
                replies_range=planner.scaled_replies(n_partners, dialog_day),
                started_b=store.parse_ts(rb["started_at"]), tz_b=rb["tz_offset_h"]))

    # ── выполнение ──────────────────────────────────────────────────────────
    async def tick(self) -> int:
        """Один проход: спланировать недостающее и выполнить созревшее. Возвращает число задач."""
        self.plan_missing()
        due = self._defer_stale(store.claim_due(self.now(), set(self.clients)))
        by_acc: dict[str, list[dict]] = defaultdict(list)
        for t in due:
            by_acc[t["account"]].append(t)
        if by_acc:
            await asyncio.gather(*[self._run_account(acc, ts) for acc, ts in by_acc.items()])
        return len(due)

    def _defer_stale(self, due: list[dict]) -> list[dict]:
        """
        Задачи, просроченные сильно (ноутбук спал, процесс был закрыт), нельзя
        выполнять пачкой «на проснулся»: 20 действий подряд за минуту — это не
        человек. Их растягиваем по времени (зазор 3–15 мин на аккаунт) и только
        в дневное окно; реплики диалога, чей момент прошёл, пропускаем (разговор
        не продолжают спустя полдня, а на следующий день план составится заново).
        Возвращает только «свежие» задачи, которые можно делать прямо сейчас.
        """
        now = self.now()
        fresh, stale = [], defaultdict(list)
        for t in due:
            late = (now - store.parse_ts(t["due_at"])).total_seconds()
            (stale[t["account"]] if late > cfg.STALE_AFTER_S else fresh).append(t)
        for acc, tasks in stale.items():
            tz = (store.get_account(acc) or {}).get("tz_offset_h", cfg.DEFAULT_TZ_OFFSET_H)
            cursor = planner.clamp_to_awake(now + timedelta(seconds=self.rng.uniform(120, 600)), tz, self.rng)
            moved = skipped = 0
            for t in sorted(tasks, key=lambda x: (x["due_at"], x["id"])):
                if t["kind"] == "dialog_turn":
                    store.finish_task(t["id"], "skipped", now, "момент реплики прошёл (пауза процесса)")
                    skipped += 1
                    continue
                store.reschedule_task(t["id"], cursor)
                cursor = planner.clamp_to_awake(
                    cursor + timedelta(seconds=self.rng.uniform(*cfg.STALE_SPACING_S)), tz, self.rng)
                moved += 1
            self._emit(acc, f"пауза процесса: {moved} просроченных задач растянуто по времени"
                            f"{f', {skipped} реплик пропущено' if skipped else ''}")
        return fresh

    async def _run_account(self, acc: str, tasks: list[dict]) -> None:
        net_down = False
        cursor = self.now()
        for t in sorted(tasks, key=lambda x: (x["due_at"], x["id"])):
            if net_down:
                # Сети нет — остальные задачи пачки даже не пытаемся: разносим их
                # по времени, иначе, когда сеть вернётся, они выполнятся разом.
                cursor += timedelta(seconds=self.rng.uniform(*cfg.STALE_SPACING_S))
                store.reschedule_task(t["id"], cursor, error="сеть: ждём восстановления")
                continue
            net_down = bool(await self._execute(t))

    async def _execute(self, task: dict) -> None:
        acc, kind, tid = task["account"], task["kind"], task["id"]
        handler = getattr(self, f"_do_{kind}", None)
        if handler is None:
            store.finish_task(tid, "failed", self.now(), f"неизвестный тип задачи {kind}")
            return
        try:
            detail = await handler(acc, self.clients[acc], task)
            store.finish_task(tid, "done", self.now())
            self._emit(acc, f"{kind}: {detail}")
        except SkipTask as e:
            store.finish_task(tid, "skipped", self.now(), str(e))
            self._emit(acc, f"{kind}: пропущено — {e}", ok=True)
        except NotReady:
            payload = dict(task["payload"])
            since = store.parse_ts(payload.setdefault("_wait_since", store.ts(self.now())))
            if (self.now() - since).total_seconds() > cfg.DEPENDENCY_GIVE_UP_S:
                store.finish_task(tid, "skipped", self.now(), "не дождались предыдущей реплики")
                self._emit(acc, f"{kind}: пропущено — партнёр не ответил вовремя")
            else:
                store.reschedule_task(tid, self.now() + timedelta(seconds=cfg.DEPENDENCY_RETRY_S),
                                      payload=payload)
        except FloodWaitError as e:
            delay = e.seconds + self.rng.uniform(5, 60)
            store.reschedule_task(tid, self.now() + timedelta(seconds=delay), error=f"FloodWait {e.seconds}s")
            self._emit(acc, f"{kind}: FloodWait {e.seconds}с — перенесено", ok=False)
        except DEAD_ERRORS as e:
            store.finish_task(tid, "failed", self.now(), type(e).__name__)
            self._mark_dead(acc, e)
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            # Обрыв сети (после сна ноутбука сеть поднимается не сразу) — это не
            # вина задачи: ждём и пробуем снова, попытки не тратим.
            store.reschedule_task(tid, self.now() + timedelta(seconds=cfg.NET_RETRY_S),
                                  error=f"сеть: {type(e).__name__}")
            self._emit(acc, f"{kind}: нет связи ({type(e).__name__}) — повтор через {cfg.NET_RETRY_S}с", ok=False)
            return True
        except Exception as e:  # noqa: BLE001 — любая ошибка одной задачи не должна валить прогрев
            err = f"{type(e).__name__}: {e}"
            if task["attempts"] + 1 >= cfg.MAX_ATTEMPTS:
                store.finish_task(tid, "failed", self.now(), err)
                self._emit(acc, f"{kind}: ошибка, попытки исчерпаны — {err}", ok=False)
            else:
                store.reschedule_task(tid, self.now() + timedelta(seconds=cfg.RETRY_DELAY_S),
                                      attempts_inc=1, error=err)
                self._emit(acc, f"{kind}: ошибка, повтор позже — {err}", ok=False)

    def all_finished(self) -> bool:
        return not any(a["account"] in self.clients
                       for a in store.list_accounts(("active",)))

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        n = store.reset_running()
        if n:
            self.on_event(f"  ↺ вернул в очередь {n} недоделанных задач после прошлого запуска")
        await self.refresh_identity()
        while not stop.is_set():
            await self.tick()
            if self.all_finished():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.TICK_S)
            except asyncio.TimeoutError:
                pass

    # ── обработчики задач ───────────────────────────────────────────────────
    async def _do_avatar(self, acc, client, task):
        from telethon.tl.functions.photos import UploadProfilePhotoRequest
        images = photos.list_images(cfg.AVATARS_DIR)
        if not images:
            raise SkipTask(f"папка {cfg.AVATARS_DIR}/ пуста")
        path = photos.variant_image(self.rng.choice(images), self.rng)
        try:
            uploaded = await client.upload_file(path)
            await client(UploadProfilePhotoRequest(file=uploaded))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return "аватар обновлён"

    async def _do_name(self, acc, client, task):
        from telethon.tl.functions.account import UpdateProfileRequest
        n = await self.content.names()
        await client(UpdateProfileRequest(first_name=n["first_name"], last_name=n.get("last_name", "")))
        self.identity.setdefault(acc, {}).update(first=n["first_name"], last=n.get("last_name", ""))
        full = f"{n['first_name']} {n.get('last_name', '')}".strip()
        return f"имя «{full}»"

    async def _me_names(self, acc, client) -> tuple[str, str]:
        ident = self.identity.get(acc) or {}
        if ident.get("first"):
            return ident["first"], ident.get("last", "")
        me = await client.get_me()
        return (getattr(me, "first_name", "") or ""), (getattr(me, "last_name", "") or "")

    async def _do_bio(self, acc, client, task):
        from telethon.tl.functions.account import UpdateProfileRequest
        first, _ = await self._me_names(acc, client)
        bio = await self.content.bio(first, cfg.BIO_MAX)
        await client(UpdateProfileRequest(about=bio))
        return f"био «{bio}»"

    async def _do_username(self, acc, client, task):
        from telethon.tl.functions.account import CheckUsernameRequest, UpdateUsernameRequest
        first, last = await self._me_names(acc, client)
        for cand in (await self.content.usernames(first, last))[:cfg.USERNAME_TRIES]:
            try:
                if await client(CheckUsernameRequest(cand)):
                    await client(UpdateUsernameRequest(cand))
                    return f"юзернейм @{cand}"
            except UsernameNotModifiedError:
                return f"юзернейм @{cand} уже стоит"
            except (UsernameOccupiedError, UsernameInvalidError):
                pass
            await self.sleep(self.rng.uniform(2, 6))   # занят/невалиден — не частим проверками
        raise RuntimeError("не нашёл свободный юзернейм среди кандидатов")

    async def _do_subscribe(self, acc, client, task):
        from telethon.tl.functions.channels import JoinChannelRequest
        have = {s.lower() for s in store.get_subs(acc)}
        pool = [c for c in self.channels() if c.lower() not in have]
        if not pool:
            raise SkipTask(f"в {cfg.CHANNELS_FILE} нет каналов, на которые аккаунт ещё не подписан")
        self.rng.shuffle(pool)
        last_err = None
        for ch in pool[:3]:
            try:
                entity = await client.get_entity(ch)
                await client(JoinChannelRequest(entity))
                store.add_sub(acc, ch, self.now())
                return f"подписан на {ch}"
            except (ChannelPrivateError, ValueError) as e:
                last_err = e
                continue
        raise RuntimeError(f"не удалось подписаться ни на один из кандидатов ({last_err})")

    async def _channel_posts(self, acc, client, limit: int):
        subs = store.get_subs(acc)
        if not subs:
            raise SkipTask("нет подписок")
        ch = self.rng.choice(subs)
        entity = await client.get_entity(ch)
        msgs = [m for m in await client.get_messages(entity, limit=limit) if getattr(m, "id", None)]
        if not msgs:
            raise SkipTask(f"в {ch} нет постов")
        return ch, entity, msgs

    async def _do_read(self, acc, client, task):
        from telethon.tl.functions.messages import GetMessagesViewsRequest
        ch, entity, msgs = await self._channel_posts(acc, client, 15)
        k = min(len(msgs), self.rng.randint(*cfg.READ_POSTS))
        chosen = sorted(self.rng.sample(msgs, k), key=lambda m: m.id, reverse=True)  # как скролл вниз
        for i, m in enumerate(chosen):
            await client(GetMessagesViewsRequest(peer=entity, id=[m.id], increment=True))
            if i < k - 1:
                await self.sleep(self.rng.uniform(*cfg.READ_DELAY_S))
        await client.send_read_acknowledge(entity, max_id=max(m.id for m in chosen))
        return f"прочитано {k} постов в {ch}"

    async def _do_saved(self, acc, client, task):
        ch, entity, msgs = await self._channel_posts(acc, client, 20)
        await client.forward_messages("me", self.rng.choice(msgs))
        return f"пост из {ch} → «Избранное»"

    async def _do_contact_add(self, acc, client, task):
        from telethon.tl.functions.contacts import ImportContactsRequest
        from telethon.tl.types import InputPhoneContact
        partner = task["payload"]["partner"]
        pid = self.identity.get(partner)
        if partner not in self.clients or not pid or not pid.get("phone"):
            raise SkipTask(f"партнёр {partner} сейчас не в прогреве / номер неизвестен")
        phone = "+" + str(pid["phone"]).lstrip("+")
        res = await client(ImportContactsRequest([InputPhoneContact(
            client_id=self.rng.randint(1, 2 ** 31 - 1), phone=phone,
            first_name=pid.get("first") or "Друг", last_name=pid.get("last") or "")]))
        if not getattr(res, "users", None):
            raise RuntimeError("Telegram не нашёл партнёра по номеру (скрыт в настройках приватности?)")
        return f"{partner} добавлен в контакты"

    async def _do_dialog_turn(self, acc, client, task):
        p = task["payload"]
        partner, key = p["partner"], p["pair"]
        if partner not in self.clients:
            raise SkipTask(f"партнёр {partner} не запущен")
        if not store.group_ready(task["group_key"], task["seq"]):
            raise NotReady()

        pid = self.identity.get(partner) or {}
        try:
            entity = await client.get_entity(pid.get("user_id"))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"не нашёл собеседника (контакт не добавлен?): {e}")

        history = store.history(key, cfg.DIALOG_HISTORY)
        me_first, _ = await self._me_names(acc, client)
        try:
            text = await self.content.dialog_message(
                me=me_first or "Я", other=pid.get("first") or "приятель", topic=p.get("topic", ""),
                history=history, me_account=acc)
        except LLMUnavailable as e:
            raise SkipTask(str(e))      # не ошибка: нечем писать — пропускаем, а не повторяем 3 раза

        # «Шахматный порядок»: сначала собеседник прочитал входящее, потом печатает, потом шлёт
        if history and history[-1]["sender"] == partner:
            await client.send_read_acknowledge(entity)
            await self.sleep(self.rng.uniform(1.5, 6.0))
        typing_s = min(12.0, self.rng.uniform(*cfg.TYPING_S) + len(text) * 0.08)
        async with client.action(entity, "typing"):
            await self.sleep(typing_s)
        await client.send_message(entity, text)
        store.add_msg(key, acc, text, self.now())
        return f"→ {partner}: «{text}»"


# ── отчёт для меню ──────────────────────────────────────────────────────────

def describe_accounts(now: datetime | None = None) -> list[dict]:
    """Строки для таблицы статуса: где аккаунт сейчас, сколько сделано, что дальше."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for a in store.list_accounts():
        started = store.parse_ts(a["started_at"])
        day = planner.day_index(started, now, a["tz_offset_h"])
        first = planner.first_day(started, a["tz_offset_h"])
        phase = planner.phase_for_day(day, bool(a["dialogs"]), first)
        if phase == planner.LIFE and planner.dialog_active(day, bool(a["dialogs"]), first):
            phase = planner.DIALOG        # жизнь + диалоги идут вместе
        counts = store.task_counts(a["account"])
        nd = store.next_due(a["account"])
        rows.append({
            "account": a["account"], "state": a["state"],
            "day": planner.counted_day(day, first),     # номер дня из WARMUP_DAYS
            "phase": phase,
            "total_days": planner.total_days(bool(a["dialogs"])),
            "done": counts.get("done", 0), "pending": counts.get("pending", 0) + counts.get("running", 0),
            "failed": counts.get("failed", 0), "skipped": counts.get("skipped", 0),
            "next": nd, "dialogs": bool(a["dialogs"]), "note": a.get("note"),
            "errors": store.failed_tasks(a["account"], 2),
        })
    return rows


# ── запуск пачки ────────────────────────────────────────────────────────────

def start_warmup(accounts: list[str], *, now: datetime, rng: random.Random,
                 dialogs: bool = True, set_name: bool = True,
                 base_tz_h: float = cfg.DEFAULT_TZ_OFFSET_H) -> dict[str, datetime]:
    """
    Ставит аккаунты на прогрев и создаёт план дня 0 (профиль).

    Старт каждого аккаунта сдвигается на случайные 0–90 минут: если запустить
    пачку из 10 аккаунтов, они не должны одновременно начать править профили.
    Часовой пояс каждого — базовый ±1 ч (чтобы «просыпались» не синхронно).
    Возвращает {аккаунт: момент старта}.
    """
    store.ensure_schema()
    starts: dict[str, datetime] = {}
    for acc in accounts:
        store.remove_account(acc)  # перезапуск прогрева = чистый лист
        started = now + timedelta(seconds=rng.uniform(*cfg.BATCH_START_SPREAD_S))
        tz = round(base_tz_h + rng.uniform(-cfg.TZ_JITTER_H, cfg.TZ_JITTER_H), 2)
        store.register_account(acc, started, tz, dialogs, set_name)
        store.add_tasks(planner.plan_profile(acc, started, rng, set_name))
        starts[acc] = started

    # Пары — СРАЗУ на всю пачку, а не «по мере входа в фазу диалогов»: из-за
    # разных часовых поясов аккаунты входят в неё в разные тики, и ленивая
    # раздача пар давала одному аккаунту 3 собеседника (втрое больше реплик
    # в день, чем по плану). Одиночный аккаунт без пары просто не болтает.
    if dialogs and len(accounts) >= 2:
        store.set_pairs(planner.make_pairs(list(accounts), rng))
    return starts
