# -*- coding: utf-8 -*-
"""
Планировщик прогрева — ЧИСТЫЕ функции: на входе время/состояние/генератор
случайных чисел, на выходе список задач. Ни сети, ни БД, ни sleep —
поэтому всё проверяется детерминированно (random.Random(seed)).

Задача — dict: {account, day, kind, due_at, payload, group_key, seq}.
"""
import random
from datetime import datetime, timedelta, timezone

from . import config as cfg

PROFILE, LIFE, DIALOG, DONE = "profile", "life", "dialog", "done"


# ── время и фазы ────────────────────────────────────────────────────────────

def _local(dt: datetime, tz_h: float) -> datetime:
    return dt.astimezone(timezone.utc) + timedelta(hours=tz_h)


def day_index(started_at: datetime, now: datetime, tz_h: float) -> int:
    """
    Номер дня прогрева по ЛОКАЛЬНОМУ календарю аккаунта (не «24 часа с момента
    старта»): тогда все, кого запустили в один день, живут в одних и тех же
    календарных сутках, и окна «бодрствования» совпадают по дням.
    """
    return (_local(now, tz_h).date() - _local(started_at, tz_h).date()).days


def day0_counts(started_at: datetime, tz_h: float) -> bool:
    """Входит ли день запуска в счёт WARMUP_DAYS (или от него остался лишь огрызок)."""
    return remaining_fraction(started_at, 0, tz_h, started_at) >= cfg.DAY0_MIN_FRACTION


def first_day(started_at: datetime, tz_h: float) -> int:
    """Индекс первого учитываемого дня: 0 (день запуска) или 1 (запуск был в конце дня)."""
    return 0 if day0_counts(started_at, tz_h) else 1


def last_day(first: int = 0) -> int:
    return first + cfg.WARMUP_DAYS - 1


def counted_day(day: int, first: int = 0) -> int:
    """Номер дня прогрева для людей: 1..WARMUP_DAYS (0 — ещё не начался/«огрызок»)."""
    return max(0, min(day - first + 1, cfg.WARMUP_DAYS))


def phase_for_day(day: int, dialogs: bool = True, first: int = 0) -> str:
    """
    «Жизнь» и диалоги идут в одни и те же дни (см. dialog_active). PROFILE — только
    для «огрызка» дня запуска, который в счёт не входит (там делается один профиль).
    """
    if day < first:
        return PROFILE
    if day <= last_day(first):
        return LIFE
    return DONE


def dialog_active(day: int, dialogs: bool = True, first: int = 0) -> bool:
    """Идут ли в этот день контакты/переписка между своими аккаунтами."""
    return bool(dialogs) and first <= day <= last_day(first)


def total_days(dialogs: bool = True) -> int:
    return cfg.WARMUP_DAYS


def window_for_day(started_at: datetime, day: int, tz_h: float,
                   now: datetime | None = None) -> tuple[datetime, datetime]:
    """
    Окно бодрствования дня (UTC). Если окно уже частично прошло (рантайм
    запустили вечером) — начало сдвигается на «сейчас»; если осталось меньше
    часа — берём ближайшие ~3 часа, чтобы день не остался без действий.
    """
    # _local() даёт «сдвинутое» время с tzinfo=UTC; полночь по нему минус сдвиг
    # = момент локальной полуночи старта в настоящем UTC.
    local_midnight = _local(started_at, tz_h).replace(hour=0, minute=0, second=0, microsecond=0)
    base_utc = local_midnight - timedelta(hours=tz_h)
    start = base_utc + timedelta(days=day, hours=cfg.DAY_WINDOW_H[0])
    end = base_utc + timedelta(days=day, hours=cfg.DAY_WINDOW_H[1])
    if now is not None and start < now:
        start = now
        if end - start < timedelta(hours=1):
            end = start + timedelta(hours=3)
    return start, end


