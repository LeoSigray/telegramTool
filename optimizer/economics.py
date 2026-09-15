"""
optimizer/economics.py — деньги.

Главная формула, из которой всё следует:

    CPL = стоимость_сообщения / (reply_rate × lead_rate_при_ответе)

    стоимость_сообщения = цена_аккаунта / сколько_сообщений_он_успеет_отправить

Отсюда два рычага снижения цены лида:

  1. Увеличить срок жизни аккаунта  → дешевле каждое сообщение
  2. Увеличить reply_rate           → нужно меньше сообщений на лид

Второй рычаг сильнее и он же безопаснее: рост reply-rate вдвое
уменьшает CPL вдвое И одновременно уменьшает нужный объём отправок вдвое,
то есть аккаунты живут дольше. Поэтому приоритет — качество текста
и качество списка, а не наращивание темпа.

Решение «покупать ли ещё аккаунт» — обычная маржинальная проверка:
покупаем, пока ожидаемая выручка с аккаунта больше его цены.
"""

from data import analytics as an
from optimizer import health

# Если история пустая — консервативная априорная оценка,
# сколько сообщений успевает отправить аккаунт до выбытия.
PRIOR_LIFETIME_SENDS = 300
# Априорная конверсия ответа в лид, пока CRM не прислала реальные исходы.
PRIOR_LEAD_PER_REPLY = 0.30


def observed_lifetime_sends() -> tuple[float, int]:
    """
    Средний «пробег» выбывшего аккаунта. Возвращает (оценка, сколько наблюдений).
    Пока нет ни одного выбывшего — отдаём априорную оценку.
    """
    dead = [a for a in an.list_accounts() if a["status"] == "dead"]
    runs = [an.account_lifetime_sends(a["name"]) for a in dead]
    runs = [r for r in runs if r > 0]
    if not runs:
        return float(PRIOR_LIFETIME_SENDS), 0
    return sum(runs) / len(runs), len(runs)


def avg_account_cost() -> float:
    accs = [a for a in an.list_accounts() if (a["cost"] or 0) > 0]
    if not accs:
        return 0.0
    return sum(a["cost"] for a in accs) / len(accs)


def cost_per_message() -> dict:
    """Амортизированная стоимость одного отправленного сообщения."""
    lifetime, observations = observed_lifetime_sends()
    cost = avg_account_cost()
    return {
        "account_cost": round(cost, 2),
        "expected_lifetime_sends": round(lifetime, 1),
        "observations": observations,
        "cost_per_message": round(cost / lifetime, 4) if lifetime else 0.0,
        "is_estimate": observations == 0,
    }


def rates(days: float = 30, niche: str | None = None) -> dict:
    """Фактические конверсии из данных."""
    f = an.funnel(days=days, niche=niche)
    decided = f["replied"] + f["no_reply"]
    reply_rate = f["replied"] / decided if decided else 0.0

    # lead_rate считаем только если CRM реально проставляет outcome='lead'
    lead_per_reply = (f["leads"] / f["replied"]) if f["replied"] else None
    return {
        "sent": f["sent"],
        "decided": decided,
        "pending": f["pending"],
        "replied": f["replied"],
        "leads": f["leads"],
        "reply_rate": round(reply_rate, 4),
        "lead_per_reply": round(lead_per_reply, 4) if lead_per_reply is not None else None,
        "lead_per_reply_used": round(lead_per_reply if lead_per_reply is not None
                                     else PRIOR_LEAD_PER_REPLY, 4),
    }


def cpl(days: float = 30, niche: str | None = None) -> dict:
    """Цена лида: фактическая (если лиды размечены) и модельная."""
    cpm = cost_per_message()
    r = rates(days=days, niche=niche)

    spend_on_sent = cpm["cost_per_message"] * r["sent"]
    actual = (spend_on_sent / r["leads"]) if r["leads"] else None

    p_lead = r["reply_rate"] * r["lead_per_reply_used"]
    modeled = (cpm["cost_per_message"] / p_lead) if p_lead else None

    return {
        "cost_per_message": cpm["cost_per_message"],
        "sent": r["sent"],
        "leads": r["leads"],
        "reply_rate": r["reply_rate"],
        "lead_per_reply_used": r["lead_per_reply_used"],
        "p_lead_per_message": round(p_lead, 5),
        "spend_estimated": round(spend_on_sent, 2),
        "cpl_actual": round(actual, 2) if actual else None,
        "cpl_modeled": round(modeled, 2) if modeled else None,
        "note": None if r["leads"] else
                "лиды не размечены — CPL модельный; шлите исходы в POST /analytics/outcome",
    }


def cpl_by_niche(days: float = 30) -> list[dict]:
    out = []
    for niche in an.list_niches():
        c = cpl(days=days, niche=niche)
        c["niche"] = niche
        out.append(c)
    return sorted(out, key=lambda x: (x["cpl_modeled"] is None, x["cpl_modeled"] or 0))


def should_buy_account(value_per_lead: float, price: float | None = None,
                       niche: str | None = None) -> dict:
    """
    Маржинальное решение о покупке аккаунта.

    Покупаем, если:  ожидаемые_лиды_с_аккаунта × ценность_лида > цена_аккаунта

        ожидаемые_лиды = ожидаемый_пробег × P(лид с сообщения)
    """
    cpm = cost_per_message()
    r = rates(niche=niche)
    price = price if price is not None else (cpm["account_cost"] or 0.0)

    p_lead = r["reply_rate"] * r["lead_per_reply_used"]
    expected_leads = cpm["expected_lifetime_sends"] * p_lead
    expected_revenue = expected_leads * value_per_lead
    margin = expected_revenue - price

    return {
        "price": round(price, 2),
        "expected_lifetime_sends": cpm["expected_lifetime_sends"],
        "p_lead_per_message": round(p_lead, 5),
        "expected_leads": round(expected_leads, 2),
        "expected_revenue": round(expected_revenue, 2),
        "margin": round(margin, 2),
        "decision": "buy" if margin > 0 else "hold",
        "breakeven_price": round(expected_revenue, 2),
        "confidence": "low" if cpm["is_estimate"] or r["decided"] < 50 else "ok",
        "why": (f"аккаунт за {price:.0f} окупается, если принесёт лидов "
                f"на {price:.0f}; по текущим данным ожидается "
                f"{expected_leads:.2f} лида × {value_per_lead:.0f} = {expected_revenue:.0f}"),
    }


def forecast(targets_remaining: int, niche: str | None = None) -> dict:
    """Сколько дней и денег нужно, чтобы обработать оставшийся список."""
    capacity = health.total_capacity_today(real=True)
    active = [a for a in health.report_real() if a["status"] == "active"]
    daily = sum(a["daily_cap"] for a in active)

    cpm = cost_per_message()
    r = rates(niche=niche)
    p_lead = r["reply_rate"] * r["lead_per_reply_used"]

    days = (targets_remaining / daily) if daily else None
    return {
        "targets_remaining": targets_remaining,
        "active_accounts": len(active),
        "capacity_today": capacity,
        "daily_throughput": daily,
        "days_to_finish": round(days, 1) if days else None,
        "expected_leads": round(targets_remaining * p_lead, 1),
        "estimated_cost": round(targets_remaining * cpm["cost_per_message"], 2),
        "bottleneck": ("нет активных аккаунтов" if not active else
                       "ёмкость пула" if days and days > 14 else "ок"),
    }
