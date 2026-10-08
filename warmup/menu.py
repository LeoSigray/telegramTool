# -*- coding: utf-8 -*-
"""
Консольное меню «Прогрев аккаунтов» (главное меню → 7).

Прогрев идёт несколько суток, поэтому цикл надо оставить работать (окно
не закрывать). Закрыли — ничего не потеряно: всё лежит в БД, и пункт
«Продолжить» подхватывает ровно с того места.
"""
import asyncio
import os
from datetime import datetime, timezone

from accounts.manager import get_session_files
from accounts.picker import pick_accounts
from . import config as cfg
from . import devices, store
from .runner import WarmupRunner, describe_accounts, read_channels, start_warmup


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _proxy_of(account: str) -> str | None:
    from config import load_proxy
    from data.db import get_account_proxy
    return get_account_proxy(account) or load_proxy() or None


def _yes(prompt: str, default: bool) -> bool:
    raw = input(f"{prompt} ({'Y/n' if default else 'y/N'}): ").strip().lower()
    return default if not raw else raw in ("y", "yes", "д", "да")


# ── проверка готовности ─────────────────────────────────────────────────────

def preflight(accounts: list[str]) -> list[str]:
    """Что может пойти не так ДО запуска. Возвращает список предупреждений."""
    from warmup.content import Content
    from accounts.photos import list_images

    warns: list[str] = []
    n_ch = len(read_channels())
    if n_ch == 0:
        warns.append(f"в {cfg.CHANNELS_FILE} нет каналов — подписки и чтение работать не будут")
    elif n_ch < 8:
        warns.append(f"в {cfg.CHANNELS_FILE} всего {n_ch} каналов — у аккаунтов получится почти "
                     f"одинаковый набор подписок (нужно хотя бы 8–10)")
    if not Content.llm_ready():
        warns.append("нейросеть не настроена: имя/био/юзернейм возьмутся из запасных списков, "
                     "а диалоги между аккаунтами будут пропущены")
    if not list_images(cfg.AVATARS_DIR):
        warns.append(f"папка {cfg.AVATARS_DIR}/ без фото — аватарки не поставятся")
    return warns


def shared_proxy_warning(accounts: list[str]) -> bool:
    """Все аккаунты выходят в сеть с одного и того же адреса?"""
    return len(accounts) > 1 and len({_proxy_of(a) for a in accounts}) <= 1


# ── запуск цикла ────────────────────────────────────────────────────────────

class KeepAwake:
    """
    Пока идёт прогрев — не даём Windows уйти в сон (экран гаснуть может).
    Закрытая крышка ноутбука может усыпить его независимо от этого — это
    настройка электропитания Windows («действие при закрытии крышки»).
    """
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __enter__(self):
        self.on = False
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
            self.on = True
        except Exception:  # не Windows / нет доступа — просто работаем без этого
            pass
        return self

    def __exit__(self, *a):
        if self.on:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
        return False


_STATE_LABEL = {"paused": "на паузе", "done": "прогрев завершён", "dead": "мёртв"}


async def sync_states(pool, runner, selected, on_event=print) -> None:
    """
    Подгоняет подключения под состояние прогрева в базе (его меняют из другого окна: пауза/возобновление).
      • на паузе / готов / мёртв — аккаунт ОТКЛЮЧАЕТСЯ и освобождается: им можно пользоваться в другом окне;
      • снова «идёт» — аккаунт подключается обратно и продолжает прогрев.
    """
    states = {r["account"]: r["state"] for r in store.list_accounts()}
    for a in list(runner.clients):
        st = states.get(a)
        if st in _STATE_LABEL:
            await pool.release(a)
            runner.clients.pop(a, None)
            on_event(f"  ⏸ [{a}] {_STATE_LABEL[st]} — аккаунт освобождён, им можно пользоваться в другом окне")
    # selected=None — все, кто сейчас «идёт» в базе (в т.ч. поставленные на прогрев из другого окна уже после старта цикла)
    candidates = selected if selected is not None else [x for x, st in states.items() if st == "active"]
    for a in candidates:
        if a not in runner.clients and states.get(a) == "active" and a not in pool.unauthorized:
            pool.busy.discard(a)
            try:   # мессенджер мог подхватить аккаунт, пока тот был на паузе: просим вернуть
                from accounts import leases
                await asyncio.get_event_loop().run_in_executor(None, lambda: leases.take([a], "warmup", wait_s=20))
                leases.release("warmup")
            except Exception:  # noqa: BLE001
                pass
            await pool.start_all(only={a})
            if a in pool.clients:
                runner.clients[a] = pool.clients[a]
                on_event(f"  ▶ [{a}] возобновлён — снова в прогреве")


