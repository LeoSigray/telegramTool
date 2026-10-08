# -*- coding: utf-8 -*-
"""
Лёгкая вариация текста: каждое следующее сообщение с одного аккаунта чуть отличается от прежних.

Меняем только малое:
  • первое слово — приветствие (из группы, к которой оно относится: «Здравствуйте» -> «Добрый день»);
  • 1–2 слова по словарю близких по смыслу (data/message_variants.py).
Всего не больше MAX_CHANGES замен. Смысл и тон не трогаем. Если слов для замены в тексте нет,
текст остаётся как есть (preview() покажет это заранее).

Для каждого аккаунта помним последние HISTORY отправленных вариантов и не повторяем их подряд.
"""
import random
import re
from collections import defaultdict, deque

from data import message_variants as mv

MAX_CHANGES = 3          # приветствие + до двух слов
HISTORY = 5              # сколько последних вариантов аккаунта не повторять

_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY))
_last_greeting: dict[str, str] = {}


def _word_re(phrase: str) -> "re.Pattern":
    return re.compile(r"(?<![\w])" + re.escape(phrase) + r"(?![\w])", re.IGNORECASE)


def _match_case(original: str, replacement: str) -> str:
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    if original[:1].islower():
        return replacement[:1].lower() + replacement[1:]
    return replacement


def _greeting_slot(text: str):
    """(span, оригинал, альтернативы) для приветствия в начале текста или None."""
    for group in (mv.GREETINGS_FORMAL, mv.GREETINGS_INFORMAL):
        for g in sorted(group, key=len, reverse=True):
            m = re.match(r"\s*(" + re.escape(g) + r")(?![\w])", text, re.IGNORECASE)
            if m:
                opts = [x for x in group if x.lower() != m.group(1).lower()]
                return m.span(1), m.group(1), opts
    return None


def _synonym_slots(text: str, taken: list[tuple[int, int]]):
    """Все замены по словарю, не пересекающиеся между собой и с приветствием."""
    found = []
    for phrase in sorted(mv.SYNONYMS, key=len, reverse=True):
        for m in _word_re(phrase).finditer(text):
            span = m.span()
            if any(not (span[1] <= a or span[0] >= b) for a, b in taken):
                continue
            taken.append(span)
            found.append((span, m.group(0), list(mv.SYNONYMS[phrase])))
    return found


def _apply(text: str, repl: list[tuple[tuple[int, int], str]]) -> str:
    for (a, b), new in sorted(repl, key=lambda x: x[0][0], reverse=True):
        text = text[:a] + new + text[b:]
    return text


def vary(text: str, account: str, rng: random.Random | None = None) -> str:
    """Следующий вариант текста для аккаунта (в чём-то отличается от последних отправленных)."""
    rng = rng or random
    taken: list[tuple[int, int]] = []
    greeting = _greeting_slot(text)
    if greeting:
        taken.append(greeting[0])
    others = _synonym_slots(text, taken)
    if not greeting and not others:
        return text

    hist = _history[account]
    last_gr = _last_greeting.get(account)
    candidate = text
    for _ in range(60):
        repl = []
        chosen_greeting = None
        if greeting and greeting[2]:
            # любое приветствие группы (в т.ч. исходное), только не то, с которого начиналось ПРЕДЫДУЩЕЕ сообщение
            pool_g = [greeting[1]] + greeting[2]
            opts = [o for o in pool_g if o.lower() != (last_gr or "").lower()] or pool_g
            chosen_greeting = rng.choice(opts)
            if chosen_greeting.lower() != greeting[1].lower():
                repl.append((greeting[0], _match_case(greeting[1], chosen_greeting)))
        budget = MAX_CHANGES - len(repl)
        if others and budget > 0:
            k = rng.randint(1, min(budget, len(others), 2))
            for span, orig, opts in rng.sample(others, k):
                repl.append((span, _match_case(orig, rng.choice(opts))))
        candidate = _apply(text, repl)
        if candidate not in hist:
            break
    hist.append(candidate)
    if greeting:
        _last_greeting[account] = chosen_greeting or greeting[1]
    return candidate


def preview(text: str, n: int = 4) -> list[str]:
    """n примеров вариаций (без записи в историю реальных аккаунтов)."""
    key = "__preview__"
    _history.pop(key, None)
    _last_greeting.pop(key, None)
    out = [vary(text, key) for _ in range(n)]
    _history.pop(key, None)
    _last_greeting.pop(key, None)
    return out


def can_vary(text: str) -> bool:
    """Есть ли в тексте что менять."""
    taken: list[tuple[int, int]] = []
    g = _greeting_slot(text)
    if g:
        taken.append(g[0])
    return bool(g or _synonym_slots(text, taken))


def reset(account: str | None = None) -> None:
    if account is None:
        _history.clear()
        _last_greeting.clear()
    else:
        _history.pop(account, None)
        _last_greeting.pop(account, None)