def remaining_fraction(started_at: datetime, day: int, tz_h: float, now: datetime) -> float:
    """
    Какая доля окна бодрствования дня ещё впереди (1.0 — день не начинался).
    Если процесс «проснулся» вечером, нельзя втиснуть полную дневную норму в
    оставшиеся пару часов — это пачка действий; норму уменьшаем пропорционально.
    """
    fs, fe = window_for_day(started_at, day, tz_h)
    full = (fe - fs).total_seconds()
    if full <= 0:
        return 1.0
    return max(0.0, min(1.0, (fe - max(now, fs)).total_seconds() / full))


def _scaled(n: int, frac: float, floor: int = 1) -> int:
    return n if frac >= 0.999 else max(floor, round(n * frac))


def _separate(tasks: list[dict], min_gap_s: float) -> list[dict]:
    """Разные действия одного аккаунта не ближе min_gap_s друг к другу (подписка и чтение
    с разницей в 5 секунд — не человек). Сдвигаем более поздние вперёд."""
    out = sorted(tasks, key=lambda x: x["due_at"])
    for prev, cur in zip(out, out[1:]):
        floor = prev["due_at"] + timedelta(seconds=min_gap_s)
        if cur["due_at"] < floor:
            cur["due_at"] = floor
    return out


def spread_times(rng: random.Random, start: datetime, end: datetime, n: int,
                 min_gap_s: float) -> list[datetime]:
    """
    n случайных моментов в [start, end] с зазором не меньше min_gap_s.
    Если окно слишком тесное — равномерная раскладка с лёгким дрожанием
    (лучше слегка сжать зазоры, чем потерять действия).
    """
    if n <= 0:
        return []
    span = (end - start).total_seconds()
    if span <= 0:
        return [start] * n
    for _ in range(200):
        pts = sorted(rng.uniform(0, span) for _ in range(n))
        if all(b - a >= min_gap_s for a, b in zip(pts, pts[1:])):
            return [start + timedelta(seconds=p) for p in pts]
    step = span / (n + 1)
    return [start + timedelta(seconds=step * (i + 1) + rng.uniform(-step * .25, step * .25))
            for i in range(n)]


# ── день 0: профиль ─────────────────────────────────────────────────────────

def plan_profile(account: str, now: datetime, rng: random.Random, set_name: bool) -> list[dict]:
    """
    Аватар / (имя) / био / юзернейм — в случайном порядке, с паузой 5–15 минут
    между действиями (быстрая серия правок профиля — прямой повод для антиспама).
    """
    # Имя всегда раньше био и юзернейма — они строятся ПО имени, иначе юзернейм
    # получился бы от старого имени. Аватар встаёт в случайное место.
    kinds = (["name"] if set_name else []) + ["bio", "username"]
    kinds.insert(rng.randint(0, len(kinds)), "avatar")
    t = now + timedelta(seconds=rng.uniform(*cfg.FIRST_ACTION_DELAY_S))
    out = []
    for i, kind in enumerate(kinds):
        if i:
            t += timedelta(seconds=rng.uniform(*cfg.PROFILE_PAUSE_S))
        out.append({"account": account, "day": 0, "kind": kind, "due_at": t,
                    "payload": {}, "group_key": None, "seq": 0})
    return out


# ── дни «имитации жизни» ────────────────────────────────────────────────────