def run_loop(accounts: list[str]) -> None:
    """Подключает аккаунты и крутит прогрев, пока не закончится или не нажмут Ctrl+C."""
    from proxy_manager import proxy_guard
    if not proxy_guard(accounts, "прогрев"):
        print("Отменено.")
        return
    async def _run():
        from api.client_pool import pool
        print("\nПодключаем аккаунты...")
        # Если мессенджер (бэкенд) уже держит эти аккаунты, просим отпустить: он делает это за пару секунд
        from accounts import leases
        await asyncio.get_event_loop().run_in_executor(None, lambda: leases.take(accounts, "warmup", wait_s=30))
        leases.release("warmup")
        await pool.start_all(only=set(accounts))
        # Аккаунты, которые Telegram НЕ авторизовал (сессия недействительна), помечаем МЁРТВ в прогреве:
        # иначе в статусе у них вечное «идёт», хотя ни одно действие они не выполнят.
        not_auth = sorted(a for a in accounts if a in pool.unauthorized)
        for a in not_auth:
            store.set_account(a, state="dead", note="не авторизован (Telegram не принимает сессию)")
            store.skip_pending(a, "аккаунт не авторизован", _now())
        if pool.busy & set(accounts):
            print(f"Заняты другим процессом (другое окно держит эти аккаунты): "
                  f"{', '.join(sorted(pool.busy & set(accounts)))}")
        if not_auth:
            print(f"Не авторизованы — помечены МЁРТВ в прогреве, задачи сняты: {', '.join(not_auth)}")
        active = pool.list_active()
        if not active:
            print("Ни один аккаунт не подключился — прогрев не запущен "
                  "(если ошибки про proxy — проверь прокси: 1 → 6 → 3).")
            return
        missing = sorted(set(accounts) - set(active) - set(not_auth))
        if missing:
            print(f"Не подключились (их задачи подождут): {', '.join(missing)}")
        print(f"Активных: {len(active)}. Ctrl+C — остановить (прогресс сохраняется).\n")
        runner = WarmupRunner(dict(pool.clients), on_event=print)
        selected = set(accounts)

        store.beat()

        async def _watch():
            while True:
                store.beat()
                await asyncio.sleep(20)
                try:
                    await sync_states(pool, runner, None)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — сбой синхронизации не должен останавливать прогрев
                    print(f"  [синхронизация паузы] {type(e).__name__}: {e}")

        def _paused_selected() -> list[str]:
            return [r["account"] for r in store.list_accounts(("paused",))]

        watcher = asyncio.get_event_loop().create_task(_watch())
        try:
            while True:
                await runner.run_forever()
                paused = _paused_selected()
                if not paused:
                    break
                # активные закончили, но кто-то на паузе: ждём возобновления, а не выходим
                print(f"\nАктивные закончили. На паузе: {', '.join(paused)}. Жду возобновления (Ctrl+C — выйти).")
                while _paused_selected():
                    await asyncio.sleep(30)
                    await sync_states(pool, runner, None)      # сразу подключаем тех, кого возобновили
                    if any(r["account"] in runner.clients for r in store.list_accounts(("active",))):
                        break
            print("\nПрогрев завершён у всех подключённых аккаунтов.")
        finally:
            watcher.cancel()
            store.beat(False)
            await pool.shutdown()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\nОстановлено. Прогресс сохранён — продолжить: Прогрев → 2.")


# ── пункты меню ─────────────────────────────────────────────────────────────

