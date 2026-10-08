# -*- coding: utf-8 -*-
"""
«Человеческий» отпечаток устройства на аккаунт.

По умолчанию Telethon представляется Telegram'у как "PC 64bit" /
"Windows ..." / версия самой библиотеки (1.4x) — на живых сессиях такого
почти нет, это самый заметный след автоматизации. План предлагает подменить
device_model / system_version / app_version на случайные «телефонные».

ВАЖНО (почему так сделано):
  • отпечаток выбирается ОДИН РАЗ на аккаунт и хранится в БД — один и тот же
    аккаунт при каждом подключении обязан выглядеть как одно и то же устройство.
    Случайный отпечаток при каждом запуске был бы хуже, чем никакого;
  • применять его лучше при ПЕРВОМ входе (так и написано в плане): у уже
    «обжитого» продавцом аккаунта смена устройства посреди жизни — сама по
    себе лишний сигнал. Поэтому для старых сессий это включается явно.

Список ниже — реалистичные пары модель/система/версия, но сверять их с
актуальными версиями приложения нужно самостоятельно и время от времени
обновлять: я не могу гарантировать, что они совпадают с текущими релизами.
"""
import random
from datetime import datetime, timezone

from . import store

# (device_model, system_version, app_version)
PROFILES: list[tuple[str, str, str]] = [
    # Android
    ("samsung SM-S918B", "SDK 34", "10.14.5"),
    ("samsung SM-S911B", "SDK 34", "10.14.5"),
    ("samsung SM-A546E", "SDK 34", "10.14.4"),
    ("samsung SM-A346B", "SDK 33", "10.14.4"),
    ("Google Pixel 8", "SDK 34", "10.14.5"),
    ("Google Pixel 7a", "SDK 34", "10.14.4"),
    ("OnePlus CPH2449", "SDK 34", "10.14.5"),
    ("Xiaomi 23049PCD8G", "SDK 33", "10.14.3"),
    ("Xiaomi 2201123G", "SDK 33", "10.14.3"),
    ("motorola moto g84 5G", "SDK 34", "10.14.4"),
    # iOS
    ("iPhone 14 Pro", "iOS 17.5.1", "10.13.1"),
    ("iPhone 13", "iOS 17.4.1", "10.13.1"),
    ("iPhone 15", "iOS 17.5.1", "10.13.1"),
    ("iPhone 12", "iOS 17.3.1", "10.12.2"),
]

LANG = ("en", "en-US")


def pick(rng: random.Random | None = None) -> dict:
    rng = rng or random.Random()
    model, system, app = rng.choice(PROFILES)
    return {"model": model, "system": system, "app": app,
            "lang": LANG[0], "system_lang": LANG[1]}


def get_device(account: str) -> dict | None:
    store.ensure_schema()
    with store._c() as c:
        r = c.execute("SELECT * FROM account_device WHERE account=?", (account,)).fetchone()
        return dict(r) if r else None


def ensure_device(account: str, rng: random.Random | None = None) -> dict:
    """Вернуть отпечаток аккаунта, создав его, если ещё нет (создаётся один раз)."""
    d = get_device(account)
    if d:
        return d
    d = pick(rng)
    with store._c() as c:
        c.execute("""INSERT OR IGNORE INTO account_device
                     (account, model, system, app, lang, system_lang, created_at)
                     VALUES (?,?,?,?,?,?,?)""",
                  (account, d["model"], d["system"], d["app"], d["lang"], d["system_lang"],
                   store.ts(datetime.now(timezone.utc))))
    return get_device(account) or d


def drop_device(account: str) -> None:
    store.ensure_schema()
    with store._c() as c:
        c.execute("DELETE FROM account_device WHERE account=?", (account,))


def device_kwargs(account: str, create: bool = False) -> dict:
    """
    kwargs для TelegramClient(...). Пусто, если отпечаток аккаунту не назначали
    (тогда поведение прежнее). create=True — назначить, если ещё нет.
    """
    try:
        d = ensure_device(account) if create else get_device(account)
    except Exception:  # noqa: BLE001 — БД не должна ломать само подключение
        return {}
    if not d:
        return {}
    return {
        "device_model": d["model"],
        "system_version": d["system"],
        "app_version": d["app"],
        "lang_code": d["lang"],
        "system_lang_code": d["system_lang"],
    }
