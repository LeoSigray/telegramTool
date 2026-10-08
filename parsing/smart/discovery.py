"""Поиск источников для любого клиента: где сидят его покупатели.

Кандидаты (только публичные чаты и каналы с комментариями):
  0. каталог — чаты, уже пробованные для прошлых клиентов, близкие по теме;
  1. поиск Telegram по названиям: короткие запросы «где общаются покупатели» (роль, бизнес,
     площадка). Telegram ищет по названиям плохо, особенно длинными запросами, поэтому
     кроме исходных запросов идут их отдельные слова;
  2. глобальный поиск ПО СООБЩЕНИЯМ публичных чатов: живые фразы покупателя
     («ищу поставщика …»). Чат, где такие сообщения встречаются, — готовый кандидат.
     Чаты, где состоит сам аккаунт, и личные диалоги в результат не берутся;
  3. похожие каналы к каналу клиента.

Отбор:
  • мусор по названию и ранее признанный мусорным — сразу вон;
  • детальная проверка: описание, размер, есть ли комментарии, публичный ли;
  • проба (probe.py): язык, активность, мусор, плотность запросов по теме;
  • граф: от лучших чатов — каналы, из которых пересылают, ссылки t.me/@ и похожие
    каналы; новые кандидаты проходят тот же отбор (одна ступень).
Очередь чтения сортируется по оценке «запросов по теме в неделю» из пробы.

Канал клиента и его обсуждение источниками не бывают: там уже его клиенты.
Всё только чтение: в чаты не вступаем. Запросы идут с паузами, FloodWait выдерживаем.
"""
from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timedelta, timezone

from . import textproc as tp
from .junk import junk_by_title
from .models import Source
from .probe import evaluate as evaluate_probes
from .probe import probe_source
from .relevance import TfIdf, core_terms, cosine, profile_lemmas, profile_vector, topic_score

CATALOG_MIN = 0.35   # минимальная близость темы чата из каталога к клиенту
_GENERIC = {"чат", "chat", "группа", "клуб", "сообщество", "форум", "канал", "оптом", "опт",
            "россия", "москва", "купить", "продажа", "магазин", "цена", "заказ"}
GENERIC_B2B = ["чат предпринимателей", "бизнес чат", "предприниматели", "владельцы бизнеса",
               "селлеры маркетплейсов"]


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



def _words(queries: list, stop: set) -> list:
    """Отдельные значимые слова из запросов, по убыванию частоты."""
    from collections import Counter
    cnt: Counter = Counter()
    for q in queries:
        for w in tp.words(q):
            if len(w) >= 4 and w not in stop and not w.isdigit():
                cnt[w] += 1
    return [w for w, _ in cnt.most_common()]


def build_queries(profile, params) -> list:
    """Полный пул запросов для поиска по названиям, в порядке использования:
    короткие (1–2 слова) запросы профиля → их отдельные слова → длинные → «… чат»."""
    base = list(profile.community_queries) or list(GENERIC_B2B)
    base += [q for q in profile.search_queries if q not in base]
    out: list = []
    seen: set = set()

    def add(q: str) -> None:
        q = " ".join(q.split())
        if q and len(q) <= 40 and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)

    short = [q for q in base if len(q.split()) <= 2]
    long_ = [q for q in base if len(q.split()) > 2]
    for q in short:
        add(q)
    for w in _words(base, set(tp.read_lexicon("stopwords")) | _GENERIC):
        add(w)
    for q in long_:
        add(q)
    for q in short[:15]:
        if "чат" not in q.lower() and "chat" not in q.lower():
            add(f"{q} чат")
    return out