def _start() -> None:
    only = pick_accounts("Прогрев — выбор аккаунтов")
    if not only:
        return
    accounts = sorted(only)
    already = [a for a in accounts if store.get_account(a)]
    if already:
        print(f"\n⚠ Уже стоят на прогреве: {', '.join(already)} — запуск начнёт их прогрев ЗАНОВО.")

    print("\n--- Проверка перед запуском ---")
    warns = preflight(accounts)
    for w in warns:
        print(f"  ⚠ {w}")
    if not warns:
        print("  ✓ каналы, нейросеть и фото на месте")

    dialogs = len(accounts) >= 2
    if not dialogs:
        # Один аккаунт можно присоединить к уже идущему прогреву: пару ему подберут
        # среди тех, кто сейчас на прогреве с диалогами (тройка = две пары).
        running = [r["account"] for r in store.list_accounts(("active",))
                   if r["account"] not in accounts and r["dialogs"]]
        if running:
            print(f"\nНа прогреве уже идут {len(running)} акк. с диалогами. Новый аккаунт может "
                  f"переписываться с ними (пара подберётся автоматически).")
            if shared_proxy_warning(accounts + running):
                print("  ⚠ Все аккаунты выходят в сеть с ОДНОГО адреса (общий прокси).")
            dialogs = _yes("Включить взаимные диалоги с уже идущими аккаунтами?", default=True)
        else:
            print("\nАккаунт один — взаимные диалоги невозможны (нужна пара).")
    else:
        shared = shared_proxy_warning(accounts)
        print("\nВзаимные диалоги: аккаунты переписываются друг с другом, чтобы набрать «доверие».")
        if shared:
            print("  ⚠ ВСЕ выбранные аккаунты выходят в сеть с ОДНОГО адреса. Переписка между ними\n"
                  "    с одного IP — как раз то, что связывает аккаунты в одну сеть, а не выглядит\n"
                  "    разными людьми. Без отдельного прокси на аккаунт это скорее вредит, чем помогает.")
        dialogs = _yes("Включить взаимные диалоги?", default=not shared)

    set_name = _yes("Менять имя/фамилию (нейросеть)? Брендирование LINKTECH лучше ставить уже ПОСЛЕ прогрева",
                    default=True)

    # Отпечаток устройства: только тем, у кого его нет
    need = [a for a in accounts if not devices.get_device(a)]
    if need:
        from data import analytics as an
        fresh = [a for a in need if not (an.get_account(a) or {}).get("first_used_at")]
        old = [a for a in need if a not in fresh]
        if fresh:
            print(f"\nОтпечаток устройства для НЕ использованных ещё аккаунтов ({len(fresh)}): "
                  f"они ещё не светились в рассылке — задаём «телефонное» устройство.")
            if _yes("  Назначить?", default=True):
                for a in fresh:
                    devices.ensure_device(a)
        if old:
            print(f"\nУ {len(old)} аккаунтов уже есть история с другим отпечатком "
                  f"({', '.join(old)}). Смена устройства посреди жизни аккаунта сама по себе — лишний сигнал.")
            if _yes("  Всё равно назначить им новое устройство?", default=False):
                for a in old:
                    devices.ensure_device(a)

    print(f"\nПрогрев: {len(accounts)} акк., {cfg.WARMUP_DAYS} дн. всего, день запуска входит"
          f"{' (подписки, чтение, контакты и переписка идут в те же дни)' if dialogs else ''}, окно активности "
          f"{cfg.DAY_WINDOW_H[0]}:00–{cfg.DAY_WINDOW_H[1]}:00 по локальному времени аккаунта.")
    if not _yes("Запустить?", default=True):
        print("Отменено.")
        return

    import random
    starts = start_warmup(accounts, now=_now(), rng=random.Random(), dialogs=dialogs, set_name=set_name)
    print(f"Поставлено на прогрев: {len(starts)}. Первые действия — в ближайшие минуты "
          f"(старты разнесены по времени).")
    if store.loop_alive():
        print("\nПрогрев уже идёт в другом окне — новые аккаунты подхватятся сами (в течение ~20–30 секунд, "
              "если они не заняты). Второй цикл не запускаю.")
        return
    if _yes("\nЗапустить цикл прогрева сейчас? (окно должно остаться открытым несколько суток)", default=True):
        run_loop(accounts)


def _resume() -> None:
    rows = [a for a in store.list_accounts(("active", "paused"))]
    if not rows:
        print("Нет аккаунтов на прогреве. Сначала: Прогрев → 1.")
        return
    only = pick_accounts("Продолжить прогрев — выбор аккаунтов", names=[r["account"] for r in rows])
    if not only:
        return
    for a in only:
        if (store.get_account(a) or {}).get("state") == "paused":
            store.set_account(a, state="active")
    if store.loop_alive():
        print("\nПрогрев уже идёт в другом окне — возобновлённые аккаунты вернутся в него сами "
              "(в течение ~20–30 секунд). Второй цикл не запускаю, чтобы не было двух прогревов.")
        return
    run_loop(sorted(only))


def resume_headless() -> None:
    """
    Для сервера (systemd/tmux): продолжить прогрев всех АКТИВНЫХ аккаунтов без вопросов.
    Аккаунты на паузе остаются на паузе. Когда прогрев закончен — просто выходит (код 0).
    Если прокси сломался — завершается с ошибкой (код 2), systemd перезапустит.
    """
    names = sorted(r["account"] for r in store.list_accounts(("active",)))
    if not names:
        print("Нет активных аккаунтов на прогреве — делать нечего.")
        return
    print(f"Продолжаю прогрев: {len(names)} акк.")
    run_loop(names)


