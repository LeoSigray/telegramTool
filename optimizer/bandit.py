"""
optimizer/bandit.py — выбор текста сообщения (Thompson Sampling).

Задача: есть N вариантов текста для ниши. Какой слать следующему контакту?
Наивно: разделить 50/50 и через неделю посмотреть. Плохо — половина
бюджета уходит на заведомо слабый вариант всё это время.

Thompson Sampling: для каждого варианта держим Beta(α, β), где
  α = 1 + количество ответов
  β = 1 + количество молчаний
Перед каждой отправкой берём случайную выборку θ из каждого распределения
и шлём тем вариантом, у которого θ максимальна.

Что это даёт: доля трафика на вариант автоматически пропорциональна
вероятности, что он лучший. Слабый вариант отмирает сам за десятки
отправок, а не за неделю. При равных вариантах — трафик делится поровну.

Reward = ответ в течение REPLY_WINDOW_HOURS (data/analytics.py).
Молчание засчитывается как неудача только после истечения окна —
поэтому свежие отправки не портят статистику.
"""

import random

from data import analytics as an

# Минимум решённых (ответ/молчание) исходов на вариант,
# прежде чем разрешаем бандиту его отсеивать.
MIN_SAMPLES = 30

# Порог, ниже которого вариант предлагается отключить (относительно лидера).
PRUNE_RATIO = 0.5


def _arms(niche: str) -> list[dict]:
    stats = an.template_stats(niche=niche)
    return [s for s in stats if s["active"]]


def pick_template(niche: str) -> dict | None:
    """
    Возвращает шаблон для следующей отправки. None — если для ниши нет вариантов.

    Пока какой-то вариант недосэмплирован (< MIN_SAMPLES решённых исходов),
    отдаём предпочтение ему — иначе бандит может закрепиться на варианте,
    которому просто повезло на первых трёх отправках.
    """
    arms = _arms(niche)
    if not arms:
        return None
    if len(arms) == 1:
        return arms[0]

    under = [a for a in arms if (a["decided"] or 0) < MIN_SAMPLES]
    if under:
        # round-robin среди недосэмплированных: берём тот, кому слали меньше всего
        return min(under, key=lambda a: (a["sent"] or 0))

    best, best_theta = None, -1.0
    for a in arms:
        alpha = 1 + (a["replies"] or 0)
        beta = 1 + (a["no_reply"] or 0) + (a["negatives"] or 0)
        theta = random.betavariate(alpha, beta)
        if theta > best_theta:
            best, best_theta = a, theta
    return best


def posterior(niche: str | None = None) -> list[dict]:
    """
    Состояние бандита для графика: среднее и 90% интервал по каждому варианту.
    Интервал считаем сэмплированием (без scipy).
    """
    out = []
    for a in an.template_stats(niche=niche):
        alpha = 1 + (a["replies"] or 0)
        beta = 1 + (a["no_reply"] or 0) + (a["negatives"] or 0)
        samples = sorted(random.betavariate(alpha, beta) for _ in range(2000))
        out.append({
            "template_id": a["id"],
            "niche": a["niche"],
            "variant": a["variant"],
            "text": a["text"],
            "active": bool(a["active"]),
            "sent": a["sent"] or 0,
            "decided": a["decided"] or 0,
            "replies": a["replies"] or 0,
            "leads": a["leads"] or 0,
            "negatives": a["negatives"] or 0,
            "pending": a["pending"] or 0,
            "mean": round(alpha / (alpha + beta), 4),
            "ci_low": round(samples[100], 4),
            "ci_high": round(samples[1900], 4),
            "enough_data": (a["decided"] or 0) >= MIN_SAMPLES,
        })
    return out


def win_probability(niche: str, draws: int = 4000) -> dict[str, float]:
    """P(вариант лучший) — самый честный ответ на вопрос «какой текст победил»."""
    arms = _arms(niche)
    if len(arms) < 2:
        return {a["variant"]: 1.0 for a in arms}

    params = [(a["variant"], 1 + (a["replies"] or 0),
               1 + (a["no_reply"] or 0) + (a["negatives"] or 0)) for a in arms]
    wins = {v: 0 for v, _, _ in params}
    for _ in range(draws):
        best_v, best_t = None, -1.0
        for v, al, be in params:
            t = random.betavariate(al, be)
            if t > best_t:
                best_v, best_t = v, t
        wins[best_v] += 1
    return {v: round(n / draws, 4) for v, n in wins.items()}


def prune_suggestions(niche: str) -> list[dict]:
    """
    Какие варианты предлагается выключить: набрали статистику и стабильно
    хуже лидера. Не выключаем автоматически — решение за человеком.
    """
    arms = [a for a in _arms(niche) if (a["decided"] or 0) >= MIN_SAMPLES]
    if len(arms) < 2:
        return []
    probs = win_probability(niche)
    leader = max(arms, key=lambda a: a["reply_rate"] or 0)
    out = []
    for a in arms:
        if a["id"] == leader["id"]:
            continue
        rate = a["reply_rate"] or 0
        lead_rate = leader["reply_rate"] or 0
        if lead_rate and rate < lead_rate * PRUNE_RATIO and probs.get(a["variant"], 1) < 0.10:
            out.append({
                "template_id": a["id"],
                "variant": a["variant"],
                "reply_rate": rate,
                "leader_variant": leader["variant"],
                "leader_reply_rate": lead_rate,
                "win_probability": probs.get(a["variant"], 0),
                "reason": "стабильно хуже лидера при достаточной выборке",
            })
    return out
