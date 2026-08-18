"""
optimizer/health.py — динамический дневной лимит на аккаунт.

Проблема сейчас: DM_LIMIT_PER_ACCOUNT = 20 одинаковый для всех аккаунтов
независимо от возраста и истории. Свежекупленный аккаунт и аккаунт,
работающий месяц без единой ошибки, получают одно и то же — первый горит,
второй недоиспользован.

Решение: cap = BASE * ramp(возраст) * health(история ошибок), сверху HARD_CAP.

  ramp    — новый аккаунт стартует медленно и разгоняется за неделю
  health  — множитель, который ПАДАЕТ вдвое при FloodWait/PeerFlood
            и медленно (+10%/чистый день) возвращается к 1.0

Важно: health никогда не поднимает лимит ВЫШЕ базового HARD_CAP.
Контур умеет только притормаживать и возвращаться к норме — это защита
аккаунтов и соблюдение лимитов площадки, а не поиск максимума.
"""

import math
from datetime import datetime, timedelta, timezone

from data import analytics as an

# Базовый дневной лимит ЛС на аккаунт (консервативный).
BASE_DAILY_DM = 25
# Жёсткий потолок — выше не поднимаемся никогда.
HARD_CAP = 35

# Разгон нового аккаунта: (возраст в днях, доля от базы)
RAMP = [(1, 0.20), (2, 0.30), (3, 0.45), (5, 0.65), (8, 0.85), (14, 1.0)]

# Порог ошибок за окно, после которого режем лимит.
ERROR_RATE_THRESHOLD = 0.10
ERROR_WINDOW_DAYS = 3

# На сколько отправлять аккаунт «отдыхать» после жёсткой ошибки.
REST_HOURS = {"peer_flood": 48, "flood_wait": 12, "banned": 24 * 365}

# Штраф/восстановление множителя
PENALTY = 0.5
RECOVERY_PER_DAY = 1.10
MIN_MULTIPLIER = 0.15


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def account_age_days(meta: dict) -> float:
    first = _parse(meta.get("first_used_at")) or _parse(meta.get("acquired_at"))
    if first is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - first).total_seconds() / 86400)


def ramp_factor(age_days: float) -> float:
    """Доля от базового лимита в зависимости от возраста аккаунта."""
    if age_days <= 0:
        return RAMP[0][1]
    factor = RAMP[-1][1]
    for days, f in RAMP:
        if age_days < days:
            factor = f
            break
    return factor


def is_resting(meta: dict) -> bool:
    until = _parse(meta.get("rest_until"))
    return until is not None and until > datetime.now(timezone.utc)


def is_available(account: str) -> bool:
    """Можно ли сегодня использовать аккаунт вообще."""
    meta = an.get_account(account)
    if meta is None:
        an.ensure_account(account)
        return True
    if meta.get("status") == "dead":
        return False
    return not is_resting(meta)


def daily_cap(account: str) -> int:
    """Сколько ЛС этот аккаунт может отправить сегодня (всего за день)."""
    meta = an.get_account(account)
    if meta is None:
        an.ensure_account(account)
        meta = an.get_account(account) or {}
    if meta.get("status") == "dead" or is_resting(meta):
        return 0

    mult = float(meta.get("cap_multiplier") or 1.0)
    cap = BASE_DAILY_DM * ramp_factor(account_age_days(meta)) * mult
    return max(0, min(HARD_CAP, int(math.floor(cap))))


def remaining_today(account: str) -> int:
    """Сколько ещё можно отправить сегодня с учётом уже отправленного."""
    return max(0, daily_cap(account) - an.sends_today(account))


def on_error(account: str, kind: str, detail: str | None = None) -> None:
    """
    Реакция на жёсткую ошибку: режем множитель и отправляем аккаунт отдыхать.
    kind: flood_wait | peer_flood | banned
    """
    an.log_event(account, kind, detail)
    meta = an.get_account(account) or {}
    mult = max(MIN_MULTIPLIER, float(meta.get("cap_multiplier") or 1.0) * PENALTY)

    hours = REST_HOURS.get(kind, 12)
    rest_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

    fields = {"cap_multiplier": mult, "rest_until": rest_until}
    if kind == "banned":
        fields["status"] = "dead"
        fields["dead_at"] = datetime.now(timezone.utc).isoformat()
    else:
        fields["status"] = "resting"
    an.update_account(account, **fields)


def on_clean_day(account: str) -> None:
    """Вызывается раз в сутки для аккаунтов без ошибок — плавное восстановление."""
    failed, total = an.account_error_rate(account, days=ERROR_WINDOW_DAYS)
    if total == 0:
        return
    rate = failed / total
    meta = an.get_account(account) or {}
    mult = float(meta.get("cap_multiplier") or 1.0)

    if rate > ERROR_RATE_THRESHOLD:
        mult = max(MIN_MULTIPLIER, mult * PENALTY)
        an.log_event(account, "throttled", f"error_rate={rate:.2%}")
    else:
        mult = min(1.0, mult * RECOVERY_PER_DAY)

    updates: dict = {"cap_multiplier": mult}
    if meta.get("status") == "resting" and not is_resting(meta):
        updates["status"] = "active"
    an.update_account(account, **updates)


def sweep() -> dict:
    """Проходит по всем аккаунтам: снимает отдых с отдохнувших, пересчитывает множители."""
    restored, throttled = 0, 0
    for meta in an.list_accounts():
        if meta["status"] == "dead":
            continue
        before = float(meta.get("cap_multiplier") or 1.0)
        on_clean_day(meta["name"])
        after = float((an.get_account(meta["name"]) or {}).get("cap_multiplier") or 1.0)
        if after > before:
            restored += 1
        elif after < before:
            throttled += 1
    return {"restored": restored, "throttled": throttled}


def report() -> list[dict]:
    """Состояние всех аккаунтов — для дашборда."""
    out = []
    for meta in an.list_accounts():
        name = meta["name"]
        failed, total = an.account_error_rate(name, days=ERROR_WINDOW_DAYS)
        out.append({
            "account": name,
            "status": "dead" if meta["status"] == "dead" else
                      ("resting" if is_resting(meta) else "active"),
            "source": meta.get("source"),
            "cost": meta.get("cost"),
            "age_days": round(account_age_days(meta), 1),
            "cap_multiplier": round(float(meta.get("cap_multiplier") or 1.0), 2),
            "daily_cap": daily_cap(name),
            "sent_today": an.sends_today(name),
            "remaining_today": remaining_today(name),
            "error_rate_3d": round(failed / total, 4) if total else 0.0,
            "lifetime_sends": an.account_lifetime_sends(name),
            "rest_until": meta.get("rest_until"),
        })
    return out


def total_capacity_today() -> int:
    """Суммарная свободная ёмкость пула на сегодня."""
    return sum(r["remaining_today"] for r in report() if r["status"] == "active")
