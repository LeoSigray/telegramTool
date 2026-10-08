"""Текст: токены, леммы, словари маркеров, отпечатки для поиска дублей.

Лемматизация идёт через pymorphy3, если он установлен (pip install pymorphy3).
Без него работает упрощённый стеммер: точность ниже, но всё так же бесплатно
и локально.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from functools import lru_cache

LEXICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lexicon")

WORD_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
CYR_RE = re.compile(r"[а-яё]", re.IGNORECASE)
URL_RE = re.compile(r"(?:https?://|www\.)\S+|\b(?:t|telegram)\.me/\S+", re.IGNORECASE)
MENTION_RE = re.compile(r"(?<![\w.])@[A-Za-z][A-Za-z0-9_]{3,31}")
PHONE_RE = re.compile(r"(?:\+7|\b8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}\b")
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿]")
MONEY_RE = re.compile(
    r"\d[\d\s.,]*\s?(?:к\b|k\b|тыс|млн|руб|р\.|₽|\$|usd|eur|€)|\bбюджет|\bдо\s+\d",
    re.IGNORECASE)
DEADLINE_RE = re.compile(
    r"\bсрок|\bсрочно|\bдедлайн|\bк\s+\d{1,2}[./]\d{1,2}|\bна (?:этой|следующей) неделе"
    r"|\bзавтра\b|\bв течение",
    re.IGNORECASE)

# Окончания для запасного стеммера (без pymorphy3). Длинные проверяются первыми.
_SUFFIXES = sorted({
    "иями", "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ешь", "ете", "ите",
    "ишь", "ать", "ять", "ить", "еть", "уть", "ться", "тся", "ась", "ось", "ись",
    "ее", "ие", "ые", "ое", "ей", "ий", "ый", "ой", "ем", "им", "ым", "ом", "их", "ых",
    "ую", "юю", "ая", "яя", "ою", "ею", "ет", "ют", "ут", "ят", "ит", "ам", "ям", "ах",
    "ях", "ов", "ев", "ия", "ья", "ию", "ью", "ии", "ем", "ешь", "ут", "ют",
    "а", "я", "о", "е", "и", "ы", "у", "ю", "ь", "й",
}, key=len, reverse=True)


def _stem(word: str) -> str:
    if CYR_RE.search(word):
        for suf in _SUFFIXES:
            # основа от 2 букв: иначе «ищу» и «ищем» не сводятся к одному «ищ»
            if word.endswith(suf) and len(word) - len(suf) >= 2:
                return word[: -len(suf)]
        return word
    if len(word) > 4 and word.endswith("s"):
        return word[:-1]
    return word


class _Lemmatizer:
    def __init__(self) -> None:
        self._morph = None
        self.backend = "стеммер (pymorphy3 не установлен)"
        self._cache: dict[str, str] = {}
        try:
            import pymorphy3  # type: ignore
            self._morph = pymorphy3.MorphAnalyzer()
            self.backend = "pymorphy3"
        except Exception:  # noqa: BLE001 — нет пакета или словарей, работаем стеммером
            self._morph = None

    def lemma(self, word: str) -> str:
        w = word.lower().replace("ё", "е")
        hit = self._cache.get(w)
        if hit is not None:
            return hit
        res = w
        if self._morph is not None and CYR_RE.search(w):
            try:
                res = self._morph.parse(w)[0].normal_form.replace("ё", "е")
            except Exception:  # noqa: BLE001
                res = _stem(w)
        else:
            res = _stem(w)
        if len(self._cache) < 300_000:
            self._cache[w] = res
        return res


_LEM: _Lemmatizer | None = None


def lemmatizer() -> _Lemmatizer:
    global _LEM
    if _LEM is None:
        _LEM = _Lemmatizer()
    return _LEM


def clean(text: str) -> str:
    """Убирает ссылки и @упоминания: на тему сообщения они не влияют."""
    return MENTION_RE.sub(" ", URL_RE.sub(" ", text or ""))


def words(text: str) -> list[str]:
    return WORD_RE.findall(clean(text).lower())


def lemmas(text: str) -> list[str]:
    lem = lemmatizer()
    return [lem.lemma(w) for w in words(text)]


# ── словари ───────────────────────────────────────────────────────────────

def read_lexicon(name: str) -> list[str]:
    path = os.path.join(LEXICON_DIR, f"{name}.txt")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.split("#", 1)[0].strip()
            if s:
                out.append(s)
    return out


class PhraseMatcher:
    """Ищет фразы словаря в последовательности лемм (подряд идущие слова).

    Фраза с префиксом ~ считается слабой: её можно отличить через .weak.
    """

    def __init__(self, phrases) -> None:
        self._by_first: dict[str, list[tuple[str, tuple]]] = defaultdict(list)
        self.weak: set[str] = set()
        seen: set[tuple] = set()
        self.size = 0
        for raw in phrases:
            if raw.startswith("~"):
                raw = raw[1:].strip()
                self.weak.add(raw)
            lem = tuple(lemmas(raw))
            if not lem or lem in seen:
                continue
            seen.add(lem)
            self._by_first[lem[0]].append((raw, lem))
            self.size += 1

    def find(self, seq: list[str]) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        n = len(seq)
        for i, tok in enumerate(seq):
            for raw, ph in self._by_first.get(tok, ()):
                if raw in seen:
                    continue
                k = len(ph)
                if i + k <= n and tuple(seq[i:i + k]) == ph:
                    found.append(raw)
                    seen.add(raw)
        # длинные совпадения важнее: «ищу клиентов» должно перекрывать «ищу»
        found.sort(key=lambda s: -len(s))
        return found


class Lexicon:
    def __init__(self, extra_seller=()) -> None:
        self.intent = PhraseMatcher(read_lexicon("intent"))
        self.seller = PhraseMatcher(read_lexicon("seller") + list(extra_seller))
        self.invite = PhraseMatcher(read_lexicon("contact_invite"))
        self.closed = PhraseMatcher(read_lexicon("closed"))
        self.competitor = PhraseMatcher(read_lexicon("competitor"))
        self.decision_maker = PhraseMatcher(read_lexicon("decision_maker"))
        self.opt_out = PhraseMatcher(read_lexicon("opt_out"))
        self.junk = PhraseMatcher(read_lexicon("junk_chat"))
        self.stopwords = set(lemmas(" ".join(read_lexicon("stopwords"))))
        # леммы маркеров «ищет исполнителя» и «просит совета» — для типа запроса без LLM
        self.vendor_lemmas = set(lemmas("ищу ищем нужен требуется подрядчик исполнитель "
                                        "заказать специалист компания"))
        self.advice_lemmas = set(lemmas("посоветуйте порекомендуйте подскажите работал "
                                        "пользовался сталкивался знает"))


# ── отпечатки и статистика ────────────────────────────────────────────────

def fingerprint(text: str) -> str:
    """Отпечаток нормализованного текста: одинаковая реклама в разных чатах."""
    t = clean(text).lower().replace("ё", "е")
    t = re.sub(r"[^a-zа-я]+", " ", t).strip()
    if len(t) < 25:
        return ""
    return hashlib.sha1(t[:400].encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=200_000)
def _feature_hash(feature: str) -> int:
    return int.from_bytes(hashlib.md5(feature.encode("utf-8")).digest()[:8], "big")


def simhash(tokens: list[str]) -> int:
    """64-битный SimHash по словам и биграммам: близкие тексты дают близкие хеши."""
    toks = tokens[:120]
    if not toks:
        return 0
    feats = toks + [a + " " + b for a, b in zip(toks, toks[1:])]
    acc = [0] * 64
    for feat in feats:
        h = _feature_hash(feat)
        for i in range(64):
            acc[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i, v in enumerate(acc):
        if v > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def emoji_ratio(text: str, n_words: int) -> float:
    return len(EMOJI_RE.findall(text or "")) / max(1, n_words)


def caps_ratio(text: str) -> float:
    letters = [c for c in (text or "") if c.isalpha()]
    if len(letters) < 20:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)
