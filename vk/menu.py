# -*- coding: utf-8 -*-
"""Консольное меню VK: токены, подбор ЦА (студенты СПб, мужчины), рассылка в ЛС."""
import os
from datetime import datetime

from . import audience, config as cfg, store
from .api import VKApi, VKError
from .sender import render, run_broadcast, _usable


def _mask(token: str) -> str:
    return token[:6] + "…" + token[-4:] if len(token) > 12 else "…"


def input_multiline(prompt: str) -> str:
    print(prompt)
    print('Вставь или напиши текст. Когда закончишь — с новой строки "END" и Enter.')
    lines = []
    while True:
        line = input()
        if line.strip().upper() == "END":
            break
        lines.append(line.rstrip("\r"))
    return "\n".join(lines)


def _read_api() -> VKApi | None:
    """API для чтения (поиск сообществ, участники): VK_TOKEN из .env или первый живой токен."""
    token = os.getenv("VK_TOKEN", "").strip()
    if not token:
        live = [a for a in store.list_accounts() if a["status"] != "dead"]
        token = live[0]["token"] if live else ""
    if not token:
        print("Нет токена VK. Добавь его: VK → 1 (Токены), либо VK_TOKEN в .env.")
        return None
    return VKApi(token)


# ── токены ──────────────────────────────────────────────────────────────────

def _check_token(token: str) -> tuple[int, str] | None:
    try:
        r = VKApi(token).call("users.get")
        u = r[0]
        return int(u["id"]), f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
    except VKError as e:
        print(f"  ✗ токен не принят VK: {e.message}")
    except ConnectionError as e:
        print(f"  ✗ {e}")
    return None


def _tokens_menu() -> None:
    while True:
        accs = store.list_accounts()
        print("\n--- Токены VK (аккаунты для рассылки и чтения) ---")
        for a in accs:
            used = store.sent_last_24h(a["id"])
            state = {"active": "активен", "resting": "отдыхает", "dead": "МЁРТВ"}.get(a["status"], a["status"])
            print(f"  {a['id']}. {a['name'] or '?'} (id{a['user_id']})  {_mask(a['token'])}  "
                  f"{state}  за 24ч отправлено {used}/{cfg.DM_LIMIT_PER_ACCOUNT}")
        if not accs:
            print("  (пусто)")
        print("\n  1. Добавить токен")
        print("  2. Проверить все токены")
        print("  3. Удалить токен")
        print("  0. Назад")
        ch = input("Выбор: ").strip()
        if ch == "1":
            print("Нужен user access token со scope: messages, groups, offline.\n"
                  "Получение: создай Standalone-приложение на vk.com/apps?act=manage, затем открой\n"
                  "https://oauth.vk.com/authorize?client_id=ID_ПРИЛОЖЕНИЯ&scope=messages,groups,offline"
                  "&response_type=token&v=5.199&redirect_uri=https://oauth.vk.com/blank.html\n"
                  "и скопируй access_token из адресной строки.")
            token = input("Токен (Enter — отмена): ").strip()
            if not token:
                continue
            res = _check_token(token)
            if res:
                store.add_account(token, res[0], res[1])
                print(f"  ✓ добавлен: {res[1]} (id{res[0]})")
        elif ch == "2":
            for a in accs:
                res = _check_token(a["token"])
                store.set_account_status(a["id"], "active" if res else "dead")
                print(f"  {a['id']}: {'ок — ' + res[1] if res else 'не работает'}")
        elif ch == "3":
            raw = input("Номер для удаления: ").strip()
            if raw.isdigit():
                store.delete_account(int(raw))
                print("  удалено.")
        elif ch == "0":
            return


# ── подбор ЦА ───────────────────────────────────────────────────────────────

