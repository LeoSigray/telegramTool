"""Поиск источников (чаты и каналы с комментариями) и их предфильтр.

Откуда берём кандидатов:
  1. ручной список (--sources-file);
  2. затравки: ссылки и @упоминания из постов клиента;
  3. свои диалоги читающего аккаунта (группы, где он уже состоит);
  4. поиск Telegram по названиям (contacts.search) по поисковым запросам профиля;
  5. рекомендации похожих каналов к каналу клиента (если API их отдаёт);
  6. поиск фраз покупателя по сообщениям. Telegram ищет только в чатах, где
     аккаунт уже состоит, поэтому это сигнал о намерениях внутри своих диалогов.

Всё только чтение: в чаты не вступаем. Запросы идут с паузами, FloodWait выдерживаем.
"""
from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timedelta, timezone

from . import textproc as tp
from .models import Source
from .relevance import TfIdf, cosine, profile_lemmas, profile_vector


async def _pause(a: float = 1.0, b: float = 2.5) -> None:
    await asyncio.sleep(random.uniform(a, b))


def source_from_entity(ent) -> Source | None:
    from telethon.tl.types import Channel

    if not isinstance(ent, Channel):
        return None
    if getattr(ent, "megagroup", False) or getattr(ent, "gigagroup", False):
        kind = "group"
    elif getattr(ent, "broadcast", False):
        kind = "channel"
    else:
        return None
    username = ent.username or ""
    if not username:
        for u in getattr(ent, "usernames", None) or []:
            if getattr(u, "active", False):
                username = u.username
                break
    return Source(chat_id=ent.id, title=ent.title or "", username=username, kind=kind,
                  members=getattr(ent, "participants_count", 0) or 0,
                  is_member=not getattr(ent, "left", True))


class _Flood(Exception):
    pass


async def _call(coro_factory, params, log, what: str):
    """Один запрос к Telegram с обработкой FloodWait."""
    from telethon.errors import FloodWaitError

    try:
        return await coro_factory()
    except FloodWaitError as e:
        if e.seconds <= params.max_flood_wait:
            log.warn("источники", f"FloodWait {e.seconds} с на «{what}» — ждём и идём дальше")
            await asyncio.sleep(e.seconds + 1)
            return None
        log.error("источники", f"FloodWait {e.seconds} с на «{what}» — этап поиска остановлен")
        raise _Flood() from e


async def _safe(coro_factory, params, log, what: str):
    """Как _call, но прочие ошибки (приватный чат, неверная ссылка) не роняют поиск."""
    try:
        return await _call(coro_factory, params, log, what)
    except _Flood:
        raise
    except Exception as e:  # noqa: BLE001
        log.info("источники", f"пропуск «{what}»: {str(e)[:120]}")
        return None


def _size_score(members: int) -> float:
    return 0.0 if members <= 0 else min(1.0, math.log10(max(members, 1)) / 5.0)


