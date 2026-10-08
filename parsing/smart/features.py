"""Признаки сообщения по правилам: без ИИ, мгновенно и бесплатно."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import textproc as tp

# Веса оценки намерения по правилам. Подбираются по разметке (см. evaluate.py).
W = {
    "base": 0.10,
    "intent": 0.40,        # есть сильный маркер запроса
    "intent_weak": 0.20,   # есть только слабые маркеры («нужно», «кто знает»)
    "intent_more": 0.05,   # два и больше маркеров
    "question": 0.15,
    "specific": 0.10,      # × конкретика 0..1
    "invite_buyer": 0.05,  # запрос + «пишите в лс»: ждёт предложений
    "invite_seller": -0.30,  # «пишите в лс» без запроса: реклама
    "seller": -0.45,
    "links": -0.15,
    "mentions": -0.10,
    "phones": -0.10,
    "emoji": -0.15,
    "caps": -0.10,
    "ad_dup": -0.40,       # одинаковый рекламный текст в нескольких чатах
    "long": -0.10,
}

MIN_LEN = 15
MAX_LEN = 1500


@dataclass
class Features:
    lemmas: list = field(default_factory=list)
    intent_hits: list = field(default_factory=list)
    intent_strong: bool = False
    seller_hits: list = field(default_factory=list)
    invite_hits: list = field(default_factory=list)
    opt_out_hits: list = field(default_factory=list)
    question: bool = False
    n_links: int = 0
    n_mentions: int = 0
    n_phones: int = 0
    emoji: float = 0.0
    caps: float = 0.0
    length: int = 0
    specificity: float = 0.0
    fp: str = ""
    dup_sources: int = 1           # в скольких источниках автор писал этот же текст
    ad_dup: bool = False           # дубль, похожий на рекламу
    intent_rule: float = 0.0


def specificity(text: str) -> float:
    s = 0.0
    if tp.MONEY_RE.search(text):
        s += 0.4
    if tp.DEADLINE_RE.search(text):
        s += 0.3
    if any(ch.isdigit() for ch in text):
        s += 0.15
    if len(text) >= 120:
        s += 0.15
    return min(1.0, s)


def extract(text: str, lex: tp.Lexicon) -> Features:
    lem = tp.lemmas(text)
    intent_hits = lex.intent.find(lem)
    seller_hits = lex.seller.find(lem)
    # «ищу клиентов» — это продавец: снимаем маркеры запроса, вложенные в продавцовые фразы
    if seller_hits:
        seller_lem = {w for h in seller_hits for w in tp.lemmas(h)}
        intent_hits = [h for h in intent_hits if not set(tp.lemmas(h)) <= seller_lem]
    # «скиньте прайс» — это покупатель: снимаем продавцовые маркеры, вложенные в фразы запроса
    if intent_hits and seller_hits:
        intent_sets = [set(tp.lemmas(h)) for h in intent_hits if len(tp.lemmas(h)) > 1]
        seller_hits = [h for h in seller_hits if not any(set(tp.lemmas(h)) < st for st in intent_sets)]
    return Features(
        lemmas=lem,
        intent_hits=intent_hits,
        intent_strong=any(h not in lex.intent.weak for h in intent_hits),
        seller_hits=seller_hits,
        invite_hits=lex.invite.find(lem),
        opt_out_hits=lex.opt_out.find(lem),
        question="?" in text,
        n_links=len(tp.URL_RE.findall(text)),
        n_mentions=len(tp.MENTION_RE.findall(text)),
        n_phones=len(tp.PHONE_RE.findall(text)),
        emoji=tp.emoji_ratio(text, len(lem)),
        caps=tp.caps_ratio(text),
        length=len(text),
        specificity=specificity(text),
        fp=tp.fingerprint(text),
    )


def intent_rule(f: Features) -> float:
    s = W["base"]
    if f.intent_hits:
        s += W["intent"] if f.intent_strong else W["intent_weak"]
        s += W["intent_more"] if len(f.intent_hits) > 1 else 0.0
    if f.question:
        s += W["question"]
    s += W["specific"] * f.specificity
    if f.invite_hits:
        s += W["invite_buyer"] if f.intent_hits else W["invite_seller"]
    if f.seller_hits:
        s += W["seller"]
    if f.n_links:
        s += W["links"]
    if f.n_mentions:
        s += W["mentions"]
    if f.n_phones:
        s += W["phones"]
    if f.emoji > 0.08:
        s += W["emoji"]
    if f.caps > 0.5:
        s += W["caps"]
    if f.ad_dup:
        s += W["ad_dup"]
    if f.length > 700:
        s += W["long"]
    return max(0.0, min(1.0, s))


def hard_reject(msg, author, f: Features, own_ids: set) -> str:
    """Причина безусловного отсева или пустая строка."""
    if msg.sender_kind != "user" or not msg.sender_id:
        return "пишет не человек (канал или анонимный админ)"
    if msg.sender_id in own_ids:
        return "наш аккаунт"
    if author is not None and author.is_bot:
        return "бот"
    if author is not None and author.is_deleted:
        return "удалённый аккаунт"
    if msg.is_fwd:
        return "пересланное сообщение"
    if f.length < MIN_LEN:
        return "слишком короткое"
    if f.length > MAX_LEN:
        return "слишком длинное (обычно реклама)"
    return ""


def looks_like_ad(f: Features) -> bool:
    return bool(f.seller_hits or f.n_links or f.n_phones or f.n_mentions
                or (f.invite_hits and not f.intent_hits)
                or f.length > 300 or f.emoji > 0.08)


def is_ad(f: Features) -> bool:
    """Сообщение похоже на рекламу исполнителя (для доли рекламы в источнике)."""
    return bool(f.ad_dup or (f.seller_hits and not f.intent_hits)
                or (f.invite_hits and not f.intent_hits))


def mark_duplicates(cands) -> None:
    """Один автор, один и тот же текст в нескольких чатах.

    Если в тексте есть признаки рекламы, это рассылка исполнителя (штраф).
    Если это чистый запрос, человек продублировал свою потребность (не штраф).
    """
    exact_sources: dict[tuple, set] = defaultdict(set)
    exact_count: Counter = Counter()
    for c in cands:
        if c.f.fp:
            key = (c.msg.sender_id, c.f.fp)
            exact_sources[key].add(c.msg.source_id)
            exact_count[key] += 1

    # почти-дубли: SimHash среди сообщений одного автора
    per_sender: dict[int, list] = defaultdict(list)
    for c in cands:
        if c.f.length >= 60:
            per_sender[c.msg.sender_id].append(c)
    near_sources: dict[int, set] = {}
    for lst in per_sender.values():
        if len(lst) < 2 or len(lst) > 150:
            continue
        hashes = [tp.simhash(c.f.lemmas) for c in lst]
        for i, c in enumerate(lst):
            group = {c.msg.source_id}
            for j, other in enumerate(lst):
                if i != j and tp.hamming(hashes[i], hashes[j]) <= 6:
                    group.add(other.msg.source_id)
            near_sources[id(c)] = group

    for c in cands:
        n = 1
        if c.f.fp:
            key = (c.msg.sender_id, c.f.fp)
            n = max(n, len(exact_sources[key]))
            if exact_count[key] >= 3:
                n = max(n, 2)
        n = max(n, len(near_sources.get(id(c), ())))
        c.f.dup_sources = n
        c.f.ad_dup = n >= 2 and looks_like_ad(c.f)


def intent_type_rule(f: Features, lex: tp.Lexicon) -> str:
    hit_lem = {w for h in f.intent_hits for w in tp.lemmas(h)}
    if hit_lem & lex.vendor_lemmas:
        return "vendor"
    if hit_lem & lex.advice_lemmas:
        return "advice"
    if f.question:
        return "advice"
    return "none"