class DiscoveryState:
    """Состояние поиска между раундами: кандидаты, использованные запросы, счётчики."""

    def __init__(self, exclude: set, own_ent=None) -> None:
        self.pool = _Pool(exclude)
        self.own_ent = own_ent
        self.used: set = set()            # использованные запросы (в нижнем регистре)
        self.used_phrases: set = set()    # использованные фразы покупателя
        self.round = 0
        self.checked_total = 0            # сколько кандидатов проверено детально за запуск
        self.queued_total = 0             # сколько источников поставлено в очередь за запуск
        self.stop = False                 # Ctrl+C: закончить текущий шаг и выйти
        # зёрна: chat_id → {"src", "ent", "probe", "score", "depth", "expanded"}
        self.seeds: dict = {}
        self.graph_score: dict = {}       # chat_id → сумма весов связей от разных зёрен
        self.graph_edges: set = set()     # (зерно, цель): одна связь считается один раз

    def add_seed(self, src, ent, probe, score: float, depth: int) -> None:
        cur = self.seeds.get(src.chat_id)
        if cur is None or depth < cur["depth"]:
            self.seeds[src.chat_id] = {"src": src, "ent": ent, "probe": probe, "score": score,
                                       "depth": depth, "expanded": cur["expanded"] if cur else False}
        else:
            cur["score"] = max(cur["score"], score)
            cur["ent"] = cur["ent"] or ent
            cur["probe"] = cur["probe"] or probe

    def frontier(self, params) -> list:
        """Нерасширенные зёрна, от которых ещё можно идти по графу."""
        return sorted((z for z in self.seeds.values()
                       if not z["expanded"] and z["depth"] < params.graph_depth),
                      key=lambda z: -z["score"])


async def start_discovery(client, params, own_ids: set, log) -> DiscoveryState:
    from telethon.tl.functions.channels import GetFullChannelRequest

    exclude: set = set(own_ids)
    own_ent = None
    try:
        own_ent = await client.get_entity(params.channel)
        own_full = await client(GetFullChannelRequest(own_ent))
        exclude.add(own_ent.id)
        if getattr(own_full.full_chat, "linked_chat_id", None):
            exclude.add(own_full.full_chat.linked_chat_id)
    except Exception as e:  # noqa: BLE001
        log.warn("источники", f"не удалось прочитать канал клиента: {str(e)[:120]}")
    return DiscoveryState(exclude, own_ent)


class _Pool:
    """Все кандидаты запуска: chat_id → (Source, entity)."""

    def __init__(self, exclude: set) -> None:
        self.items: dict = {}
        self.exclude = exclude
        self.seen_usernames: set = set()

    def add(self, ent, via: str):
        s = source_from_entity(ent)
        if s is None or s.chat_id in self.exclude:
            return None
        if s.chat_id in self.items:
            self.items[s.chat_id][0].add_via(via)
            return None
        s.add_via(via)
        self.items[s.chat_id] = (s, ent)
        if s.username:
            self.seen_usernames.add(s.username.lower())
        return s


