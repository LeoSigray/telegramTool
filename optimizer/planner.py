"""
optimizer/planner.py — сборка дневного плана.

Порядок действий перед запуском рассылки:

  1. Очистка списка   — убрать дубли, стоп-лист и тех, кому уже писали.
                        Это самая дешёвая оптимизация: каждое повторное
                        сообщение — потраченные деньги с нулевым шансом
                        и прямой риск жалобы.
  2. Ёмкость          — сколько сегодня реально можно отправить (health).
  3. Размер волны     — не вываливать весь список сразу: первая волна
                        набирает статистику по текстам, следующие уже
                        идут победившим вариантом.
  4. Распределение    — раскидать цели по аккаунтам в пределах их лимитов.
"""

from data import analytics as an
from optimizer import bandit, economics, health


def clean_targets(raw: list[str]) -> dict:
    """
    Фильтрация списка. Возвращает {targets, removed: {...}} — что и почему выкинули.
    """
    seen: set[str] = set()
    deduped: list[str] = []
    dup = 0
    for t in raw:
        s = (t or "").strip()
        if not s:
            continue
        key = s.lower().lstrip("@")
        if key in seen:
            dup += 1
            continue
        seen.add(key)
        deduped.append(s)

    contacted = an.already_contacted(deduped)

    kept, suppressed, repeat = [], 0, 0
    for t in deduped:
        key = t.lower().lstrip("@")
        if an.is_suppressed(key):
            suppressed += 1
            continue
        if key in contacted:
            repeat += 1
            continue
        kept.append(t)

    return {
        "targets": kept,
        "input": len(raw),
        "removed": {
            "duplicates": dup,
            "suppressed": suppressed,
            "already_contacted": repeat,
        },
        "kept": len(kept),
    }


def wave_size(niche: str, available: int) -> dict:
    """
    Сколько отправить в ближайшей волне.

    Пока варианты текста не набрали статистику — ограничиваем волну так,
    чтобы не сжечь весь список на непроверенных текстах. Как только
    статистика есть — ограничение снимается, работает только ёмкость пула.
    """
    arms = [a for a in an.template_stats(niche=niche) if a["active"]]
    if not arms:
        return {"size": available, "mode": "no_templates", "reason": "нет шаблонов для ниши"}

    under = [a for a in arms if (a["decided"] or 0) < bandit.MIN_SAMPLES]
    if under and len(arms) > 1:
        need = sum(bandit.MIN_SAMPLES - (a["decided"] or 0) for a in under)
        size = min(available, max(need, len(arms) * 10))
        return {
            "size": size,
            "mode": "learning",
            "reason": f"{len(under)} из {len(arms)} вариантов ещё без статистики",
            "samples_needed": need,
        }

    return {"size": available, "mode": "exploit",
            "reason": "статистика собрана, шлём победителем"}


def capacity() -> dict:
    """Ёмкость на сегодня — только по РЕАЛЬНЫМ аккаунтам (см. health.report_real:
    account_meta вперемешку хранит демо-данные для дашбордов и купленные-но-
    неавторизованные аккаунты — план рассылки должен видеть только тех,
    кто реально может отправить сообщение)."""
    rows = health.report_real()
    accounts = [a for a in rows if a["status"] == "active"]
    return {
        "accounts_active": len(accounts),
        "accounts_resting": len([a for a in rows if a["status"] == "resting"]),
        "accounts_dead": len([a for a in rows if a["status"] == "dead"]),
        "remaining_today": sum(a["remaining_today"] for a in accounts),
        "per_account": {a["account"]: a["remaining_today"] for a in accounts},
    }


def build_plan(raw_targets: list[str], niche: str | None = None,
               value_per_lead: float | None = None) -> dict:
    """
    Полный план: что отправить сегодня, каким текстом, с каких аккаунтов,
    сколько это будет стоить и что делать с бюджетом.
    """
    cleaned = clean_targets(raw_targets)
    cap = capacity()
    available = min(cleaned["kept"], cap["remaining_today"])

    wave = wave_size(niche, available) if niche else {
        "size": available, "mode": "no_niche", "reason": "ниша не указана"}
    size = min(wave["size"], available)

    # раскидываем волну по аккаунтам пропорционально их свободной ёмкости
    allocation: dict[str, int] = {}
    left = size
    for acc, free in sorted(cap["per_account"].items(), key=lambda kv: -kv[1]):
        if left <= 0:
            break
        take = min(free, left)
        if take > 0:
            allocation[acc] = take
            left -= take

    plan = {
        "niche": niche,
        "targets": cleaned,
        "capacity": cap,
        "wave": {**wave, "size": size},
        "allocation": allocation,
        "backlog": cleaned["kept"] - size,
        "templates": bandit.posterior(niche) if niche else [],
        "economics": economics.cpl(niche=niche),
        "forecast": economics.forecast(cleaned["kept"], niche=niche),
        "warnings": [],
    }

    if not cap["accounts_active"]:
        plan["warnings"].append("нет активных аккаунтов — все отдыхают или выбыли")
    if niche and not plan["templates"]:
        plan["warnings"].append(f"для ниши '{niche}' не заведено ни одного шаблона")
    if cleaned["removed"]["already_contacted"]:
        plan["warnings"].append(
            f"{cleaned['removed']['already_contacted']} контактов уже получали сообщение — пропущены")
    if cleaned["removed"]["suppressed"]:
        plan["warnings"].append(
            f"{cleaned['removed']['suppressed']} контактов в стоп-листе — пропущены")
    if plan["backlog"] > 0:
        plan["warnings"].append(
            f"{plan['backlog']} контактов не влезли в сегодняшнюю ёмкость — уйдут в следующие дни")

    if value_per_lead:
        plan["buy_decision"] = economics.should_buy_account(value_per_lead, niche=niche)

    return plan
