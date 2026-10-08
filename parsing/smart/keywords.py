"""Ключевые слова, привязанные к реальности, а не к фантазии нейросети.

1. Язык аудитории. Частые слова из комментариев под каналом клиента: так пишут его настоящие
   покупатели. Нейросеть получает их как образец, когда придумывает слова товара и фразы.
2. Проверка в Telegram. Каждое слово товара и каждую фразу покупателя ищем глобальным поиском
   по сообщениям публичных чатов. Слово, которого в живых чатах нет («нейрокурсы»,
   «AI-онбординг»), выбрасываем; остальные сортируем по частоте. Проверка делается один раз
   на профиль (keywords_checked), её результат виден в профиле (keyword_hits) и в логе.
Совпадение слова товара с сообщением — по целой фразе в леммах (PhraseMatcher), а не по
обрывкам: «ai» из «AI-практикумы» больше не совпадает с любым постом про ИИ.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from . import textproc as tp


def product_matcher(profile):
    """Поиск слов товара в леммах сообщения. None — если слов товара нет."""
    from .profile import clean_product_keywords
    words = clean_product_keywords(getattr(profile, "product_keywords", []) or [])
    return tp.PhraseMatcher(words) if words else None


async def audience_vocabulary(client, params, lex, log, limit: int = 400) -> list:
    """Частые слова из последних комментариев в обсуждении канала клиента."""
    from .audience import _resolve
    from .profile import _GENERIC_KW, top_terms

    try:
        _, group = await _resolve(client, params, log)
    except Exception as e:  # noqa: BLE001
        log.info("ключевые слова", f"обсуждение канала клиента недоступно ({str(e)[:80]})")
        return []
    if group is None:
        log.info("ключевые слова", "у канала клиента нет обсуждения — язык аудитории не взять")
        return []
    texts: list = []
    try:
        async for m in client.iter_messages(group, limit=limit, wait_time=params.wait_time):
            if getattr(m, "message", None):
                texts.append(m.message)
    except Exception as e:  # noqa: BLE001
        log.info("ключевые слова", f"комментарии клиента не прочитать ({str(e)[:80]})")
    vocab = top_terms(texts, set(lex.stopwords) | _GENERIC_KW, limit=40) if texts else []
    if vocab:
        log.info("ключевые слова", f"язык аудитории клиента ({len(texts)} комментариев): "
                                   + ", ".join(vocab[:15]))
    return vocab


async def count_hits(client, term: str, params) -> int:
    """Сколько сообщений с этим словом/фразой есть в публичных чатах (не в чатах аккаунта),
    за последние validate_days дней."""
    from telethon.tl.types import Channel

    cutoff = datetime.now(timezone.utc) - timedelta(days=params.validate_days)
    hits = 0
    async for m in client.iter_messages(None, search=term, limit=params.validate_limit):
        ch = getattr(m, "chat", None)
        if not isinstance(ch, Channel) or not getattr(ch, "left", True):
            continue
        if not (getattr(ch, "username", None) or getattr(ch, "usernames", None)):
            continue
        if m.date and m.date < cutoff:
            continue
        hits += 1
    return hits


async def _check(client, terms: list, params, log, what: str) -> dict:
    from telethon.errors import FloodWaitError

    out: dict = {}
    for t in terms:
        try:
            out[t] = await count_hits(client, t, params)
        except FloodWaitError as e:
            if e.seconds > params.max_flood_wait:
                log.warn("ключевые слова", f"FloodWait {e.seconds} с — проверка {what} остановлена")
                break
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:  # noqa: BLE001
            log.info("ключевые слова", f"«{t}»: поиск не удался ({str(e)[:60]})")
        await asyncio.sleep(random.uniform(1.0, 2.0))
    return out


def _keep(terms: list, hits: dict, min_keep: int) -> tuple[list, list]:
    found = sorted((t for t in terms if hits.get(t, 0) > 0), key=lambda t: -hits[t])
    dropped = [t for t in terms if t in hits and hits[t] == 0]
    if len(found) < min_keep:            # почти ничего не нашлось — не обнуляем список целиком
        found += [t for t in terms if t not in found][:min_keep - len(found)]
    return found, dropped


async def ground_profile(client, profile, params, log) -> bool:
    """Проверка слов товара и фраз покупателя в Telegram. True — профиль изменился."""
    if profile.keywords_checked or client is None:
        return False
    kws = list(profile.product_keywords[:20])
    phrases = list(profile.buyer_phrases[:params.validate_phrases])
    if not kws and not phrases:
        return False
    log.info("ключевые слова", f"проверяю в Telegram {len(kws)} слов товара и {len(phrases)} фраз "
                               "покупателя (один раз на профиль, ~1–2 мин)")
    hits = await _check(client, kws, params, log, "слов товара")
    hits.update(await _check(client, phrases, params, log, "фраз"))
    kept_kw, drop_kw = _keep(kws, hits, 3)
    kept_ph, drop_ph = _keep(phrases, hits, 5)
    profile.product_keywords = kept_kw + [k for k in profile.product_keywords[20:] if k not in kept_kw]
    rest = [p for p in profile.buyer_phrases[params.validate_phrases:] if p not in kept_ph]
    profile.buyer_phrases = kept_ph + rest
    profile.keyword_hits = {t: hits[t] for t in hits}
    profile.keywords_checked = True

    def fmt(ts):
        return ", ".join(f"{t} ({hits.get(t, 0)})" for t in ts[:8])
    log.info("ключевые слова", f"слова товара в живых чатах: {fmt(kept_kw)}"
                               + (f"; выброшены (0 совпадений): {', '.join(drop_kw[:8])}" if drop_kw else ""))
    log.info("ключевые слова", f"фразы покупателя: нашлось {sum(1 for p in phrases if hits.get(p, 0))} "
                               f"из {len(phrases)}; лучшие: {fmt(kept_ph)}")
    log.step("Слова товара после проверки", len(kept_kw))
    return True


def harvest_terms(profile, params) -> list:
    """По чему искать внутри чата при точечном чтении: лучшие слова товара и фразы покупателя.
    Пустой список — читаем чат целиком (как раньше)."""
    if not params.targeted:
        return []
    from .profile import clean_product_keywords
    kws = clean_product_keywords(profile.product_keywords)[:params.targeted_terms]
    hits = getattr(profile, "keyword_hits", {}) or {}
    phrases = [p for p in profile.buyer_phrases if not hits or hits.get(p, 0) > 0][:params.targeted_terms]
    return kws + [p for p in phrases if p.lower() not in kws]