async def _qualify(client, ids: list, pool: _Pool, profile, params, store, lex, log,
                   report: list, budget: dict, state: DiscoveryState) -> list:
    """Отбор кандидатов: мусор → предфильтр по названию → детали → проба. Возвращает
    [(Source, entity, Probe)] прошедших."""
    from telethon.tl.functions.channels import GetFullChannelRequest

    items = [pool.items[i] for i in ids]
    alive = []
    for s, ent in items:
        reason = junk_by_title(s, lex) or store.junk_reason(s.chat_id, params.junk_recheck_days)
        if reason:
            if not reason.startswith("мусорная тематика"):
                reason = f"ранее признан мусорным ({reason})"
            s.status, s.reason = "rejected", reason
            report.append(s)
        else:
            alive.append((s, ent))
    if not alive:
        return []

    # предфильтр по названию: детально проверяем не больше check_cap за запуск
    plem = profile_lemmas(profile)
    docs = [tp.lemmas(f"{s.title} {s.username.replace('_', ' ')} {s.about}") for s, _ in alive]
    tfidf = TfIdf(docs + [plem], lex.stopwords)
    pvec = profile_vector(tfidf, profile)
    raw = [cosine(tfidf.vector(d), pvec) for d in docs]
    mx = max(raw) or 1.0
    for (s, _), r in zip(alive, raw):
        bonus = 0.3 if "каталог" in s.found_via else 0.0
        bonus += 0.6 * min(1.0, state.graph_score.get(s.chat_id, 0.0) / 2.0)
        s.meta_score = 0.7 * (r / mx) + bonus
    alive.sort(key=lambda x: -x[0].meta_score)
    room = max(0, min(params.check_cap - budget["checked"],
                      params.total_check_cap - state.checked_total))
    for s, _ in alive[room:]:
        s.status, s.reason = "rejected", f"не проверен: предел детальной проверки ({params.check_cap} за раунд)"
        report.append(s)
    alive = alive[:room]
    budget["checked"] += len(alive)
    state.checked_total += len(alive)

    checked = []
    try:
        for s, ent in alive:
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
            min_members = params.min_members_channel if s.kind == "channel" else params.min_members_group
            store.upsert_source(s)
            reason = junk_by_title(s, lex)
            if reason:
                pass
            elif s.kind == "channel" and not s.linked_chat_id:
                reason = "у канала нет комментариев"
            elif not s.username:
                reason = "закрытый чат: читаем только публичные"
            elif s.members and s.members < min_members:
                reason = f"мало участников ({s.members} < {min_members})"
            elif s.members > params.max_members:
                reason = f"слишком большой ({s.members}), обычно флуд"
            if reason:
                s.status, s.reason = "rejected", reason
                report.append(s)
            else:
                checked.append((s, ent))
    except _Flood:
        log.warn("источники", "детальная проверка прервана FloodWait")
        for s, ent in alive:
            if not s.status and all(s is not c[0] for c in checked):
                s.status, s.reason = "rejected", "не проверен (FloodWait)"
                report.append(s)

    # проба: смотрим содержимое
    probes: dict = {}
    for s, ent in checked:
        if state.stop:
            break
        try:
            probes[s.chat_id] = await probe_source(client, s, ent, params, store)
        except Exception as e:  # noqa: BLE001
            from telethon.errors import FloodWaitError
            if isinstance(e, FloodWaitError) and e.seconds > params.max_flood_wait:
                log.warn("источники", f"FloodWait {e.seconds} с на пробе — остальные без пробы")
                break
            s.status, s.reason = "rejected", f"проба не удалась: {str(e)[:80]}"
            report.append(s)
            continue
        if not probes[s.chat_id].from_cache:
            await _pause(1.0, 2.0)
    evaluate_probes(probes, profile, lex, params, store)

    passed = []
    for s, ent in checked:
        p = probes.get(s.chat_id)
        if p is None:
            if not s.status:
                s.status, s.reason = "rejected", "не пробован (FloodWait)"
                report.append(s)
            continue
        s.lang, s.lang_share, s.activity, s.y_est = p.lang, p.lang_share, p.activity, p.y_est
        if not p.from_cache:
            store.save_probe(s.chat_id, p.lang, p.lang_share, p.activity, p.terms, p.hints)
        if p.reason:
            s.status, s.reason = "rejected", p.reason
            if p.reason.startswith("мусорный"):
                store.mark_junk(s.chat_id, p.reason)
            report.append(s)
        else:
            passed.append((s, ent, p))
    log.info("источники", f"проба: прошли {len(passed)} из {len(checked)}"
                         + (f" (из каталога/кеша: {sum(1 for c in passed if c[2].from_cache)})"
                            if passed else ""))
    return passed


EDGE_W = {"fwd": 1.0, "rec": 0.7, "link": 0.5}   # пересылка точнее всего, ссылка шумнее