def plan_life(account: str, day: int, started_at: datetime, tz_h: float,
              now: datetime, rng: random.Random) -> list[dict]:
    """
    3–4 подписки в первой половине дня, 4–6 «пролистываний ленты» дальше,
    и с вероятностью SAVED_PROB — пост в «Избранное» (не каждый день и не со
    всех каналов, как и просит план).
    """
    start, end = window_for_day(started_at, day, tz_h, now)
    span = end - start
    frac = remaining_fraction(started_at, day, tz_h, now)
    n_sub = _scaled(rng.randint(*cfg.SUBS_PER_DAY), frac)
    n_read = _scaled(rng.randint(*cfg.READS_PER_DAY), frac)

    sub_times = spread_times(rng, start, start + span * 0.5, n_sub, 15 * 60)
    # чтение — позже первой подписки, чтобы было что читать
    read_from = (sub_times[0] if sub_times else start) + timedelta(minutes=10)
    read_times = spread_times(rng, min(read_from, end), end, n_read, 40 * 60)

    def mk(kind, t, payload=None):
        return {"account": account, "day": day, "kind": kind, "due_at": t,
                "payload": payload or {}, "group_key": None, "seq": 0}

    tasks = [mk("subscribe", t) for t in sub_times]
    tasks += [mk("read", t) for t in read_times]
    if rng.random() < cfg.SAVED_PROB:
        t = spread_times(rng, start + span * 0.5, end, 1, 0)[0]
        tasks.append(mk("saved", t))
    return _separate(tasks, 4 * 60)


# ── дни взаимных диалогов ───────────────────────────────────────────────────

def _compose(rng: random.Random, total: int, parts: int) -> list[int]:
    """Разбить total на parts положительных слагаемых (случайно)."""
    parts = max(1, min(parts, total))
    cuts = sorted(rng.sample(range(1, total), parts - 1)) if parts > 1 else []
    edges = [0, *cuts, total]
    return [b - a for a, b in zip(edges, edges[1:])]


def replies_for_day(dialog_day: int) -> tuple[int, int]:
    """Реплик в день от каждого: в первые дни переписки — меньше (DIALOG_RAMP), потом норма."""
    return cfg.DIALOG_RAMP.get(dialog_day, cfg.DIALOG_REPLIES_PER_DAY)


