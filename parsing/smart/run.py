"""Умный парсинг людей: точка входа.

    python -m parsing.smart --channel @client_channel
    python -m parsing.smart --channel @client_channel --days 14 --max-sources 30 --llm-budget 25
    python -m parsing.smart --channel @client_channel --offline      # пересчёт без Telegram

На выходе: data/smart_leads/<канал>_<дата>.xlsx (+ .log рядом).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from . import scoring
from .enrich import enrich_authors
from .export_excel import write_report
from .llm_judge import LLMJudge, llm_available
from .profile import Profile, build_profile
from .relevance import try_embedder
from .runlog import RunLog
from .settings import OUT_DIR, Params
from .store import Store
from .textproc import Lexicon, lemmatizer


def default_out(channel: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return os.path.join(OUT_DIR, f"{channel}_{stamp}.xlsx")


async def _open_client(params: Params, log: RunLog):
    from accounts.manager import create_client, get_session_files

    sessions = get_session_files()
    if not sessions:
        raise RuntimeError("нет аккаунтов в sessions/ — добавьте через «Управление аккаунтами»")
    path = sessions[0]
    if params.session:
        named = [p for p in sessions
                 if os.path.splitext(os.path.basename(p))[0] == params.session]
        if not named:
            raise RuntimeError(f"сессия {params.session} не найдена в sessions/")
        path = named[0]
    client = create_client(path)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(f"сессия {os.path.basename(path)} не авторизована")
    log.info("старт", f"читающий аккаунт: {os.path.splitext(os.path.basename(path))[0]}")
    return client


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
    try:
        log.info("старт", f"канал @{params.channel}; окно {params.days} дн.; источников до "
                          f"{params.max_sources}; лемматизация: {lemmatizer().backend}")
        purged = store.purge(params.retention_days)
        if purged:
            log.info("старт", f"удалено старых сообщений (>{params.retention_days} дн.): {purged}")

        use_llm = False
        if params.no_llm:
            log.info("LLM", "выключен параметром --no-llm")
        else:
            ok, info = llm_available()
            use_llm = ok
            log.info("LLM", f"бесплатный облачный: {info}" if ok
                     else f"недоступен ({info}) — работаем только на правилах")

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
        if profile.seller_phrases:
            lex = Lexicon(extra_seller=profile.seller_phrases)

        # 2. источники и сбор
        if params.offline:
            report = store.load_decisions(params.channel)
            selected = [s for s in report if s.status == "selected"]
            log.info("источники", f"офлайн: {len(selected)} источников из прошлого запуска")
        else:
            from .discovery import discover
            from .harvest import harvest
            items, report = await discover(client, profile, params, store, lex, log, own_ids)
            log.step("Источников найдено", len(report))
            selected = [s for s, _ in items]
            await harvest(client, items, params, store, log)
        log.step("Источников прочитано", len(selected))
        sources = {s.chat_id: s for s in selected}

        # 3. сообщения окна
        since = now - timedelta(days=params.days)
        messages = store.load_messages(list(sources), since)
        posts = store.load_posts(list(sources))
        authors = store.get_authors({m.sender_id for m in messages})
        log.info("сообщения", f"в окне {len(messages)} сообщений от {len(authors)} авторов")

        # 4. правила, тема, свежесть, ветки
        embedder = try_embedder(params.embeddings_model, log) if params.embeddings else None
        prep = scoring.prepare(profile, sources, messages, posts, authors, lex, params, now,
                               own_ids, log, embedder)

        # 5. LLM только для верхушки
        verdicts: dict = {}
        judge = None
        if use_llm and prep.cands:
            judge = LLMJudge(store, profile, params, log)
            top = prep.cands[:params.llm_budget * params.llm_batch]
            log.info("LLM", f"на проверку {len(top)} лучших по правилам "
                            f"(бюджет {params.llm_budget} вызовов × {params.llm_batch})")
            verdicts = await judge.judge([(c.msg.key, c.msg.text, c.post_text) for c in top])
            log.step("Проверено LLM (из них из кеша)", f"{len(verdicts)} ({judge.cache_hits})")
        scoring.apply_verdicts(prep, verdicts, params, lex)
        log.step("Запросов после проверки", len(prep.cands))

        # 6. авторы, исключения, индекс источников
        people, cqi = scoring.aggregate(prep, sources, params, log,
                                        make_exclusion_check(params, log))

        # 7. био для верхушки (ЛПР)
        if client is not None and people and params.enrich_top > 0:
            top_authors = [c.author for c in people if c.author and c.author.username]
            await enrich_authors(client, top_authors, params, store, log)

        # 8. итог
        with_u, no_u = scoring.finalize(people, lex, params, now)
        log.step("Людей в Excel (с username)", len(with_u))
        log.step("Без username (отдельный лист)", len(no_u))
        tiers = {t: sum(1 for c in with_u if c.tier == t) for t in "ABC"}
        log.step("Уровни A / B / C", f"{tiers['A']} / {tiers['B']} / {tiers['C']}")
        if judge is not None:
            log.step("Вызовов LLM", f"{judge.calls} из {params.llm_budget}")
        log.info("отчёт", f"сохраняю {out_path}")
        path = write_report(out_path, with_u, no_u, prep.dropped, report, cqi, profile, params, log)
        log.info("готово", f"{path}: {len(with_u)} человек, уровни A/B/C = "
                           f"{tiers['A']}/{tiers['B']}/{tiers['C']}")
        store.finish_run(run_id, path, dict(log.funnel))
        return path
    except Exception as e:
        log.error("ошибка", str(e))
        raise
    finally:
        if own_client and client is not None:
            await client.disconnect()
        store.close()
        log.close()


def parse_args(argv=None) -> Params:
    p = argparse.ArgumentParser(prog="python -m parsing.smart",
                                description="Умный парсинг людей по каналу клиента → Excel")
    p.add_argument("--channel", required=True, help="канал клиента: @name или ссылка t.me/name")
    p.add_argument("--brief", default="", help="бриф: текст или путь к .txt")
    p.add_argument("--days", type=int, default=14, help="окно свежести, дней (14)")
    p.add_argument("--max-sources", type=int, default=30, help="сколько источников читать (30)")
    p.add_argument("--sources-file", default="", help="файл со ссылками на чаты, по одной в строке")
    p.add_argument("--msgs-per-source", type=int, default=500)
    p.add_argument("--llm-budget", type=int, default=25, help="максимум вызовов LLM (25)")
    p.add_argument("--no-llm", action="store_true", help="только правила, без LLM")
    p.add_argument("--embeddings", action="store_true",
                   help="добавить локальные эмбеддинги (нужен sentence-transformers)")
    p.add_argument("--enrich-top", type=int, default=150, help="скольким авторам запросить био")
    p.add_argument("--top", type=int, default=500, help="максимум людей на листе")
    p.add_argument("--session", default="", help="имя сессии читающего аккаунта")
    p.add_argument("--out", default="", help="путь к xlsx")
    p.add_argument("--offline", action="store_true", help="пересчёт по собранному, без Telegram")
    p.add_argument("--rebuild-profile", action="store_true", help="пересобрать профиль клиента")
    p.add_argument("--include-own", action="store_true", help="брать комментаторов канала клиента")
    p.add_argument("--no-contacted-check", action="store_true",
                   help="не исключать тех, кому уже писали")
    a = p.parse_args(argv)
    return Params(channel=a.channel, brief=a.brief, days=a.days, max_sources=a.max_sources,
                  sources_file=a.sources_file, msgs_per_source=a.msgs_per_source,
                  llm_budget=a.llm_budget, no_llm=a.no_llm, embeddings=a.embeddings,
                  enrich_top=a.enrich_top, top=a.top, session=a.session, out=a.out,
                  offline=a.offline, rebuild_profile=a.rebuild_profile,
                  include_own=a.include_own, check_contacted=not a.no_contacted_check)


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