async def _expand_graph(client, seeds: list, pool: _Pool, params, log, state: DiscoveryState) -> list:
    """От зёрен: пересылки (без запросов), похожие каналы, ссылки t.me/@ (лимит).
    Каждой цели копится вес связей от РАЗНЫХ зёрен: чат, на который ссылаются несколько
    хороших чатов, — центральный в нише и проверяется первым."""
    from telethon.tl.types import Channel

    new: list = []
    resolves = 0
    known = {(s.username or "").lower(): cid for cid, (s, _) in pool.items.items() if s.username}

    def edge(seed_id: int, ent_or_id, kind: str, via: str):
        target = ent_or_id
        if not isinstance(target, int):
            added = pool.add(target, via)
            if added:
                new.append(added.chat_id)
            target = getattr(ent_or_id, "id", None)
        if target is None or target == seed_id or (seed_id, target) in state.graph_edges:
            return
        state.graph_edges.add((seed_id, target))
        state.graph_score[target] = state.graph_score.get(target, 0.0) + EDGE_W[kind]

    for s, ent, p in seeds:
        if state.stop:
            break
        fwd_names = set()
        for ch in (p.fwd_ents if p else []):
            fwd_names.add((ch.username or "").lower())
            edge(s.chat_id, ch, "fwd", f"граф: пересылка в {s.label()}")
        for h in (p.hints if p else []):
            kind = "fwd" if h.startswith("fwd:") else "link"
            u = h.removeprefix("fwd:")
            if u.lower() in fwd_names:
                continue
            if u.lower() in known:                     # уже в пуле: только усиливаем связь
                edge(s.chat_id, known[u.lower()], kind, "")
                continue
            if resolves >= params.graph_resolves:
                continue
            resolves += 1
            got = await _safe(lambda: client.get_entity(u), params, log, f"@{u}")
            await _pause(1.5, 3.0)
            if isinstance(got, Channel):
                edge(s.chat_id, got, kind, f"граф: {'пересылка' if kind == 'fwd' else 'ссылка'} в {s.label()}")
                known[u.lower()] = got.id
            pool.seen_usernames.add(u.lower())
        if s.kind == "channel" and ent is not None:
            try:
                from telethon.tl.functions.channels import GetChannelRecommendationsRequest
            except ImportError:
                continue
            res = await _safe(lambda: client(GetChannelRecommendationsRequest(channel=ent)),
                              params, log, f"похожие на {s.label()}")
            for ch in (getattr(res, "chats", None) or []):
                edge(s.chat_id, ch, "rec", f"граф: похож на {s.label()}")
            await _pause(1.0, 2.0)
    top = sorted(((state.graph_score.get(i, 0.0), i) for i in new), reverse=True)[:3]
    names = [f"{pool.items[i][0].label()} ({sc:.1f})" for sc, i in top if i in pool.items]
    log.info("источники", f"граф: от {len(seeds)} зёрен новых кандидатов {len(new)} "
                         f"(разрешено ссылок {resolves})"
                         + (f"; больше всего связей: {', '.join(names)}" if names else ""))
    return new


def _register_seeds(passed: list, state: DiscoveryState, store, params, depth: int) -> int:
    n = 0
    for s, ent, p in passed:
        if p.y_est >= params.seed_min_y:
            state.add_seed(s, ent, p, p.y_est, depth)
            store.save_seed(params.channel, s.chat_id, p.y_est, depth)
            n += 1
    return n


def _to_queue(passed: list, report: list, state: DiscoveryState, store, params, log, label: str):
    passed.sort(key=lambda x: (-x[2].y_est, -x[2].topic_share, -x[0].meta_score))
    room = max(0, params.source_cap - state.queued_total)
    queue = [(s, ent) for s, ent, _ in passed[:room]]
    state.queued_total += len(queue)
    for s, _ in queue:
        s.status, s.reason = "queued", "в очереди на чтение"
    for s, _, _ in passed[room:]:
        s.status, s.reason = "rejected", f"не вошёл в предел источников ({params.source_cap})"
        report.append(s)
    for s in report + [s for s, _ in queue]:
        store.upsert_source(s)
        store.save_decision(params.channel, s)
    log.info("источники", f"{label}: в очереди на чтение {len(queue)}"
                         + (": " + ", ".join(f"{s.label()} (~{s.y_est:.1f}/нед)" for s, _ in queue[:6])
                            + (" …" if len(queue) > 6 else "") if queue else "")
                         + f"; зёрен всего {len(state.seeds)}")
    return queue


