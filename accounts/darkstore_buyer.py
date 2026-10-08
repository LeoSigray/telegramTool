# -*- coding: utf-8 -*-
"""
Покупка аккаунтов на DarkStore (dark.shopping) через API: Telegram и VK.

  Telegram -> выданные .session / tdata попадают в sessions/ и проверяются на живую
              авторизацию (через прокси — жёсткий режим), покупка пишется в аналитику;
  VK       -> выданные логин:пароль сохраняются в базе (VK → «Купленные аккаунты»);
              для работы с VK API нужен токен — его получают отдельно (см. VK → 1).

Что именно DarkStore выдаёт за товар (ZIP с сессиями, tdata или текст), проверяется по
содержимому файла: формат разбирается автоматически. Первую покупку лучше сделать на 1 шт.
"""
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile

from config import CONFIG, SESSIONS_DIR
from . import darkstore as ds

TG_HINTS = ("telegram", "телеграм", "телега", "tg")
VK_HINTS = ("vk", "вк", "вконтакте", "контакте")


# ── Telegram: разбор выданного ─────────────────────────────────────────────

def _session_kind(path: str) -> str:
    """'telethon' / 'pyrogram' / 'unknown' по схеме sqlite-файла."""
    try:
        db = sqlite3.connect(path)
        cols = [r[1] for r in db.execute("PRAGMA table_info(sessions)")]
        db.close()
    except sqlite3.Error:
        return "unknown"
    if "server_address" in cols:
        return "telethon"
    if "user_id" in cols and "api_id" in cols:
        return "pyrogram"
    return "unknown"


def _name_from_json(session_path: str) -> str | None:
    stem = os.path.splitext(session_path)[0]
    for cand in (stem + ".json", os.path.join(os.path.dirname(session_path), "data.json")):
        if os.path.isfile(cand):
            try:
                with open(cand, encoding="utf-8") as f:
                    j = json.load(f)
                for k in ("phone", "phone_number", "user_id", "id"):
                    v = str(j.get(k) or "").strip().lstrip("+")
                    if v.isdigit():
                        return v
            except (OSError, ValueError):
                pass
    return None