def _status() -> None:
    rows = describe_accounts(_now())
    if not rows:
        print("Ни один аккаунт не стоит на прогреве.")
        return
    print("\n--- Статус прогрева ---")
    print(f"{'аккаунт':<12} {'статус':<8} {'день':<8} {'фаза':<11} {'сделано':>7} {'ждёт':>5} "
          f"{'проп.':>5} {'ошиб.':>5}  дальше")
    names = {"active": "идёт", "paused": "пауза", "done": "готов", "dead": "МЁРТВ"}
    ph = {"profile": "профиль", "life": "жизнь", "dialog": "жизнь+диал.", "done": "—"}
    for r in rows:
        day = f"{r['day']}/{r['total_days']}"
        nxt = "—"
        if r["next"]:
            nxt = f"{r['next'][1]} в {r['next'][0].replace('T', ' ')[5:16]} UTC"
        print(f"{r['account']:<12} {names.get(r['state'], r['state']):<8} {day:<8} "
              f"{ph.get(r['phase'], r['phase']):<11} {r['done']:>7} {r['pending']:>5} "
              f"{r['skipped']:>5} {r['failed']:>5}  {nxt}")
        for e in r["errors"]:
            print(f"     ✗ {e['kind']}: {e['error']}")
        if r["state"] == "dead":
            print(f"     ✗ аккаунт мёртв ({r['note']}) — из прогрева исключён")


def _log() -> None:
    for e in reversed(store.recent_log(40)):
        print(f"  {e['ts'][5:16].replace('T', ' ')}  [{e['account'] or '—'}] "
              f"{'✓' if e['ok'] else '✗'} {e['detail']}")
    if not store.recent_log(1):
        print("Лог пуст.")


def _pause() -> None:
    act = [a["account"] for a in store.list_accounts(("active",))]
    if not act:
        print("Нет идущих прогревов.")
        return
    only = pick_accounts("Пауза прогрева — выбор аккаунтов", names=act)
    if only:
        for a in only:
            store.set_account(a, state="paused")
        print(f"На паузе: {', '.join(sorted(only))}. Задачи сохранены — продолжить: Прогрев → 2.")


def _reset() -> None:
    names = [a["account"] for a in store.list_accounts()]
    if not names:
        print("Нечего сбрасывать.")
        return
    only = pick_accounts("Сбросить прогрев — выбор аккаунтов", names=names)
    if only and _yes(f"Удалить состояние прогрева у {len(only)} аккаунт(ов) (задачи, подписки, переписка)?",
                     default=False):
        for a in only:
            store.remove_account(a)
        print("Сброшено. Сами аккаунты (имя, аватар, подписки в Telegram) не затронуты.")


def _devices() -> None:
    only = pick_accounts("Устройство — выбор аккаунтов")
    if not only:
        return
    print("\nНазначает аккаунту «телефонный» отпечаток устройства (вместо стандартного Telethon).\n"
          "Отпечаток выбирается ОДИН раз и больше не меняется. Для давно работающих аккаунтов\n"
          "смена устройства — лишний сигнал; надёжнее всего задавать его при первом входе.")
    if not _yes("Назначить выбранным?", default=False):
        return
    for a in sorted(only):
        d = devices.get_device(a) or devices.ensure_device(a)
        print(f"  {a}: {d['model']} / {d['system']} / {d['app']}")


def handle_warmup() -> None:
    while True:
        accs = store.list_accounts()
        active = sum(1 for a in accs if a["state"] == "active")
        print("\n--- Прогрев аккаунтов ---")
        print(f"  Аккаунтов в прогреве: {len(accs)} (идёт: {active})")
        print("  1. Запустить прогрев (выбрать аккаунты)")
        print("  2. Продолжить прогрев (цикл по уже поставленным)")
        print("  3. Статус")
        print("  4. Лог последних действий")
        print("  5. Пауза (выбрать аккаунты)")
        print("  6. Сбросить прогрев (выбрать аккаунты)")
        print("  7. Назначить «телефонное» устройство")
        print(f"  8. Каналы для подписок ({len(read_channels())} в {cfg.CHANNELS_FILE})")
        print("  0. Назад")
        choice = input("\nВыбор: ").strip()
        if choice == "1":
            _start()
        elif choice == "2":
            _resume()
        elif choice == "3":
            _status()
        elif choice == "4":
            _log()
        elif choice == "5":
            _pause()
        elif choice == "6":
            _reset()
        elif choice == "7":
            _devices()
        elif choice == "8":
            ch = read_channels()
            print(f"\nФайл: {os.path.abspath(cfg.CHANNELS_FILE)}")
            print("Открой его в блокноте, впиши каналы по одному на строку (@name), сохрани.")
            print(f"Сейчас каналов: {len(ch)}" + (f" → {', '.join(ch[:10])}" if ch else ""))
        elif choice == "0":
            break
        else:
            print("Неверный выбор.")