def load_saved_seeds(store, state: DiscoveryState, params, log) -> int:
    """Зёрна клиента из прошлых запусков: с них граф начинается сразу, без поиска."""
    from .probe import Probe
    n = 0
    for src, score in store.load_seeds(params.channel):
        if src.chat_id in state.pool.exclude:
            continue
        cached = store.get_probe(src.chat_id, max_age_hours=24 * 30)
        probe = Probe(hints=list(cached["hints"]) if cached else [])
        state.add_seed(src, None, probe, score, 0)
        state.seeds[src.chat_id]["saved"] = True     # прочитать его тоже: там новые запросы
        n += 1
    if n:
        log.info("источники", f"зёрна из прошлых запусков: {n} (граф начнётся с них)")
    return n


async def graph_round(client, profile, params, store, lex, log, state: DiscoveryState):
    """Раунд графа: от лучших нерасширенных зёрен — новые кандидаты, отбор, новые зёрна."""
    state.round += 1
    report: list = []
    frontier = state.frontier(params)[:params.graph_seeds]
    seeds = []
    reread: list = []
    for z in frontier:
        z["expanded"] = True
        if z["ent"] is None and z["src"].username:
            z["ent"] = await _safe(lambda: client.get_entity(z["src"].username), params, log,
                                   z["src"].label())
            await _pause(0.5, 1.0)
        if z["ent"] is not None:
            seeds.append((z["src"], z["ent"], z["probe"]))
            if z.get("saved"):
                added = state.pool.add(z["ent"], "зерно из прошлых запусков")
                if added:
                    reread.append(added.chat_id)
    depth = min((z["depth"] for z in frontier), default=0) + 1
    try:
        new_ids = await _expand_graph(client, seeds, state.pool, params, log, state)
    except _Flood:
        new_ids = []
        log.warn("источники", "граф прерван из-за FloodWait")
    new_ids = reread + [i for i in new_ids if i not in reread]
    budget = {"checked": 0}
    passed = await _qualify(client, new_ids, state.pool, profile, params, store, lex, log, report,
                            budget, state) if new_ids else []
    n_seeds = _register_seeds(passed, state, store, params, depth)
    log.info("источники", f"раунд {state.round} (граф, ступень {depth}): прошли {len(passed)}, "
                         f"новых зёрен {n_seeds}")
    return _to_queue(passed, report, state, store, params, log, f"раунд {state.round}"), report