def scaled_replies(partners: int, dialog_day: int | None = None) -> tuple[int, int]:
    """
    Диапазон реплик в день ОТ КАЖДОГО для пары, если у кого-то из двоих
    несколько собеседников (нечётная пачка: один аккаунт сидит в двух парах).
    План: 5–7 реплик в день от аккаунта ВСЕГО — значит, у такого аккаунта
    на каждую пару приходится меньше, иначе он нагенерирует вдвое больше нормы.
    """
    lo, hi = replies_for_day(dialog_day) if dialog_day else cfg.DIALOG_REPLIES_PER_DAY
    n = max(1, partners)
    return (max(2, lo // n), max(3, -(-hi // n)))      # -(-x//n) = потолок деления


def plan_dialog_pair(a: str, b: str, day: int, started_at: datetime, tz_h: float,
                     now: datetime, rng: random.Random, first_day: bool,
                     topic: str | None = None,
                     replies_range: tuple[int, int] | None = None,
                     started_b: datetime | None = None,
                     tz_b: float | None = None) -> list[dict]:
    """
    План дня для пары (a, b).

    • первый день: оба добавляют друг друга в контакты, и только спустя
      20–40 минут стартует переписка (общение с контактом для Telegram доверенное);
    • реплик в день ОТ КАЖДОГО — 5–7, то есть всего 10–14 (чётное число);
    • день делится на 2–3 «захода» с зазором ≥ 1.5 ч, внутри захода реплики
      идут в шахматном порядке (a, b, a, b… или b, a, b, a…);
    • группа (group_key) + seq задают порядок: реплика не уйдёт, пока не
      отработана предыдущая (иначе «ответ» может обогнать вопрос).
    """
    topic = topic or rng.choice(cfg.DIALOG_TOPICS)
    start, end = window_for_day(started_at, day, tz_h, now)
    if tz_b is not None:
        # У партнёра свой часовой пояс (±1 ч от базового) — переписка должна
        # попадать в «бодрствование» ОБОИХ, поэтому берём пересечение окон.
        sb, eb = window_for_day(started_b or started_at, day, tz_b, now)
        if min(end, eb) - max(start, sb) >= timedelta(hours=2):
            start, end = max(start, sb), min(end, eb)
    tasks: list[dict] = []
    burst_from = start

    if first_day:
        for acc, other in ((a, b), (b, a)):
            t = start + timedelta(seconds=rng.uniform(0, min(1200, (end - start).total_seconds() * 0.1)))
            tasks.append({"account": acc, "day": day, "kind": "contact_add", "due_at": t,
                          "payload": {"partner": other},
                          "group_key": "contact|" + "|".join(sorted((a, b))), "seq": 0})
        latest = max(t["due_at"] for t in tasks)
        burst_from = latest + timedelta(seconds=rng.uniform(*cfg.CONTACT_ADD_DELAY_S))

    frac = remaining_fraction(started_at, day, tz_h, now)
    replies = _scaled(rng.randint(*(replies_range or cfg.DIALOG_REPLIES_PER_DAY)), frac, 2)   # от КАЖДОГО
    k = rng.randint(*cfg.DIALOG_BURSTS_PER_DAY)
    pairs_per_burst = _compose(rng, replies, k)                  # «круги» a→b на заход
    starts = spread_times(rng, burst_from, max(end, burst_from + timedelta(hours=1)),
                          len(pairs_per_burst), cfg.BURST_MIN_SEPARATION_S)

    key = "|".join(sorted((a, b)))
    for bi, (n_pairs, t0) in enumerate(zip(pairs_per_burst, starts)):
        order = [a, b] if rng.random() < 0.5 else [b, a]
        t = t0
        gk = f"{key}|d{day}|b{bi}"
        for j in range(n_pairs * 2):
            speaker = order[j % 2]
            partner = b if speaker == a else a
            tasks.append({"account": speaker, "day": day, "kind": "dialog_turn", "due_at": t,
                          "payload": {"pair": key, "partner": partner, "topic": topic,
                                      "first": (bi == 0 and j == 0)},
                          "group_key": gk, "seq": j})
            t += timedelta(seconds=rng.uniform(*cfg.TURN_GAP_S))
    return sorted(tasks, key=lambda x: x["due_at"])


def plan_dialog_day_reading(account: str, day: int, started_at: datetime, tz_h: float,
                            now: datetime, rng: random.Random) -> list[dict]:
    """В дни диалогов аккаунт не пропадает из каналов — одна лёгкая сессия чтения."""
    start, end = window_for_day(started_at, day, tz_h, now)
    t = spread_times(rng, start, end, 1, 0)[0]
    return [{"account": account, "day": day, "kind": "read", "due_at": t,
             "payload": {}, "group_key": None, "seq": 0}]


# ── пары ────────────────────────────────────────────────────────────────────

def make_pairs(accounts: list[str], rng: random.Random) -> list[tuple[str, str]]:
    """
    Случайные пары. Нечётный аккаунт не остаётся один: он вливается в
    последнюю пару и общается с её участниками (тройка = две пары).
    Меньше двух аккаунтов — пар нет (диалог невозможен).
    """
    accs = list(accounts)
    if len(accs) < 2:
        return []
    rng.shuffle(accs)
    pairs = [(accs[i], accs[i + 1]) for i in range(0, len(accs) - 1, 2)]
    if len(accs) % 2:
        pairs.append((accs[-1], pairs[-1][0]))
    return pairs


def clamp_to_awake(dt: datetime, tz_h: float, rng: random.Random) -> datetime:
    """
    Если момент dt приходится на ночь по локальному времени аккаунта —
    переносим на ближайшее утро (начало окна + 0–60 мин). Иначе — как есть.
    """
    local = _local(dt, tz_h)
    lo, hi = cfg.DAY_WINDOW_H
    if lo <= local.hour < hi:
        return dt
    morning = local.replace(hour=lo, minute=0, second=0, microsecond=0)
    if local.hour >= hi:
        morning += timedelta(days=1)
    return morning - timedelta(hours=tz_h) + timedelta(seconds=rng.uniform(0, 3600))