def _store_session(src: str, name: str, dest_dir: str) -> str | None:
    """Кладёт сессию как <name>.session (Telethon). Pyrogram-формат конвертирует. None — не удалось."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name + ".session")
    kind = _session_kind(src)
    if kind == "telethon":
        shutil.copy(src, dest)
        return dest
    if kind == "pyrogram":
        from accounts.tdata_importer import _make_telethon_session
        db = sqlite3.connect(src)
        dc_id, auth_key, user_id = db.execute("SELECT dc_id, auth_key, user_id FROM sessions").fetchone()
        db.close()
        _make_telethon_session(os.path.join(dest_dir, name), int(dc_id), auth_key, int(user_id or 0))
        return dest
    return None


# Ответы Telegram, которые однозначно значат «этого аккаунта/сессии больше нет»
def _dead_errors():
    from telethon import errors as E
    names = ("AuthKeyUnregisteredError", "UserDeactivatedBanError", "UserDeactivatedError",
             "SessionRevokedError", "SessionExpiredError", "AuthKeyInvalidError")
    return tuple(getattr(E, n) for n in names if hasattr(E, n))


def check_session(path: str) -> tuple[str, str]:
    """
    ('live'|'dead'|'unknown', подробности). dead — ТОЛЬКО если Telegram прямо ответил, что ключа/аккаунта
    нет. Сеть, прокси, таймауты — это 'unknown': сессию в таком случае удалять нельзя.
    """
    import asyncio
    from telethon import TelegramClient
    from telethon.tl.functions.users import GetUsersRequest
    from telethon.tl.types import InputUserSelf
    from proxy_manager import client_kwargs_or_die

    name = os.path.splitext(os.path.basename(path))[0]

    async def _run():
        client = TelegramClient(path[:-len(".session")], CONFIG["TELEGRAM_API_ID"], CONFIG["TELEGRAM_API_HASH"],
                                **client_kwargs_or_die(name), connection_retries=3)
        try:
            await client.connect()
            await client(GetUsersRequest([InputUserSelf()]))
            return "live", ""
        except _dead_errors() as e:
            return "dead", f"Telegram: {type(e).__name__} (ключ сессии не принят)"
        except Exception as e:  # noqa: BLE001
            return "unknown", f"{type(e).__name__}: {str(e)[:80]}"
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    return asyncio.run(_run())


def import_telegram(path: str, order_id, unit_cost: float, *, verify: bool = True, log=print,
                    fetch=None) -> dict:
    """
    Разбирает выданный файл заказа. Поддержано:
      • ZIP/папка с .session (Telethon или Pyrogram) и/или tdata;
      • текст со ссылками на архивы («18325147520.zip: https://disk.yandex.ru/d/...») — архивы
        скачиваются (Яндекс.Диск / прямая ссылка) и разбираются так же.
    Возвращает {'live','dead','failed','texts'}.
    """
    from accounts.tdata_importer import convert_tdata
    fetch = fetch or ds.fetch_link
    res = {"live": [], "dead": [], "unverified": [], "failed": [], "texts": [], "notes": []}
    notes = res["notes"]
    cleanup: list[str] = []
    try:
        root = ds.unpack(path)
        cleanup.append(root)
        files = ds.collect_files(root)
        sources = [(files, None)]                       # (найденные файлы, подсказка для имени = номер телефона)

        if not files["session"] and not files["tdata"] and files["txt"]:
            text = "\n".join(ds._read_text(t) for t in files["txt"])
            links = ds.parse_links(text)
            if links:
                log(f"  В заказе ссылки на архивы: {len(links)} шт. — скачиваю...")
                dl = tempfile.mkdtemp(prefix="ds_dl_")
                cleanup.append(dl)
                for name, url in links:
                    try:
                        zp = fetch(url, dl)
                        kind, size_kb = ds.sniff(zp), os.path.getsize(zp) // 1024
                        sub = ds.unpack(zp)
                        cleanup.append(sub)
                        sources.append((ds.collect_files(sub), ds.hint_from_name(name)))
                        notes.append(f"{name or url[-14:]}: тип {kind}, {size_kb} КБ; {ds.describe_tree(sub)}")
                    except (ds.DarkstoreError, OSError, zipfile.BadZipFile) as e:
                        res["failed"].append(f"{name or url}: {e}")

        staged: list[tuple[str, str]] = []              # (имя, путь в sessions/)
        found_any = False
        for fl, hint in sources:
            for i, sp in enumerate(fl["session"], 1):
                found_any = True
                name = _name_from_json(sp) or hint or os.path.splitext(os.path.basename(sp))[0]
                if os.path.exists(os.path.join(SESSIONS_DIR, name + ".session")):
                    name = f"{name}_{order_id}_{i}"
                dest = _store_session(sp, name, SESSIONS_DIR)
                if dest:
                    staged.append((name, dest))
                else:
                    res["failed"].append(f"{os.path.basename(sp)}: неизвестный формат .session")
            for i, td in enumerate(fl["tdata"], 1):
                found_any = True
                tmpdir = tempfile.mkdtemp(prefix="ds_td_")
                cleanup.append(tmpdir)
                ok, err, info = convert_tdata(f"ds{order_id}_{i}", td, sessions_dir=tmpdir)
                if not ok:
                    res["failed"].append(f"tdata {hint or i}: {err}")
                    continue
                name = hint or str(info["user_id"])
                if os.path.exists(os.path.join(SESSIONS_DIR, name + ".session")):
                    name = f"{name}_{order_id}_{i}"
                os.makedirs(SESSIONS_DIR, exist_ok=True)
                dest = os.path.join(SESSIONS_DIR, name + ".session")
                shutil.move(info["session_file"], dest)
                staged.append((name, dest))

        if not found_any and not res["failed"] and not notes:
            res["texts"] = files["txt"]

        for name, dest in staged:
            state, detail = ("live", "") if not verify else check_session(dest)
            if state == "unknown":
                # Не смогли проверить (сеть/прокси): сессию НЕ удаляем, решим после проверки
                res["unverified"].append(f"{name}: {detail}")
                continue
            if state == "live":
                res["live"].append(name)
                if verify:
                    # Сразу завершаем чужие сессии (продавец, другие устройства): аккаунт теперь наш.
                    # Если сессии младше 24ч, Telegram не даёт — сброс выполнится сам позже.
                    try:
                        import asyncio
                        from accounts.lzt_buyer import _kick_via_session_file
                        asyncio.run(_kick_via_session_file(dest))
                    except Exception as e:  # noqa: BLE001
                        log(f"  [session] сброс чужих сессий для {name} не выполнен: {e}")
                try:
                    from data import analytics as an
                    from data.db import sync_session_to_db
                    an.register_purchase(name, source="darkstore", cost=unit_cost or 0.0, seller="darkstore")
                    sync_session_to_db(name, SESSIONS_DIR)
                except Exception as e:  # noqa: BLE001
                    log(f"  [analytics] не записана покупка {name}: {e}")
            else:
                res["dead"].append(f"{name}: {detail}")
                try:                              # в карантин, а не в мусор: пригодится для претензии продавцу
                    qdir = os.path.join(ds.PURCHASES_DIR, "dead")
                    os.makedirs(qdir, exist_ok=True)
                    shutil.move(dest, os.path.join(qdir, f"{order_id}_{name}.session"))
                except OSError:
                    pass
    finally:
        for d in cleanup:
            shutil.rmtree(d, ignore_errors=True)
    return res


# ── VK: разбор выданного ───────────────────────────────────────────────────

def import_vk(path: str, order_id, product_name: str, unit_cost: float) -> int:
    """Логины/пароли из выданных текстов -> база (vk_purchased). Возвращает, сколько записано."""
    from vk import store as vks
    root = ds.unpack(path)
    n = 0
    try:
        for tp in ds.collect_files(root)["txt"]:
            for acc in ds.parse_credentials(ds._read_text(tp)):
                vks.add_purchased(acc["login"], acc["password"], acc["extra"], str(order_id),
                                  product_name, unit_cost)
                n += 1
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return n


# ── интерактив ──────────────────────────────────────────────────────────────

def _fmt_guarantee(sec) -> str:
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        return "—"
    if sec <= 0:
        return "нет"
    if sec < 3600:
        return f"{max(1, sec // 60)} мин"
    return f"{sec // 3600} ч" if sec < 86400 else f"{sec // 86400} сут"


def _api() -> "ds.DarkstoreAPI | None":
    key = CONFIG.get("DARKSTORE_API_KEY", "")
    if not key:
        print("\nНе задан ключ DarkStore API.\n"
              "  1. Подай заявку на доступ к API: https://dark.shopping/developer\n"
              "     (нужны баланс на аккаунте и привязанный Telegram).\n"
              "  2. Впиши ключ в .env:  DARKSTORE_API_KEY=...\n"
              "  3. Перезапусти программу.")
        return None
    return ds.DarkstoreAPI(key)


# В категориях DarkStore вместе с аккаунтами лежат каналы, Stars, услуги, софт, «ретрив» и спамблок.
# По умолчанию показываем только подкатегории, где продаются сами аккаунты.
ACCOUNT_GROUPS = {
    "telegram": re.compile(r"авторег|ручная регистрация", re.I),
    "vk": re.compile(r"авторег|ручная регистрация|раскрученные аккаунты", re.I),
}


def _clean(text) -> str:
    """Для вывода: убираем эмодзи и прочие символы, которые не печатаются в консоли Windows."""
    return str(text or "").encode("cp1251", "ignore").decode("cp1251").strip()


def _choose_groups(api, category_id, kind: str) -> list:
    """Список id выбранных подкатегорий ([] — искать по всей категории)."""
    groups = api.groups(category_id)
    pattern = ACCOUNT_GROUPS[kind]
    shown = [g for g in groups if pattern.search(str(g.get("name")))] or groups
    while True:
        print("\nПодкатегории (аккаунты):")
        for i, g in enumerate(shown, 1):
            print(f"  {i}. {_clean(g.get('name'))}")
        print("  a. Показать все подкатегории (каналы, услуги, ретрив, спамблок…)")
        raw = input("Выбор (Enter = все из списка выше): ").strip().lower()
        if raw == "a":
            shown = groups
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(shown):
            return [shown[int(raw) - 1].get("id")]
        return [g.get("id") for g in shown] if shown is not groups else []


def _finish_delivery(api, kind: str, oid, link: str, unit: float, product_name: str = "") -> None:
    """Скачивает выданный файл заказа и импортирует аккаунты (общая часть покупки и «забрать заказ»)."""
    try:
        path = api.download(link, oid)
    except ds.DarkstoreError as e:
        print(f"DarkStore: {e}")
        return
    print(f"Файл заказа: {path}")
    if kind == "telegram":
        res = import_telegram(path, oid, unit)
        print(f"\nИмпортировано рабочих: {len(res['live'])} {res['live']}")
        if res["dead"]:
            print(f"Нерабочих (Telegram не принимает ключ; отложены в {os.path.join(ds.PURCHASES_DIR, 'dead')}):")
            for d in res["dead"]:
                print(f"  ✗ {d}")
            print("  Это не ошибка программы: продавец выдал мёртвые сессии. Пиши в поддержку DarkStore "
                  "с номером заказа — у товара есть гарантия на замену.")
        if res["unverified"]:
            print("Не удалось проверить (сеть/прокси) — сессии ОСТАВЛЕНЫ в sessions/, проверь позже "
                  "командой check_account.py:")
            for u in res["unverified"]:
                print(f"  ? {u}")
        for f in res["failed"]:
            print(f"  ✗ {f}")
        if res["notes"] and not res["live"]:
            print("Архивы скачаны, но .session/tdata в них не найдены. Что внутри (только имена файлов):")
            for n in res["notes"]:
                print(f"  • {n}")
            print("Пришли этот список — доработаю разбор (имена файлов без паролей).")
        if res["texts"]:
            print("В заказе нет .session/tdata, только текст — формат не распознан. Файлы:")
            for t in res["texts"]:
                print(f"  {t}")
            print("Пришли пример структуры (без паролей) — доработаю разбор.")
    else:
        n = import_vk(path, oid, product_name, unit)
        print(f"\nСохранено аккаунтов VK: {n}. Дальше: VK → «Купленные аккаунты» — там инструкция по токену.")


def _claim_order(api, kind: str) -> None:
    raw = input("Номер заказа: ").strip()
    if not raw.isdigit():
        print("Отменено.")
        return
    try:
        st = api.order_status(raw)
        print(f"Статус заказа {raw}: {st}")
        if st != "completed":
            print("Заказ ещё не выполнен — попробуй позже.")
            return
        link = api.order_link(raw)
    except ds.DarkstoreError as e:
        print(f"DarkStore: {e}")
        return
    if not link:
        print("Ссылки на файл нет — скачай заказ на сайте.")
        return
    c = input("Цена за штуку для учёта в аналитике (Enter — 0): ").strip().replace(",", ".")
    _finish_delivery(api, kind, raw, link, float(c) if re.fullmatch(r"\d+(\.\d+)?", c) else 0.0)


PAGE_SIZE = 30
SORTS = {
    "1": ("цена, дешёвые сверху", lambda p: float(p.get("price") or 0), False),
    "2": ("цена, дорогие сверху", lambda p: float(p.get("price") or 0), True),
    "3": ("качество", lambda p: p.get("quality_percent") if p.get("quality_percent") is not None else -1, True),
    "4": ("остаток на складе", lambda p: int(p.get("quantity") or 0), True),
    "5": ("отлёжка", lambda p: ds.product_days(p) or 0, True),
}


def _ask_price(prompt: str) -> float | None:
    raw = input(prompt).strip().replace(",", ".")
    return float(raw) if re.fullmatch(r"[0-9]+([.][0-9]+)?", raw) else None


def _ask_int(prompt: str) -> int | None:
    raw = input(prompt).strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _ask_country() -> str | None:
    """Страна: название по-русски или по-английски. Возвращает ISO-2 или None (любая)."""
    from .countries import normalize_country_query, country_label
    raw = input("Страна (например США, Канада, Индия; Enter — любая): ").strip()
    if not raw:
        return None
    iso = normalize_country_query(raw)
    if not iso:
        print(f"  Не распознал «{raw}» — страна не фильтруется.")
        return None
    print(f"  Страна: {country_label(iso)} ({iso})")
    return iso


def _pick_product(products: list[dict]) -> dict | None:
    """Таблица товаров с листанием и сортировкой; возвращает выбранный товар или None."""
    sort_key, page = "1", 0
    while True:
        title, key, rev = SORTS[sort_key]
        ordered = sorted(products, key=key, reverse=rev)
        pages = max(1, (len(ordered) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        chunk = ordered[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        print(f"\nНайдено {len(ordered)}; сортировка: {title}; страница {page + 1}/{pages}")
        print(f"{'№':>4} {'id':>7} {'цена':>7} {'нал.':>5} {'гар.':>6} {'кач.':>5} {'страна':<11} "
              f"{'отл.':>4} {'2FA':>3} {'формат':<7} название")
        for i, p in enumerate(chunk, page * PAGE_SIZE + 1):
            qual = p.get("quality_percent")
            days = ds.product_days(p)
            manual = " [ВРУЧНУЮ]" if (p.get("is_manual_order_delivery") or p.get("manual")) else ""
            print(f"{i:>4} {str(p.get('id')):>7} {float(p.get('price') or 0):>7.2f} {str(p.get('quantity')):>5} "
                  f"{_fmt_guarantee(p.get('guarantee_time_seconds')):>6} "
                  f"{(str(qual) + '%') if qual is not None else '-':>5} "
                  f"{_clean(ds.product_country(p))[:11]:<11} {str(days) if days is not None else '-':>4} "
                  f"{'да' if ds.has_2fa(p) else '-':>3} {ds.product_formats(p)[:7]:<7} "
                  f"{_clean(p.get('name'))[:48]}{manual}")
        print("(API не отдаёт продавца: ссылка на страницу товара покажется после выбора)")
        cmd = input("Номер товара | n — дальше | p — назад | s — сортировка | Enter — отмена: ").strip().lower()
        if not cmd:
            return None
        if cmd == "n":
            page += 1
        elif cmd == "p":
            page -= 1
        elif cmd == "s":
            for k, (t, _, _) in SORTS.items():
                print(f"  {k}. {t}")
            sort_key = input("Сортировка (Enter — оставить): ").strip()
            sort_key = sort_key if sort_key in SORTS else "1"
            page = 0
        elif cmd.isdigit() and 1 <= int(cmd) <= len(ordered):
            return ordered[int(cmd) - 1]
        else:
            print("Не понял команду.")


def buy_interactive(kind: str) -> None:
    """kind: 'telegram' | 'vk'."""
    if kind == "telegram":
        from proxy_manager import proxy_guard
        if not proxy_guard(["_new_account"], "покупку и проверку аккаунтов"):
            return
    api = _api()
    if not api:
        return
    hints = TG_HINTS if kind == "telegram" else VK_HINTS
    title = "Telegram" if kind == "telegram" else "VK"

    print("\n  1. Купить")
    print("  2. Забрать уже оплаченный заказ по номеру (например, если выдача была вручную)")
    if input("Выбор (Enter = 1): ").strip() == "2":
        _claim_order(api, kind)
        return

    try:
        bal, cur = api.balance()
        print(f"\nDarkStore. Баланс: {bal} {cur}")
        cats = ds.pick_by_name(api.categories(), hints)
        if not cats:
            print(f"Категория «{title}» в каталоге DarkStore не найдена.")
            return
        cat = cats[0]
        if len(cats) > 1:
            print("\nНесколько категорий:")
            for i, c in enumerate(cats, 1):
                print(f"  {i}. {c.get('name')}")
            raw = input("Номер (Enter = 1): ").strip()
            cat = cats[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(cats) else cats[0]
        print(f"Категория: {cat.get('name')}")
        group_ids = _choose_groups(api, cat.get("id"), kind)

        kw = input("Слово в названии (Enter — без фильтра): ").strip() or None
        iso = _ask_country()
        price_from = _ask_price("Минимальная цена за штуку (Enter — без ограничения): ")
        price_to = _ask_price("Максимальная цена за штуку (Enter — без ограничения): ")
        min_days = _ask_int("Минимальная отлёжка в днях (Enter — любая): ")
        min_q = _ask_int("Минимальное качество, % (Enter — любое; товары без оценки остаются): ")
        show_risky = input("Показывать «ретрив» и спамблок? (y/N): ").strip().lower() == "y"

        products, seen = [], set()
        for gid in (group_ids or [None]):
            for p in api.products(category_id=cat.get("id"), group_id=gid, name=kw, price_from=price_from,
                                  price_to=price_to, per_page=500, max_pages=6):
                if p.get("id") not in seen:
                    seen.add(p.get("id"))
                    products.append(p)
    except ds.DarkstoreError as e:
        print(f"DarkStore: {e}")
        return

    if not show_risky:
        hidden = [p for p in products if ds.is_risky(p)]
        products = [p for p in products if not ds.is_risky(p)]
        if hidden:
            print(f"(скрыто {len(hidden)} «ретрив»/спамблок — их можно показать, ответив y выше)")
    if iso:
        from .countries import normalize_country_query
        before = len(products)
        products = [p for p in products if ds.matches_country(p, iso, normalize_country_query)]
        print(f"По стране осталось {len(products)} из {before}.")
    if min_days:
        products = [p for p in products if (ds.product_days(p) or 0) >= min_days]
    if min_q:
        products = [p for p in products if p.get("quality_percent") is None or p.get("quality_percent") >= min_q]
    if not products:
        print("Подходящих товаров нет. Ослабь фильтры (страна, цена, отлёжка).")
        return

    prod = _pick_product(products)
    if not prod:
        print("Отменено.")
        return
    print(f"Ссылка на товар (там видно детали и условия замены): {prod.get('url')}")
    stock = int(prod.get("quantity") or 1)
    lo = max(1, int(prod.get("minimum_order") or 1))
    if prod.get("is_manual_order_delivery") or prod.get("manual"):
        print("  ⚠ Выдача вручную: продавец отдаёт заказ не сразу. Программа подождёт несколько минут,\n"
              "    если не дождётся — заказ можно забрать позже (при повторном входе в покупку, пункт 2).")
    q = input(f"Сколько штук ({lo}–{stock}, Enter = {lo}): ").strip()
    qty = int(q) if q.isdigit() and lo <= int(q) <= stock else lo
    unit = float(prod.get("price") or 0)
    total = round(unit * qty, 2)
    print(f"\nКупить: {_clean(prod.get('name'))}\n  {qty} шт. × {unit} = {total} {cur}; баланс {bal} {cur}")
    if total > bal:
        print("Не хватает баланса. Пополни его на сайте DarkStore.")
        return
    if input("Купить? (y/n): ").strip().lower() != "y":
        print("Отменено — ничего не куплено.")
        return

    try:
        order = api.create_order(prod.get("id"), qty)
        oid = order.get("id")
        if not oid:
            print(f"Заказ не создан: {order}")
            return
        print(f"Заказ {oid} создан, жду выдачу...")
        link = order.get("link")
        if not link:
            st = api.wait_completed(oid)
            if st != "completed":
                print(f"Заказ {oid} пока не выполнен (статус: {st}). Деньги списаны или зарезервированы — "
                      f"забрать заказ позже можно здесь же (пункт 2 «Забрать заказ по номеру»).")
                return
            link = api.order_link(oid)
        if not link:
            print("Заказ выполнен, но ссылки на файл нет — скачай на сайте.")
            return
    except ds.DarkstoreError as e:
        print(f"DarkStore: {e}")
        return
    _finish_delivery(api, kind, oid, link, unit, _clean(prod.get("name")))
