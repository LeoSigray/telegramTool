"""Умный парсинг людей: точка входа.

    python -m parsing.smart --channel @client_channel
    python -m parsing.smart --channel @client_channel --days 14 --max-sources 30 --llm-budget 25
    python -m parsing.smart --channel @client_channel --offline      # пересчёт без Telegram

На выходе: data/smart_leads/<канал>_<дата>.xlsx (+ .log рядом).
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

from . import interest, scoring
from .audience import exclude_audience, load_audience
from .enrich import enrich_authors, refresh_access
from .export_excel import write_report
from .junk import filter_sources
from .members import collect_members
from .llm_judge import LLMJudge, llm_available, llm_usage
from .keywords import audience_vocabulary, ground_profile, harvest_terms, product_matcher
from .profile import (Profile, build_profile, ensure_product_keywords, ensure_query_pool, reset_if_outdated,
                      generate_queries)
from .relevance import try_embedder
from .runlog import RunLog
from .settings import OUT_DIR, Params
from .store import Store
from .textproc import Lexicon, lemmatizer


def default_out(channel: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return os.path.join(OUT_DIR, f"{channel}_{stamp}.xlsx")


CONNECT_TIMEOUT = 45  # сек на подключение одного аккаунта, дальше пробуем следующий


def _session_order(sessions: list, preferred: str) -> list:
    """Сначала аккаунт из --session (если задан), затем остальные по порядку."""
    name = lambda p: os.path.splitext(os.path.basename(p))[0]  # noqa: E731
    first = [p for p in sessions if preferred and name(p) == preferred]
    return first + [p for p in sessions if p not in first]


def _why_dead(e: Exception) -> str:
    n = type(e).__name__
    if n == "AuthKeyDuplicatedError":
        return "ключ сессии аннулирован (использовался с двух IP одновременно) — войдите заново"
    if n in ("AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError"):
        return "сессия отозвана или устарела — войдите заново"
    if n in ("UserDeactivatedError", "UserDeactivatedBanError"):
        return "аккаунт удалён или заблокирован"
    if isinstance(e, asyncio.TimeoutError):
        return f"не подключился за {CONNECT_TIMEOUT} с (сеть, прокси или VPN)"
    return f"{n}: {str(e)[:120]}"


async def _open_client(params: Params, log: RunLog):
    """Подключает первый рабочий аккаунт: если текущий не работает, берёт следующий."""
    from accounts.manager import create_client, get_session_files

    sessions = get_session_files()
    if not sessions:
        raise RuntimeError("нет аккаунтов в sessions/ — добавьте через «Управление аккаунтами»")
    if params.session and not any(os.path.splitext(os.path.basename(p))[0] == params.session
                                  for p in sessions):
        log.warn("старт", f"сессия {params.session} не найдена — пробуем остальные")

    failed = []
    for path in _session_order(sessions, params.session):
        acc = os.path.splitext(os.path.basename(path))[0]
        client = None
        try:
            client = create_client(path)
            await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
            if not await client.is_user_authorized():
                raise RuntimeError("не авторизована")
            await client.get_me()  # проверяем, что аккаунт реально отвечает
            if failed:
                log.info("старт", f"не сработали: {', '.join(failed)}")
            log.info("старт", f"читающий аккаунт: {acc}")
            client._smart_session_name = acc   # noqa: SLF001 — для сохранения сессии в базу в конце
            return client
        except Exception as e:  # noqa: BLE001 — любой сбой аккаунта: пробуем следующий
            reason = _why_dead(e)
            log.warn("старт", f"аккаунт {acc} не работает: {reason}")
            failed.append(acc)
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
    raise RuntimeError(f"ни один аккаунт не подключился ({len(failed)} шт.): "
                       f"{', '.join(failed)}. Подробности выше в логе.")


def make_exclusion_check(params: Params, log: RunLog):
    """Исключаем тех, кому уже писали, и стоп-лист из базы рассылок (data/database.db)."""
    if not params.check_contacted:
        return None
    try:
        from data import analytics as an
        an.init_analytics()
    except Exception as e:  # noqa: BLE001
        log.warn("исключения", f"база рассылок недоступна, проверка пропущена: {e}")
        return None

    def check(people: list) -> dict:
        names = [c.author.username for c in people if c.author and c.author.username]
        contacted = an.already_contacted(names)
        with an._conn() as conn:  # noqa: SLF001 — нужен ещё и матч по peer_id
            sent_ids = {str(r[0]) for r in conn.execute(
                "SELECT DISTINCT peer_id FROM sends WHERE status='sent' AND peer_id IS NOT NULL")}
        out = {}
        for c in people:
            uname = (c.author.username or "").lower() if c.author else ""
            uid = str(c.msg.sender_id)
            if (uname and uname in contacted) or uid in sent_ids:
                out[c.msg.sender_id] = "уже писали (база рассылок)"
            elif (uname and an.is_suppressed(uname)) or an.is_suppressed(uid):
                out[c.msg.sender_id] = "в стоп-листе"
        return out

    return check



def _rank_people(read: list, store, profile, lex, params, now, own_ids, audience: set,
                 exclusion, embedder):
    """Лёгкий пересчёт между волнами: правила без LLM и без обращений к Telegram.
    Возвращает (достаточно сильные для остановки чтения, все). Это только решение «хватит ли читать»,
    в отчёт этот порог не влияет."""
    quiet = RunLog(echo=False)
    sources = {s.chat_id: s for s in read}
    since = now - timedelta(days=params.days)
    messages = store.load_messages(list(sources), since)
    posts = store.load_posts(list(sources))
    authors = store.get_authors({m.sender_id for m in messages})
    sources, messages, _ = filter_sources(sources, messages, authors, lex, params, store, quiet,
                                          quiet=True)
    prep = scoring.prepare(profile, sources, messages, posts, authors, lex, params, now, own_ids,
                           quiet, embedder)
    scoring.apply_verdicts(prep, {}, params, lex)
    people, _ = scoring.aggregate(prep, sources, params, quiet, exclusion)
    people = [c for c in people if c.msg.sender_id not in audience]
    ranked, _ = scoring.finalize(people, lex, params, now)
    # био ещё не загружено (A занижен), поэтому считаем оптимистично: как если бы человек был ЛПР.
    # Иначе порог 35 недостижим и подсчёт всегда нулевой, а чтение никогда не останавливается.
    return [c for c in ranked
            if (c.pqi / (0.5 + 0.5 * c.a) if c.a < 1 else c.pqi) >= params.stop_pqi], ranked


async def run_smart_parse(params: Params, client=None, echo: bool = True) -> str:
    """Полный прогон. Возвращает путь к Excel."""
    if not params.channel:
        raise RuntimeError("не указан канал клиента")
    out_path = params.out or default_out(params.channel)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    log = RunLog(os.path.splitext(out_path)[0] + ".log", echo=echo)
    store = Store(params.db_path)
    run_id = store.start_run(params.channel)
    now = datetime.now(timezone.utc)
    lex = Lexicon()
    own_client = False
    stop = {"v": False}
    loop = asyncio.get_running_loop()

    def _on_sigint() -> None:
        stop["v"] = True
        if state_ref.get("state") is not None:
            state_ref["state"].stop = True
        log.warn("стоп", "Ctrl+C: заканчиваю текущий шаг и сохраняю отчёт по тому, что уже собрано. "
                        "Нажмите Ctrl+C ещё раз, чтобы прервать сразу без отчёта")
        try:
            loop.remove_signal_handler(signal.SIGINT)   # второй Ctrl+C — обычное прерывание
        except Exception:  # noqa: BLE001
            pass

    state_ref: dict = {}
    deadline = None
    if params.max_minutes and params.max_minutes > 0 and not params.offline:
        def _on_deadline() -> None:
            if not stop["v"]:
                stop["v"] = True
                if state_ref.get("state") is not None:
                    state_ref["state"].stop = True
                log.warn("стоп", f"лимит времени {params.max_minutes:g} мин: заканчиваю текущий шаг и "
                                 "собираю отчёт по уже найденному")
        deadline = loop.call_later(params.max_minutes * 60, _on_deadline)
    sigint_installed = False
    try:
        loop.add_signal_handler(signal.SIGINT, _on_sigint)
        sigint_installed = True
    except (NotImplementedError, RuntimeError):
        pass   # Windows: мягкая остановка недоступна, Ctrl+C прерывает сразу
    try:
        log.info("старт", f"канал @{params.channel}; нужно людей: {params.people}; окно "
                          f"{params.days} дн.; лемматизация: {lemmatizer().backend}")
        purged = store.purge(params.retention_days)
        if purged:
            log.info("старт", f"удалено старых сообщений (>{params.retention_days} дн.): {purged}")

        use_llm = False
        if params.no_llm:
            log.info("LLM", "выключен параметром --no-llm")
        else:
            ok, info = llm_available(params.llm_chain, log)
            use_llm = ok
            log.info("LLM", info if ok else f"недоступен ({info}) — работаем только на правилах")

        if not params.offline and client is None:
            client = await _open_client(params, log)
            own_client = True
        own_ids: set = set()
        if client is not None:
            me = await client.get_me()
            own_ids.add(me.id)

        # 1. профиль клиента
        profile = None
        ppath = params.profile_path()
        if os.path.exists(ppath) and not params.rebuild_profile:
            profile = Profile.load(ppath)
            log.info("профиль", f"взят сохранённый профиль {ppath} (правки вручную учитываются)")
        elif params.offline:
            raise RuntimeError(f"нет сохранённого профиля {ppath} — сначала запустите без --offline")
        else:
            profile = await build_profile(client, params, lex, log, use_llm)
            profile.save(ppath)
            log.info("профиль", f"сохранён: {ppath}")
        changed = reset_if_outdated(profile, log)
        if (client is not None and not params.offline and not profile.audience_vocab
                and not profile.keywords_checked):
            # язык аудитории клиента — образец для нейросети; слова, придуманные без него,
            # создаются заново
            profile.audience_vocab = await audience_vocabulary(client, params, lex, log)
            if profile.audience_vocab:
                profile.product_keywords, profile.buyer_phrases = [], []
                changed = True
        if await ensure_product_keywords(profile, use_llm and not params.offline, log, lex.stopwords):
            changed = True
        if not (params.offline or params.finish) and await ensure_query_pool(profile, use_llm, log, params):
            changed = True
        if (params.validate and client is not None and not (params.offline or params.finish)
                and await ground_profile(client, profile, params, log)):
            changed = True
        if changed:
            profile.save(ppath)
        terms = harvest_terms(profile, params)
        if terms:
            log.info("сбор", "читаем чаты точечно — поиск внутри чата по: " + ", ".join(terms))
        if profile.seller_phrases:
            lex = Lexicon(extra_seller=profile.seller_phrases)

        # 2. аудитория клиента (нужна и для подсчёта по ходу, и для финальной проверки)
        audience, client_group = await load_audience(client, params, store, log)
        exclusion = make_exclusion_check(params, log)
        embedder = try_embedder(params.embeddings_model, log) if params.embeddings else None
        goal = max(params.people, math.ceil(params.people * params.overshoot))

        def count_now(read: list) -> int:
            """Сколько подходящих людей уже есть (по правилам, без LLM и обращений к Telegram)."""
            return len(_rank_people(read, store, profile, lex, params, now, own_ids, audience,
                                    exclusion, embedder)[0])

        # 3. источники: раунды поиска. Читаем волнами, пока не наберём нужное число людей;
        #    если очередь кончилась, а людей мало, берём следующую порцию запросов
        #    (когда пул из 50 кончился, нейросеть генерирует новые 50) и ищем ещё раз
        queue: list = []
        read: list = []
        report: list = []
        pos = 0
        state = None
        empty_rounds = 0
        refills = 0
        if params.offline or params.finish:
            # прочитанные раньше: «взят» и «в очереди», но уже прочитанные (запуск прервали)
            report = store.load_decisions(params.channel)
            read = [s for s in report if s.status == "selected"
                    or (s.status == "queued" and s.harvested_at)]
            for s in read:
                s.status, s.reason = "selected", "прочитан"
            log.info("источники", f"{'офлайн' if params.offline else 'дособираем отчёт'}: "
                                  f"{len(read)} уже прочитанных источников, без нового поиска")
        else:
            from .discovery import (build_queries, discover_round, graph_round, load_saved_seeds,
                                    start_discovery)
            from .harvest import harvest
            state = await start_discovery(client, params, own_ids, log)
            state_ref["state"] = state
            if params.reset_seeds:
                log.info("источники", f"зёрна клиента сброшены: удалено {store.clear_seeds(params.channel)}")
            if params.graph and not params.reset_seeds:
                load_saved_seeds(store, state, params, log)

        async def take_queries() -> list:
            """Следующая порция запросов; если пул кончился, просим нейросеть новые."""
            nonlocal refills
            pool = build_queries(profile, params)
            unused = [q for q in pool if q.lower() not in state.used]
            if not unused and refills < params.query_refills:
                refills += 1
                good = [s.title for s in read if s.y_est > 0][:12]
                log.info("поиск", f"пул запросов исчерпан — просим {params.queries_target} новых "
                                  f"(догенерация {refills} из {params.query_refills})")
                more = await generate_queries(profile, use_llm, log, set(state.used) | {q.lower() for q in pool},
                                              good, params.queries_target, params.languages, "chats")
                profile.community_queries = list(profile.community_queries) + more
                profile.save(ppath)
                unused = [q for q in build_queries(profile, params) if q.lower() not in state.used]
            return unused[:params.queries_per_round]

        async def new_round() -> bool:
            """Новый раунд поиска. False — искать больше нечем."""
            nonlocal empty_rounds
            if (params.offline or params.finish or stop["v"] or state.round >= params.max_rounds
                    or empty_rounds >= params.max_empty_rounds):
                return False
            # зёрен достаточно (или поиск буксует) — идём по графу от зёрен;
            # иначе ищем новые чаты по запросам, пока не наберётся min_seeds
            graph_ready = params.graph and state.frontier(params) and (
                len(state.seeds) >= params.min_seeds or empty_rounds >= 1)
            if graph_ready:
                q, rej = await graph_round(client, profile, params, store, lex, log, state)
            else:
                queries = await take_queries()
                if queries:
                    q, rej = await discover_round(client, profile, params, store, lex, log, state,
                                                  queries)
                elif params.graph and state.frontier(params):
                    q, rej = await graph_round(client, profile, params, store, lex, log, state)
                else:
                    log.warn("поиск", "новые запросы закончились, расширять граф не от чего")
                    return False
            queue.extend(q)
            report.extend(rej)
            report.extend(src for src, _ in q)
            empty_rounds = 0 if q else empty_rounds + 1
            if not q:
                log.warn("поиск", f"раунд {state.round} не дал новых подходящих чатов")
            log.step(f"Раунд {state.round}: новых чатов в очереди", len(q))
            return True

        async def read_next_wave() -> bool:
            nonlocal pos
            if params.offline or params.finish or stop["v"]:
                return False
            while pos >= len(queue):
                if not await new_round():
                    return False
            batch = queue[pos:pos + max(1, params.wave)]
            pos += len(batch)
            await harvest(client, batch, params, store, log, terms)
            for src, _ in batch:
                src.status, src.reason = "selected", "прочитан"
                store.save_decision(params.channel, src)   # чтобы прерванный запуск можно было дособрать
            read.extend(src for src, _ in batch)
            return True

        while await read_next_wave():
            have = count_now(read)
            log.info("сбор", f"прочитано источников: {len(read)}, подходящих людей: {have} "
                             f"(нужно {params.people}, с запасом {goal}), раундов поиска: "
                             f"{state.round}")
            if have >= goal:
                break

        # 4. финальный разбор: LLM, проверка аудитории, био. Если после всех проверок людей
        #    меньше нужного, а в очереди есть чаты — дочитываем ещё волну и повторяем
        judge = LLMJudge(store, profile, params, log) if use_llm else None
        not_members: set = set()
        params.audience_check_top = max(params.audience_check_top, params.people * 2)
        params.enrich_top = max(params.enrich_top, params.people * 2)

        async def final_pass() -> dict:
            sources = {s.chat_id: s for s in read}
            since = now - timedelta(days=params.days)
            messages = store.load_messages(list(sources), since)
            posts = store.load_posts(list(sources))
            authors = store.get_authors({m.sender_id for m in messages})
            sources, messages, junk_metrics = filter_sources(sources, messages, authors, lex,
                                                             params, store, log, quiet=True)
            prep = scoring.prepare(profile, sources, messages, posts, authors, lex, params, now,
                                   own_ids, log, embedder)
            verdicts: dict = {}
            if judge is not None and prep.cands:
                top = prep.cands[:params.llm_budget * params.llm_batch]
                verdicts = await judge.judge([(c.msg.key, c.msg.text, c.post_text) for c in top])
                log.step("Проверено LLM (из них из кеша)", f"{len(verdicts)} ({judge.cache_hits})")
            scoring.apply_verdicts(prep, verdicts, params, lex)
            log.step("Запросов после проверки", len(prep.cands))
            people, cqi = scoring.aggregate(prep, sources, params, log, exclusion)
            for c in people:
                c.kind = "hot"

            # пересечения: кто есть в нескольких чатах ниши (пишет или состоит)
            seed_ids = set(state.seeds) if state is not None else set()
            order = sorted(sources.values(), key=lambda s: -(
                (1.0 if s.chat_id in seed_ids else 0.0) + cqi.get(s.chat_id, {}).get("cqi_n", 0.0)))
            members = (await collect_members(client, order, store, params, log)
                       if params.members else {})
            product = product_matcher(profile)
            shares = interest.product_shares(messages, product)
            stats = interest.collect_stats(prep, members, sources, product)
            weights, overlap, people_by_chat = interest.chat_weights(sources, cqi, stats, seed_ids, shares)
            everyone = store.get_authors(set(stats))
            excluded = ({c.msg.sender_id for c in prep.dropped
                         if c.drop.startswith(interest.EXCLUDE_REASONS)} | set(prep.opt_out))
            warm = (interest.build_warm(stats, {c.msg.sender_id for c in people}, excluded, everyone,
                                        sources, weights, params, now, own_ids)
                    if params.warm else [])
            if warm and exclusion is not None:
                try:
                    ex = exclusion(warm)
                except Exception as e:  # noqa: BLE001
                    ex = {}
                    log.warn("исключения", f"не удалось проверить тёплых по базе рассылок: {e}")
                for c in warm:
                    if c.msg.sender_id in ex:
                        c.drop = ex[c.msg.sender_id]
                        prep.dropped.append(c)
                warm = [c for c in warm if not c.drop]
            log.step("Тёплых (без запроса, но в нескольких чатах ниши)", len(warm))

            cands = people + warm
            cands, in_audience = await exclude_audience(client, cands, audience, client_group,
                                                        params, store, log, not_members)
            prep.dropped.extend(in_audience)
            if client is not None and cands and params.enrich_top > 0:
                await refresh_access(client, [c for c in cands if c.msg.msg_id], params, store, log)
                top_authors = [c.author for c in cands if c.author and c.author.username]
                await enrich_authors(client, top_authors, params, store, log)
            scoring.finalize([c for c in cands if c.kind == "hot"], lex, params, now)
            ranked, sellers = interest.score_all(cands, stats, weights, sources, lex, params, now)
            prep.dropped.extend(sellers)
            log.step("Отсеяно похожих на продавцов (по рекламе, LLM, био)", len(sellers))
            scoring.describe_people(ranked, messages, prep.cands, sources)
            all_u = [c for c in ranked if c.author and c.author.username]
            no_u = [c for c in ranked if not (c.author and c.author.username) and c.eligible
                    and c.interest >= params.min_interest]
            result = [c for c in all_u if c.eligible and c.interest >= params.min_interest][:params.people]
            taken = {id(c) for c in result}
            reserve = [c for c in all_u if id(c) not in taken][:params.reserve] if params.reserve else []
            n_hot = sum(1 for c in result if c.kind == "hot")
            log.info("сообщения", f"прочитано {len(messages)} сообщений в {len(sources)} чатах; "
                                  f"людей после всех проверок: {len(result)} из {params.people} "
                                  f"(с запросом {n_hot}, тёплых {len(result) - n_hot})")
            chat_info = {sid: {"weight": weights.get(sid, 0.0), "overlap": overlap.get(sid, 0.0),
                               "product_share": shares.get(sid),
                               "members": len(members.get(sid, ())), "people": len(people_by_chat.get(sid, ()))}
                         for sid in sources}
            return {"result": result, "reserve": reserve, "no_u": no_u, "prep": prep, "cqi": cqi,
                    "junk": junk_metrics, "sources": sources, "messages": messages, "authors": authors,
                    "chat_info": chat_info, "pairs": interest.chat_pairs(people_by_chat, sources)}

        final = await final_pass()
        rounds = 0
        while len(final["result"]) < params.people and rounds < params.topup_rounds and not stop["v"]:
            if not await read_next_wave():
                break
            rounds += 1
            log.info("сбор", f"после финальных проверок не хватает людей — дочитали волну {rounds}")
            final = await final_pass()

        # статусы источников: мусор по содержимому, непрочитанные из очереди
        filter_sources({s.chat_id: s for s in read}, final["messages"], final["authors"], lex,
                       params, store, log)
        for src, _ in queue[pos:]:
            src.status, src.reason = "rejected", "не понадобился: нужное число людей уже найдено"
        log.step("Источников найдено", len(report))
        log.step("Раундов поиска", state.round if state is not None else 0)
        log.step("Источников прочитано", len(read))
        if pos < len(queue):
            log.info("сбор", f"остановились на {len(read)} из {len(queue)} источников: людей достаточно")
        elif len(final["result"]) < params.people and not params.offline:
            log.warn("сбор", f"поиск исчерпан за {state.round} раундов, а нужное число людей не набрано. "
                             "Расширьте окно (--days), уточните бриф или увеличьте "
                             "--max-rounds")
        for src in report:
            store.save_decision(params.channel, src)

        result, reserve, no_u = final["result"], final["reserve"], final["no_u"]
        if not params.offline:
            from collections import Counter
            per_src = Counter(c.msg.source_id for c in result)
            for sid, n in per_src.items():
                store.save_seed(params.channel, sid, float(n), 0)   # «дал людей» — лучшее зерно
                store.mark_productive(sid)                          # и кандидат в общий каталог
            if per_src:
                log.info("источники", f"зёрна для следующих запусков: {len(per_src)} чатов, давших людей")
        prep, cqi, junk_metrics = final["prep"], final["cqi"], final["junk"]

        log.step("Людей в результате", f"{len(result)} из {params.people}")
        if reserve:
            log.step("В запасе (ниже порога или сверх нормы)", len(reserve))
        log.step("Без username (отдельный лист)", len(no_u))
        tiers = {t: sum(1 for c in result if c.tier == t) for t in "ABC"}
        log.step("Уровни A / B / C", f"{tiers['A']} / {tiers['B']} / {tiers['C']}")
        if use_llm:
            log.step("Вызовов LLM (по провайдерам)", f"{llm_usage()} (запросов {judge.calls if judge else 0}"
                                                    f" на проверку, бюджет {params.llm_budget})")
        if len(result) < params.people:
            log.warn("итог", f"нашлось {len(result)} из {params.people} нужных людей")
        log.info("отчёт", f"сохраняю {out_path}")
        path = write_report(out_path, result, no_u, prep.dropped, report, cqi, profile, params, log,
                            junk_metrics, reserve, final["chat_info"], final["pairs"])
        log.info("готово", f"{path}: {len(result)} из {params.people} человек, уровни A/B/C = "
                           f"{tiers['A']}/{tiers['B']}/{tiers['C']}")
        store.finish_run(run_id, path, dict(log.funnel))
        return path
    except Exception as e:
        log.error("ошибка", str(e))
        raise
    finally:
        if deadline is not None:
            deadline.cancel()
        if sigint_installed:
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except Exception:  # noqa: BLE001
                pass
        if own_client and client is not None:
            await client.disconnect()
            # кеш сессии (ключи доступа к людям и чатам) — обратно в базу проекта: при следующем
            # старте get_session_files() перезаписывает файл сессии копией из базы
            try:
                from config import SESSIONS_DIR
                from data.db import sync_session_to_db
                sync_session_to_db(getattr(client, "_smart_session_name", ""), SESSIONS_DIR)
            except Exception as e:  # noqa: BLE001
                log.warn("сессия", f"не удалось сохранить сессию в базу: {str(e)[:120]}")
        store.close()
        log.close()


def parse_args(argv=None) -> Params:
    p = argparse.ArgumentParser(prog="python -m parsing.smart",
                                description="Умный парсинг людей по каналу клиента → Excel")
    p.add_argument("--channel", required=True, help="канал клиента: @name или ссылка t.me/name")
    p.add_argument("--brief", default="", help="бриф: текст или путь к .txt")
    p.add_argument("--days", type=int, default=14, help="окно свежести, дней (14)")
    p.add_argument("--people", type=int, default=100,
                   help="сколько людей нужно найти (100): чаты читаются, пока не наберётся")
    p.add_argument("--wave", type=int, default=5, help="источников за одну волну чтения (5)")
    p.add_argument("--queries", type=int, default=50,
                   help="сколько поисковых запросов генерирует нейросеть за раз (50)")
    p.add_argument("--per-round", type=int, default=10, help="запросов в одном раунде поиска (10)")
    p.add_argument("--max-rounds", type=int, default=12, help="предел раундов поиска (12)")
    p.add_argument("--source-cap", type=int, default=60, help="предел источников на запуск (60)")
    p.add_argument("--min-pqi", type=float, default=0.0,
                   help="порог по PQI запроса (по умолчанию нет); людей по нему можно отсечь")
    p.add_argument("--min-interest", type=float, default=20.0,
                   help="порог по итоговому «Интересу» 0–100 (20; 0 — без порога)")
    p.add_argument("--full-read", action="store_true",
                   help="читать чаты целиком, а не точечно по словам товара и фразам покупателя")
    p.add_argument("--no-validate", action="store_true",
                   help="не проверять слова товара и фразы поиском Telegram")
    p.add_argument("--no-warm", action="store_true",
                   help="не брать «тёплых» (без запроса, но активных в нескольких чатах ниши)")
    p.add_argument("--lang", default="ru", help="языки чатов через запятую (ru)")
    p.add_argument("--min-seeds", type=int, default=5,
                   help="сколько хороших чатов-зёрен найти поиском, прежде чем идти по графу (5)")
    p.add_argument("--graph-depth", type=int, default=2, help="ступеней графа от зёрен (2)")
    p.add_argument("--no-members", action="store_true",
                   help="не брать списки участников чатов (пересечения только по тем, кто пишет)")
    p.add_argument("--no-graph", action="store_true",
                   help="не искать новые чаты по пересылкам, ссылкам и похожим каналам")
    p.add_argument("--no-message-search", action="store_true",
                   help="не искать фразы покупателя по сообщениям публичных чатов")
    p.add_argument("--msgs-per-source", type=int, default=500)
    p.add_argument("--llm-budget", type=int, default=25, help="максимум вызовов LLM (25)")
    p.add_argument("--no-llm", action="store_true", help="только правила, без LLM")
    p.add_argument("--llm-chain", default="",
                   help='провайдеры LLM по порядку: "openrouter:модель,groq:модель" '
                        "(по умолчанию бесплатные: LLM_FAST из .env → Groq)")
    p.add_argument("--embeddings", action="store_true",
                   help="добавить локальные эмбеддинги (нужен sentence-transformers)")
    p.add_argument("--enrich-top", type=int, default=150, help="скольким авторам запросить био")
    p.add_argument("--session", default=os.getenv("SMART_SESSION", ""),
                   help="имя сессии читающего аккаунта (или SMART_SESSION в .env)")
    p.add_argument("--out", default="", help="путь к xlsx")
    p.add_argument("--offline", action="store_true", help="пересчёт по собранному, без Telegram")
    p.add_argument("--max-minutes", type=float, default=45.0,
                   help="лимит времени, мин (45): по истечении отчёт собирается по найденному; 0 — без лимита")
    p.add_argument("--finish", action="store_true",
                   help="дособрать отчёт по уже прочитанным чатам (после остановки): без поиска, "
                        "но с проверками и био из Telegram")
    p.add_argument("--rebuild-profile", action="store_true", help="пересобрать профиль клиента")
    p.add_argument("--reset-seeds", action="store_true",
                   help="забыть сохранённые зёрна клиента и начать с поиска (если прошлые запуски "
                        "сохранили не те чаты)")
    p.add_argument("--no-contacted-check", action="store_true",
                   help="не исключать тех, кому уже писали")
    a = p.parse_args(argv)
    return Params(channel=a.channel, brief=a.brief, days=a.days, people=a.people, wave=a.wave,
                  source_cap=a.source_cap, min_pqi=a.min_pqi, min_interest=a.min_interest, warm=not a.no_warm, search_messages=not a.no_message_search,
                  languages=a.lang, graph=not a.no_graph, members=not a.no_members, min_seeds=a.min_seeds, graph_depth=a.graph_depth,
                  queries_target=a.queries, queries_per_round=a.per_round, max_rounds=a.max_rounds,
                  msgs_per_source=a.msgs_per_source,
                  llm_budget=a.llm_budget, no_llm=a.no_llm, llm_chain=a.llm_chain, embeddings=a.embeddings,
                  enrich_top=a.enrich_top, session=a.session, out=a.out,
                  offline=a.offline, finish=a.finish, max_minutes=a.max_minutes, rebuild_profile=a.rebuild_profile, reset_seeds=a.reset_seeds,
                  check_contacted=not a.no_contacted_check, targeted=not a.full_read,
                  validate=not a.no_validate)


def main(argv=None) -> int:
    params = parse_args(argv)
    try:
        path = asyncio.run(run_smart_parse(params))
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"\nОшибка: {e}", file=sys.stderr)
        return 1
    print(f"\nГотово: {path}")
    return 0