async def discover_round(client, profile, params, store, lex, log, state: DiscoveryState,
                          queries: list):
    """Один раунд поиска: запросы → кандидаты → отбор → проба → граф.
    Возвращает (очередь [(Source, entity)] по убыванию пользы, отброшенные для отчёта)."""
    from telethon.tl.functions.contacts import SearchRequest
    from telethon.tl.types import Channel

    state.round += 1
    first = state.round == 1
    pool, own_ent = state.pool, state.own_ent
    before = set(pool.items)
    report: list = []

    try:
        if first:
            # каталог прошлых запусков (для любых клиентов)
            catalog = store.catalog(max_age_days=30)
            if catalog:
                plem = profile_lemmas(profile)
                tf = TfIdf([t for _, t in catalog] + [plem], lex.stopwords)
                pv = profile_vector(tf, profile)
                core = core_terms(tf, profile)
                sims = sorted(((topic_score(tf, t, pv, core), s) for s, t in catalog),
                              key=lambda x: -x[0])
                picked = [s for sim, s in sims if sim >= CATALOG_MIN][:params.index_candidates]
                n_cat = 0
                for s in picked:
                    if s.chat_id in pool.exclude:
                        continue
                    ent = await _safe(lambda: client.get_entity(s.username), params, log,
                                      f"@{s.username}")
                    if ent is not None and pool.add(ent, "каталог (прошлые запуски)"):
                        n_cat += 1
                    await _pause(0.5, 1.0)
                log.info("источники", f"каталог: {n_cat} близких по теме из {len(catalog)} известных чатов")

        # поиск по названиям: порция запросов этого раунда
        n_search = 0
        for q in queries:
            if state.stop:
                break
            state.used.add(q.lower())
            res = await _safe(lambda: client(SearchRequest(q=q, limit=params.search_limit)),
                              params, log, f"поиск «{q}»")
            for ch in (getattr(res, "chats", None) or []):
                if pool.add(ch, f"поиск: {q}"):
                    n_search += 1
            await _pause(2.0, 4.0)
        log.info("источники", f"раунд {state.round}: поиск по названиям, новых {n_search} по "
                             f"{len(queries)} запросам ({', '.join(queries[:4])}{' …' if len(queries) > 4 else ''})")

        if first and own_ent is not None:
            # похожие каналы к каналу клиента
            try:
                from telethon.tl.functions.channels import GetChannelRecommendationsRequest
                res = await _safe(lambda: client(GetChannelRecommendationsRequest(channel=own_ent)),
                                  params, log, "похожие каналы")
                n_rec = sum(1 for ch in (getattr(res, "chats", None) or [])
                            if pool.add(ch, "похожие каналы"))
                log.info("источники", f"похожие каналы: {n_rec}")
            except ImportError:
                log.info("источники", "похожие каналы недоступны в этой версии Telethon")

        if params.search_messages:
            # глобальный поиск по сообщениям публичных чатов. В результат берём только
            # публичные группы и каналы, где аккаунт НЕ состоит: личные диалоги и чаты
            # самого аккаунта не используются.
            since = datetime.now(timezone.utc) - timedelta(days=params.days)
            unused = [p for p in profile.buyer_phrases if p.lower() not in state.used_phrases]
            phrases = unused[:params.phrases_per_round]
            hits_total, new_chats = 0, 0
            raw_total, own_total, fresh_total, other_total = 0, 0, 0, 0
            for phrase in phrases:
                if state.stop:
                    break
                state.used_phrases.add(phrase.lower())

                async def _search(phrase=phrase):
                    return [m async for m in client.iter_messages(None, search=phrase,
                                                                  limit=params.phrase_hits)]
                for m in await _safe(_search, params, log, f"поиск фразы «{phrase}»") or []:
                    raw_total += 1
                    ch = getattr(m, "chat", None)
                    # возраст сообщения не важен: чат, где когда-то писали «куплю айфон», — кандидат;
                    # живой ли он сейчас, решит проба
                    if m.date and m.date >= since:
                        fresh_total += 1
                    if not isinstance(ch, Channel) or not (getattr(ch, "username", None)
                                                           or getattr(ch, "usernames", None)):
                        other_total += 1    # личные диалоги, закрытые чаты
                        continue
                    if not getattr(ch, "left", True):
                        own_total += 1
                        continue            # аккаунт состоит в этом чате: личное, не трогаем
                    existed = ch.id in pool.items
                    s = pool.add(ch, f"фраза: {phrase}") or pool.items.get(ch.id, (None,))[0]
                    if s is not None:
                        s.hits += 1
                        hits_total += 1
                        if not existed:
                            new_chats += 1
                await _pause(2.0, 4.0)
            if phrases:
                log.info("источники", f"раунд {state.round}: поиск по сообщениям, фраз {len(phrases)}, "
                                      f"найдено сообщений {raw_total} (свежих {fresh_total}, в чатах "
                                      f"аккаунта {own_total}, личные/закрытые {other_total}), "
                                      f"подходящих {hits_total}, новых чатов {new_chats} "
                                      f"({'; '.join(phrases[:3])}{' …' if len(phrases) > 3 else ''})")
    except _Flood:
        log.warn("источники", "поиск прерван из-за FloodWait, работаем с тем, что нашли")

    new_ids = [i for i in pool.items if i not in before]
    log.info("источники", f"раунд {state.round}: новых кандидатов {len(new_ids)} "
                         f"(всего в пуле {len(pool.items)})")
    budget = {"checked": 0}
    passed = await _qualify(client, new_ids, pool, profile, params, store, lex, log, report,
                            budget, state) if new_ids else []

    n_seeds = _register_seeds(passed, state, store, params, 0)
    if n_seeds:
        log.info("источники", f"раунд {state.round}: новых зёрен {n_seeds} (всего {len(state.seeds)})")
    queue = _to_queue(passed, report, state, store, params, log, f"раунд {state.round}")
    return queue, report
