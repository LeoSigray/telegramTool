"""
Трекинг дневных лимитов на аккаунт.

Хранит данные в data/daily_usage.json:
{
    "2026-03-20": {
        "account_name": {"joins": 3, "sends": 8}
    }
}

Сбрасывается автоматически при смене дня.
"""

import json
import os
from datetime import date

from config import DATA_DIR, JOIN_LIMIT_PER_ACCOUNT, DM_LIMIT_PER_ACCOUNT

USAGE_FILE = os.path.join(DATA_DIR, "daily_usage.json")

# Дневные лимиты (безопасные значения для Telegram)
DAILY_JOIN_LIMIT = 35    # вступлений в чаты за день
DAILY_SEND_LIMIT = 800    # сообщений в чаты за день
DAILY_DM_LIMIT = 39      # ЛС за день


def _load() -> dict:
    if not os.path.exists(USAGE_FILE):
        return {}
    with open(USAGE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _today() -> str:
    return date.today().isoformat()


def _get_today_usage(account: str) -> dict:
    data = _load()
    today = _today()
    return data.get(today, {}).get(account, {"joins": 0, "sends": 0, "dms": 0})


def _update_today_usage(account: str, usage: dict):
    data = _load()
    today = _today()

    # Чистим старые дни (оставляем только сегодня)
    data = {k: v for k, v in data.items() if k == today}

    if today not in data:
        data[today] = {}
    data[today][account] = usage
    _save(data)


def get_daily_joins_left(account: str) -> int:
    """Сколько вступлений осталось на сегодня."""
    usage = _get_today_usage(account)
    return max(0, DAILY_JOIN_LIMIT - usage.get("joins", 0))


def get_daily_sends_left(account: str) -> int:
    """Сколько отправок в чаты осталось на сегодня."""
    usage = _get_today_usage(account)
    return max(0, DAILY_SEND_LIMIT - usage.get("sends", 0))


def record_join(account: str):
    """Записывает одно вступление."""
    usage = _get_today_usage(account)
    usage["joins"] = usage.get("joins", 0) + 1
    _update_today_usage(account, usage)


def record_send(account: str):
    """Записывает одну отправку в чат."""
    usage = _get_today_usage(account)
    usage["sends"] = usage.get("sends", 0) + 1
    _update_today_usage(account, usage)


def record_dm(account: str):
    """Записывает одну отправку в ЛС."""
    usage = _get_today_usage(account)
    usage["dms"] = usage.get("dms", 0) + 1
    _update_today_usage(account, usage)


def get_account_daily_stats(account: str) -> dict:
    """Полная статистика аккаунта за сегодня."""
    usage = _get_today_usage(account)
    return {
        "joins": usage.get("joins", 0),
        "joins_left": max(0, DAILY_JOIN_LIMIT - usage.get("joins", 0)),
        "sends": usage.get("sends", 0),
        "sends_left": max(0, DAILY_SEND_LIMIT - usage.get("sends", 0)),
        "dms": usage.get("dms", 0),
        "dms_left": max(0, DAILY_DM_LIMIT - usage.get("dms", 0)),
    }