async def discover(client, profile, params, store, lex, log, own_ids: set):
    """Возвращает (выбранные [(Source, entity)], все кандидаты для отчёта)."""
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.contacts import SearchRequest
    from telethon.tl.types import Channel

    cands: dict[int, tuple[Source, object]] = {}
    manual: set[int] = set()
    exclude: set[int] = set(own_ids)

    # канал клиента и его обсуждение — это его же аудитория, по умолчанию не берём
    own_ent = None
    try:
        own_ent = await client.get_entity(params.channel)
        own_full = await client(GetFullChannelRequest(own_ent))
        if not params.include_own:
            exclude.add(own_ent.id)
            if getattr(own_full.full_chat, "linked_chat_id", None):
                exclude.add(own_full.full_chat.linked_chat_id)
    except Exception as e:  # noqa: BLE001
        log.warn("источники", f"не удалось прочитать канал клиента: {str(e)[:120]}")

    def add(ent, via: str) -> Source | None:
        s = source_from_entity(ent)
        if s is None or s.chat_id in exclude:
            return None
        if s.chat_id in cands:
            cands[s.chat_id][0].add_via(via)
            cands[s.chat_id][0].is_member |= s.is_member
            return cands[s.chat_id][0]
        s.add_via(via)
        cands[s.chat_id] = (s, ent)
        return s

    try:
        # 1. ручной список
        if params.sources_file:
            try:
                with open(params.sources_file, "r", encoding="utf-8") as f:
                    links = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
            except OSError as e:
                links = []
                log.warn("источники", f"не прочитать {params.sources_file}: {e}")
            for link in links:
                ent = await _safe(lambda: client.get_entity(link), params, log, link)
                s = add(ent, "вручную") if ent is not None else None
                if s:
                    manual.add(s.chat_id)
                await _pause(1.5, 3.0)
            log.info("источники", f"ручной список: {len(manual)} из {len(links)}")

        # 2. затравки из постов клиента
        n_seed = 0
        for uname in profile.seeds[:params.max_seeds]:
            ent = await _safe(lambda: client.get_entity(uname), params, log, f"@{uname}")
            if ent is not None and add(ent, "ссылка в постах клиента"):
                n_seed += 1
            await _pause(1.5, 3.0)
        log.info("источники", f"затравки из постов клиента: {n_seed}")

        # 3. свои диалоги
        async def _dialogs():
            return [d.entity async for d in client.iter_dialogs(limit=params.max_dialogs)]
        dialogs = await _safe(_dialogs, params, log, "свои диалоги") or []
        n_dlg = sum(1 for ent in dialogs if isinstance(ent, Channel) and add(ent, "свои чаты"))
        log.info("источники", f"группы и каналы в своих диалогах: {n_dlg}")

        # 4. поиск по названиям
        n_search = 0
        for q in profile.search_queries[:params.max_queries]:
            res = await _safe(lambda: client(SearchRequest(q=q, limit=params.search_limit)),
                              params, log, f"поиск «{q}»")
            for ch in (getattr(res, "chats", None) or []):
                if add(ch, f"поиск: {q}"):
                    n_search += 1
            await _pause(2.0, 4.0)
        log.info("источники", f"поиск по названиям: {n_search} совпадений по "
                             f"{min(len(profile.search_queries), params.max_queries)} запросам")

        # 5. рекомендации похожих каналов
        if own_ent is not None:
            try:
                from telethon.tl.functions.channels import GetChannelRecommendationsRequest
                res = await _safe(lambda: client(GetChannelRecommendationsRequest(channel=own_ent)),
                                  params, log, "похожие каналы")
                n_rec = sum(1 for ch in (getattr(res, "chats", None) or [])
                            if add(ch, "похожие каналы"))
                log.info("источники", f"похожие каналы: {n_rec}")
            except ImportError:
                log.info("источники", "похожие каналы недоступны в этой версии Telethon")

        # 6. фразы покупателя по своим чатам
        since = datetime.now(timezone.utc) - timedelta(days=params.days)
        hits_total = 0
        for phrase in profile.buyer_phrases[:params.dialog_search_queries]:
            async def _search(phrase=phrase):
                found = []
                async for m in client.iter_messages(None, search=phrase,
                                                    limit=params.dialog_search_limit):
                    found.append(m)
                return found
            found = await _safe(_search, params, log, f"поиск фразы «{phrase}»") or []
            for m in found:
                if m.date and m.date < since:
                    continue
                ch = m.chat
                if isinstance(ch, Channel):
                    s = add(ch, "фраза покупателя")
                    if s:
                        s.hits += 1
                        hits_total += 1
            await _pause(2.0, 4.0)
        log.info("источники", f"совпадений фраз покупателя в своих чатах: {hits_total}")
    except _Flood:
        log.warn("источники", "поиск прерван из-за FloodWait, работаем с тем, что нашли")

    if params.include_own and own_ent is not None:
        s = add(own_ent, "свой канал клиента")
        if s:
            manual.add(s.chat_id)

    report: list[Source] = []
    if not cands:
        log.warn("источники", "не найдено ни одного кандидата")
        return [], report

    # ── предфильтр 1: без запросов к Telegram, по названию и совпадениям ──
    plem = profile_lemmas(profile)
    docs = [tp.lemmas(f"{s.title} {s.username.replace('_', ' ')}") for s, _ in cands.values()]
    tfidf = TfIdf(docs + [plem], lex.stopwords)
    pvec = profile_vector(tfidf, profile)
    raw = [cosine(tfidf.vector(d), pvec) for d in docs]
    mx = max(raw) or 1.0
    for (s, _), r in zip(cands.values(), raw):
        s.meta_score = (0.55 * (r / mx) + 0.25 * min(1.0, s.hits / 3.0)
                        + (0.10 if "ссылка в постах клиента" in s.found_via else 0.0)
                        + (0.10 if s.is_member else 0.0))
        if s.chat_id in manual:
            s.meta_score += 10.0
    ranked = sorted(cands.values(), key=lambda x: -x[0].meta_score)
    shortlist = ranked[:params.max_sources * 2]
    for s, _ in ranked[params.max_sources * 2:]:
        s.status, s.reason = "rejected", "название далеко от темы (предфильтр 1)"
        report.append(s)
    log.info("источники", f"кандидатов {len(cands)}, на детальную проверку {len(shortlist)}")

    # ── предфильтр 2: описание, размер, наличие комментариев ──
    checked: list[tuple[Source, object]] = []
    try:
        for s, ent in shortlist:
            full = await _safe(lambda: client(GetFullChannelRequest(ent)), params, log, s.label())
            await _pause(1.0, 2.0)
            if full is None:
                s.status, s.reason = "rejected", "не удалось получить данные"
                report.append(s)
                continue
            fc = full.full_chat
            s.about = getattr(fc, "about", "") or ""
            s.members = getattr(fc, "participants_count", 0) or s.members
            s.linked_chat_id = getattr(fc, "linked_chat_id", 0) or 0
            is_manual = s.chat_id in manual
            if s.kind == "channel" and not s.linked_chat_id:
                s.status, s.reason = "rejected", "у канала нет комментариев"
            elif not s.username and not s.is_member:
                s.status, s.reason = "rejected", "закрытый чат, аккаунт не состоит"
            elif s.members and s.members < params.min_members and not (is_manual or s.is_member):
                s.status, s.reason = "rejected", f"мало участников ({s.members})"
            elif s.members > params.max_members and not is_manual:
                s.status, s.reason = "rejected", f"слишком большой ({s.members}), обычно флуд"
            else:
                checked.append((s, ent))
                continue
            report.append(s)
    except _Flood:
        log.warn("источники", "детальная проверка прервана FloodWait")
        for s, ent in shortlist:
            if not s.status and (s, ent) not in checked:
                s.status, s.reason = "rejected", "не проверен (FloodWait)"
                report.append(s)

    if checked:
        docs2 = [tp.lemmas(f"{s.title} {s.about}") for s, _ in checked]
        tfidf2 = TfIdf(docs2 + [plem], lex.stopwords)
        pvec2 = profile_vector(tfidf2, profile)
        raw2 = [cosine(tfidf2.vector(d), pvec2) for d in docs2]
        mx2 = max(raw2) or 1.0
        for (s, _), r in zip(checked, raw2):
            s.meta_score = (0.60 * (r / mx2) + 0.25 * min(1.0, s.hits / 3.0)
                            + 0.15 * _size_score(s.members)
                            + (10.0 if s.chat_id in manual else 0.0))
        checked.sort(key=lambda x: -x[0].meta_score)

    selected = checked[:params.max_sources]
    for s, _ in selected:
        s.status, s.reason = "selected", "в топе предфильтра"
    for s, _ in checked[params.max_sources:]:
        s.status, s.reason = "rejected", f"не вошёл в топ-{params.max_sources}"
        report.append(s)
    report = [s for s, _ in selected] + report

    for s in report:
        store.upsert_source(s)
        store.save_decision(params.channel, s)
    log.info("источники", f"выбрано {len(selected)}: "
                         + ", ".join(s.label() for s, _ in selected[:10])
                         + (" …" if len(selected) > 10 else ""))
    return selected, report