def _load_communities_file() -> list[str]:
    try:
        with open(cfg.COMMUNITIES_FILE, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
    except OSError:
        return []


def _save_communities_file(groups: list[dict]) -> None:
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    with open(cfg.COMMUNITIES_FILE, "w", encoding="utf-8") as f:
        f.write("# Сообщества СПб для подбора ЦА (по одному в строке: ссылка или короткое имя).\n")
        for g in groups:
            f.write(f"{g['screen_name']}   # {g['name']} — {g['members_count']} уч.\n")


def _pick_communities(api: VKApi) -> list[dict]:
    saved = _load_communities_file()
    groups: list[dict] = []
    if saved:
        print(f"\nВ {cfg.COMMUNITIES_FILE} сохранён список ({len(saved)} сообществ).")
        if input("Использовать его? (Enter = да, 2 = подобрать заново): ").strip() != "2":
            groups = audience.resolve_communities(api, saved)
    if not groups:
        print(f"\nИщу топ-{cfg.TOP_COMMUNITIES} открытых сообществ Санкт-Петербурга по числу участников...")
        groups = audience.find_top_communities(api)
    while True:
        print("\nСообщества для подбора ЦА:")
        for i, g in enumerate(groups, 1):
            print(f"  {i:>2}. {g['name'][:50]:<50} {g['members_count']:>9} уч.  vk.com/{g['screen_name']}")
        print("\n  Enter — использовать этот список")
        print("  2     — вписать свои (ссылки/имена через пробел или запятую)")
        print("  0     — отмена")
        ch = input("Выбор: ").strip()
        if ch == "":
            _save_communities_file(groups)
            return groups
        if ch == "0":
            return []
        if ch == "2":
            raw = input("Сообщества: ")
            refs = [r for r in raw.replace(",", " ").split() if r]
            found = audience.resolve_communities(api, refs) if refs else []
            if found:
                groups = found
            else:
                print("  Ничего не нашлось — проверь ссылки.")


def _collect_audience() -> None:
    api = _read_api()
    if not api:
        return
    try:
        groups = _pick_communities(api)
        if not groups:
            return
        total = sum(g["members_count"] for g in groups)
        print(f"\nУчастников суммарно: {total}. Это ~{total // cfg.MEMBERS_PAGE + len(groups)} запросов, "
              f"примерно {(total // cfg.MEMBERS_PAGE + len(groups)) * cfg.API_MIN_INTERVAL_S / 60:.0f} мин.")
        raw = input("Ограничить число участников на сообщество? (Enter = все): ").strip()
        limit = int(raw) if raw.isdigit() and int(raw) > 0 else None
        print("\nСобираю участников (мужчины, город — Санкт-Петербург)...")
        found, scanned = audience.collect(api, groups, limit)
    except VKError as e:
        print(f"VK вернул ошибку: {e.message}")
        return
    except ConnectionError as e:
        print(str(e))
        return
    except KeyboardInterrupt:
        print("\nОстановлено, ничего не сохранено.")
        return
    if not scanned:
        print("Ни одно сообщество не удалось прочитать.")
        return
    if scanned < len(groups):
        print(f"⚠ Прочитано {scanned} из {len(groups)} — «a» считается от прочитанных.")

    rows = audience.rank(found, scanned)
    fit = sum(1 for r in rows if r["b"] > 0)
    print(f"\nМужчин из СПб в сообществах: {len(found)}")
    print(f"Состоят минимум в {cfg.MIN_COMMUNITIES} из {scanned}: {len(rows)}")
    print(f"  из них со ВУЗом СПб и подходящим годом начала (±{cfg.FIT_YEARS}): {fit}")
    new, upd = store.save_targets(rows)
    print(f"Сохранено в базу: новых {new}, обновлено {upd} (обработанные не воскрешаются).")
    _print_top(20)


def _print_top(n: int = 20) -> None:
    rows = store.list_targets("new", n)
    c = store.counts()
    print(f"\nВ базе: {', '.join(f'{k} — {v}' for k, v in sorted(c.items())) or 'пусто'}")
    if not rows:
        return
    print(f"{'#':>3} {'id':>11} {'имя':<22} {'сообщ.':>6} {'a':>5} {'b':>5} {'score':>6}  ВУЗ (год начала)")
    for i, r in enumerate(rows, 1):
        uni = f"{(r['university'] or '')[:30]} ({r['start_year']})" if r["university"] else "—"
        print(f"{i:>3} {r['user_id']:>11} {(r['first_name'] + ' ' + r['last_name'])[:22]:<22} "
              f"{r['communities']:>6} {r['a']:>5.2f} {r['b']:>5.2f} {r['score']:>6.2f}  {uni}")


def _export() -> None:
    rows = store.list_targets(None)
    if not rows:
        print("База ЦА пуста.")
        return
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["id", "ссылка", "имя", "фамилия", "сообществ", "a", "b", "score", "ВУЗ", "год начала", "статус"])
    for r in rows:
        ws.append([r["user_id"], f"https://vk.com/id{r['user_id']}", r["first_name"], r["last_name"],
                   r["communities"], r["a"], r["b"], r["score"], r["university"], r["start_year"], r["status"]])
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    wb.save(cfg.EXPORT_FILE)
    print(f"Выгружено {len(rows)} строк -> {cfg.EXPORT_FILE}")


# ── рассылка ────────────────────────────────────────────────────────────────

def _pick_text() -> str | None:
    from data.vk_static_message import VK_STATIC_MESSAGE
    print("\n--- Текст сообщения (можно использовать {name} — имя получателя) ---")
    print("  1. Статичный (data/vk_static_message.py)")
    print("  2. Динамичный (написать/вставить новый)")
    if input("Выбор (Enter = динамичный): ").strip() == "1":
        if VK_STATIC_MESSAGE.strip():
            return VK_STATIC_MESSAGE
        print("VK_STATIC_MESSAGE пустой — впиши текст в data/vk_static_message.py.")
    return input_multiline("Текст сообщения:").strip() or None


