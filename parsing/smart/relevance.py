"""Релевантность сообщения теме клиента.

Основной метод — TF-IDF по леммам и биграммам, на чистом Python, без
зависимостей и затрат. Если включить --embeddings и установить
sentence-transformers, к нему добавляется локальная модель эмбеддингов
(тоже бесплатно, но это тяжёлая зависимость: torch и модель на сотни МБ).
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Optional

from .textproc import lemmas

# Тема R = 0.7 · покрытие терминов клиента + 0.3 · косинус TF-IDF (до COS_REF).
# Покрытие насыщается: R не падает от того, что запрос длинный и подробный.
COVERAGE_K = 4.0      # «полвеса» покрытия: сумма idf совпавших терминов, дающая 0.5
COS_REF = 0.30        # косинус, который уже считаем «полностью про тему»
EMB_FLOOR = 0.20


def _normalize(v: dict) -> dict:
    n = math.sqrt(sum(x * x for x in v.values()))
    return {k: x / n for k, x in v.items()} if n else {}


def cosine(a: dict, b: dict) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(t, 0.0) for t, w in a.items())


def combine(*weighted: tuple) -> dict:
    """Взвешенная сумма нормированных векторов → нормированный вектор."""
    acc: Counter = Counter()
    for vec, w in weighted:
        for t, x in vec.items():
            acc[t] += x * w
    return _normalize(dict(acc))


class TfIdf:
    def __init__(self, docs: list[list[str]], stopwords: set[str]) -> None:
        self.stop = stopwords
        df: Counter = Counter()
        for d in docs:
            df.update(set(self.terms(d)))
        self.n = max(1, len(docs))
        self.idf = {t: math.log((self.n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
        self.default_idf = math.log(self.n + 1) + 1.0

    def terms(self, lem: list[str]) -> list[str]:
        toks = [t for t in lem if len(t) > 2 and t not in self.stop and not t.isdigit()]
        return toks + [a + " " + b for a, b in zip(toks, toks[1:])]

    def vector(self, lem: list[str]) -> dict:
        tf = Counter(self.terms(lem))
        return _normalize({t: (1.0 + math.log(c)) * self.idf.get(t, self.default_idf)
                           for t, c in tf.items()})


def profile_vector(tfidf: TfIdf, profile) -> dict:
    """Вектор темы клиента: термины весят больше постов, оффер и бриф — между ними."""
    parts = [
        (tfidf.vector(lemmas(" ; ".join(profile.topic_terms))), 2.0),
        (tfidf.vector(lemmas(f"{profile.offer} {profile.audience} {profile.brief}")), 1.5),
        (tfidf.vector(lemmas(profile.about)), 1.0),
        (tfidf.vector(lemmas(" ".join(profile.posts_sample))), 1.0),
    ]
    return combine(*[(v, w) for v, w in parts if v])


def core_terms(tfidf: TfIdf, profile) -> set:
    """Ядро темы: термины профиля и оффера (слова и биграммы внутри фраз)."""
    core: set = set()
    for phrase in list(profile.topic_terms) + [profile.offer]:
        core.update(tfidf.terms(lemmas(phrase)))
    return core


def coverage(tfidf: TfIdf, lem: list[str], core: set) -> float:
    """Сколько в сообщении терминов клиента (с весом idf), с насыщением в [0, 1)."""
    hit = set(tfidf.terms(lem)) & core
    ev = sum(tfidf.idf.get(t, tfidf.default_idf) for t in hit)
    return ev / (ev + COVERAGE_K) if ev else 0.0


def topic_score(tfidf: TfIdf, lem: list[str], pvec: dict, core: set) -> float:
    cos = cosine(tfidf.vector(lem), pvec)
    return 0.7 * coverage(tfidf, lem, core) + 0.3 * min(1.0, cos / COS_REF)


def profile_lemmas(profile) -> list[str]:
    return lemmas(" ".join([" ; ".join(profile.topic_terms), profile.offer, profile.audience,
                            profile.brief, profile.about, " ".join(profile.posts_sample)]))


class Embedder:
    """Локальные эмбеддинги (sentence-transformers). Только если явно включены."""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore
        self.model = SentenceTransformer(model_name)

    def encode(self, texts: list[str]):
        return self.model.encode(texts, batch_size=64, normalize_embeddings=True,
                                 show_progress_bar=False)


def try_embedder(model_name: str, log) -> Optional[Embedder]:
    try:
        return Embedder(model_name)
    except Exception as e:  # noqa: BLE001
        log.warn("релевантность", f"эмбеддинги недоступны ({e}); работаем на TF-IDF. "
                                  "Установите: pip install sentence-transformers")
        return None


def normalize_scores(raw: list[float], floor: float) -> list[float]:
    """Сырые близости → R в [0, 1]: ниже порога 0, выше 99-го перцентиля 1."""
    above = sorted(x for x in raw if x >= floor)
    if not above:
        return [0.0] * len(raw)
    hi = above[min(len(above) - 1, int(0.99 * (len(above) - 1)))]
    hi = max(hi, floor * 1.5)
    return [0.0 if x < floor else min(1.0, x / hi) for x in raw]