def _broadcast() -> None:
    accs = store.list_accounts()
    if not accs:
        print("Нет токенов VK — добавь: VK → 1.")
        return
    now = datetime.now().astimezone()
    usable = [a for a in accs if _usable(a, now)]
    capacity = sum(cfg.DM_LIMIT_PER_ACCOUNT - store.sent_last_24h(a["id"]) for a in usable)
    targets = store.list_targets("new")
    print(f"\nАккаунтов доступно: {len(usable)} из {len(accs)}; лимит на сейчас ≈ {capacity} сообщений.")
    print(f"Целей в базе (new): {len(targets)}")
    if not targets:
        print("Сначала подбери ЦА: VK → 2.")
        return
    if not usable:
        print("Все аккаунты исчерпали лимит/отдыхают/мертвы.")
        return
    raw = input(f"Скольким отправить? (Enter = {min(capacity, len(targets))}): ").strip()
    limit = int(raw) if raw.isdigit() and int(raw) > 0 else min(capacity, len(targets))
    text = _pick_text()
    if not text:
        print("Пустой текст — отмена.")
        return
    first = targets[0]
    print(f"\nПример (получателю id{first['user_id']}):\n---\n{render(text, first)}\n---")
    print(f"Будет отправлено до {limit} (сверху — самые подходящие), пауза "
          f"{cfg.DM_DELAY_MIN}–{cfg.DM_DELAY_MAX} с, до {cfg.DM_LIMIT_PER_ACCOUNT} на аккаунт в сутки.")
    if input("Запустить? (y/n): ").strip().lower() != "y":
        print("Отменено.")
        return
    try:
        st = run_broadcast(text, limit)
    except KeyboardInterrupt:
        print("\nОстановлено. Уже отправленные отмечены в базе.")
        return
    print(f"\nИтог: отправлено {st['sent']}, нельзя написать {st['closed']}, ошибок {st['failed']}, "
          f"в очереди осталось {st['left']}.")
    if st["stop"]:
        print(f"Остановлено: {st['stop']}")


# ── купленные аккаунты ──────────────────────────────────────────────────────

def _purchased_menu() -> None:
    """Купленные на DarkStore логин/пароль. Для рассылки и чтения нужен токен каждого аккаунта."""
    rows = store.list_purchased()
    if not rows:
        print("\nКупленных аккаунтов нет. Купить: VK → 6.")
        return
    print(f"\n--- Купленные аккаунты VK ({len(rows)}) ---")
    for r in rows:
        mark = "токен есть" if r["has_token"] else "ждёт токен"
        print(f"  {r['id']:>3}. {r['login']}   {mark}   заказ {r['order_id']}")
    print("\nКак получить токен для аккаунта:\n"
          "  1. Войди в аккаунт в браузере (лучше отдельный профиль, пароль — из выгрузки ниже).\n"
          "  2. Открой ссылку из VK → 1 (Токены), нажми «Разрешить», скопируй access_token из адреса.\n"
          "  3. Добавь токен: VK → 1 → «Добавить токен», затем отметь здесь аккаунт как готовый.")
    print("\n  v. Показать логины и пароли (открытым текстом)")
    print("  <номер>. Отметить аккаунт как «токен получен»")
    print("  Enter — назад")
    ch = input("Выбор: ").strip().lower()
    if ch == "v":
        for r in rows:
            print(f"  {r['id']:>3}. {r['login']}:{r['password']}" + (f":{r['extra']}" if r["extra"] else ""))
    elif ch.isdigit():
        store.mark_purchased_token(int(ch), True)
        print("  отмечено.")


# ── вход ────────────────────────────────────────────────────────────────────

def handle_vk() -> None:
    while True:
        c = store.counts()
        print("\n" + "=" * 50)
        print("       VK АВТОМАТИЗАТОР")
        print("=" * 50)
        print(f"  Токенов: {len(store.list_accounts())}   ЦА в базе: "
              f"{sum(c.values())} (новых {c.get('new', 0)}, отправлено {c.get('sent', 0)})")
        print("  Целевая аудитория: студенты СПб, мужчины")
        print("-" * 50)
        print("  1. Токены VK (аккаунты)")
        print("  2. Подобрать ЦА")
        print("  3. Показать топ ЦА")
        print("  4. Выгрузить ЦА в Excel")
        print("  5. Рассылка в личные сообщения")
        print("  6. Купить аккаунты VK (DarkStore)")
        print("  7. Купленные аккаунты (ждут токен)")
        print("  0. Назад (выбор платформы)")
        print("-" * 50)
        ch = input("\nВыбор: ").strip()
        if ch == "1":
            _tokens_menu()
        elif ch == "2":
            _collect_audience()
        elif ch == "3":
            _print_top(int(raw) if (raw := input("Сколько показать (20): ").strip()).isdigit() else 20)
        elif ch == "4":
            _export()
        elif ch == "5":
            _broadcast()
        elif ch == "6":
            from accounts.darkstore_buyer import buy_interactive
            buy_interactive("vk")
        elif ch == "7":
            _purchased_menu()
        elif ch == "0":
            return
        else:
            print("Неверный выбор.")
