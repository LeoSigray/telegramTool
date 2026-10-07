"""Скоринг: из сообщений в отсортированный список людей.

Этапы (их же видно в воронке на листе «Лог»):
  prepare   — признаки, жёсткие фильтры, дубли, ветки, тема, свежесть, мягкие фильтры;
  apply_verdicts — объединение правил и ответа LLM;
  aggregate — лучший запрос на автора, исключения, индекс источника CQI;
  finalize  — активность, ЛПР, итоговый PQI, уровни, объяснения.

Формулы:
  CQI = U · A · Rel / (1 + Comp) · (1 − Ad)
  PQI = 100 · I · R · F · T · E · (0.5 + 0.5·A) · (0.7 + 0.3·CQĨ) · бонус
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from . import features as ft
from . import textproc as tp
from .llm_judge import INTENT_RU, ROLE_RU
from .models import Author, Message, Source, Verdict
from .relevance import (EMB_FLOOR, TfIdf, core_terms, normalize_scores, profile_lemmas,
                        profile_vector, topic_score)
from .store import parse_iso

REQUEST_I = 0.5      # с какого намерения сообщение считается запросом (для CQI)
REQUEST_R = 0.3      # и с какой темы


@dataclass
class Cand:
    msg: Message
    source: Source
    author: Optional[Author]
    f: ft.Features
    post_text: str = ""
    r_raw: float = 0.0
    r: float = 0.0              # тема по TF-IDF / эмбеддингам, 0..1
    age_h: float = 0.0
    fresh: float = 0.0          # F
    n_comp: int = 0
    closed: bool = False
    t: float = 1.0              # T
    pre: float = 0.0            # предварительная оценка для выбора кандидатов в LLM
    verdict: Optional[Verdict] = None
    i: float = 0.0              # I — итоговое намерение
    rel: float = 0.0            # R — итоговая тема
    intent_type: str = "none"
    base: float = 0.0           # I·R·F·T
    e: float = 1.0              # E
    a: float = 0.3              # A
    a_hit: str = ""
    cqi_n: float = 0.0
    n_requests: int = 1
    bonus: float = 1.0
    pqi: float = 0.0
    tier: str = "C"
    why: str = ""
    warnings: list = field(default_factory=list)
    drop: str = ""


@dataclass
class Prep:
    cands: list                 # прошли фильтры, по убыванию pre
    dropped: list               # отсеянные, с причиной в .drop
    src_stats: dict             # chat_id → {"text": сообщений людей, "ad": из них реклама}
    opt_out: set


def _thread_stats(all_cands: list, alive: list, lex: tp.Lexicon) -> None:
    """Конкуренты в ветке и закрыл ли автор свой запрос."""
    children: dict[tuple, list] = defaultdict(list)
    by_author: dict[tuple, list] = defaultdict(list)
    for c in all_cands:
        if c.msg.reply_to:
            children[(c.msg.source_id, c.msg.reply_to)].append(c)
        by_author[(c.msg.source_id, c.msg.post_id, c.msg.sender_id)].append(c)
    for c in alive:
        comp: set = set()
        closed = False
        for k in children.get((c.msg.source_id, c.msg.msg_id), ()):
            if k.msg.sender_id == c.msg.sender_id:
                closed = closed or bool(lex.closed.find(k.f.lemmas))
            elif k.msg.sender_id and (lex.competitor.find(k.f.lemmas) or k.f.seller_hits
                                      or k.f.invite_hits or k.f.n_links or k.f.n_mentions
                                      or k.f.n_phones):
                comp.add(k.msg.sender_id)
        if not closed:
            for o in by_author.get((c.msg.source_id, c.msg.post_id, c.msg.sender_id), ()):
                dt = (o.msg.date - c.msg.date).total_seconds()
                if 0 < dt < 7 * 86400 and lex.closed.find(o.f.lemmas):
                    closed = True
                    break
        c.n_comp, c.closed = len(comp), closed


def prepare(profile, sources: dict, messages: list, posts: dict, authors: dict,
            lex: tp.Lexicon, params, now, own_ids: set, log, embedder=None) -> Prep:
    dropped: list = []
    everything: list = []
    alive: list = []
    src_stats: dict = defaultdict(lambda: {"text": 0, "ad": 0})
    opt_out: set = set()

    for m in messages:
        src = sources.get(m.source_id)
        if src is None:
            continue
        f = ft.extract(m.text, lex)
        c = Cand(msg=m, source=src, author=authors.get(m.sender_id), f=f,
                 post_text=posts.get((m.source_id, m.post_id), "") if m.post_id else "")
        everything.append(c)
        if m.sender_kind == "user" and m.sender_id:
            src_stats[m.source_id]["text"] += 1
            if f.opt_out_hits:
                opt_out.add(m.sender_id)
        reason = ft.hard_reject(m, c.author, f, own_ids)
        if reason:
            if m.sender_kind == "user" and ft.is_ad(f):
                src_stats[m.source_id]["ad"] += 1
            c.drop = reason
            dropped.append(c)
            continue
        alive.append(c)
    log.step("Сообщений в окне", len(messages))
    log.step("После жёстких фильтров", len(alive))

    ft.mark_duplicates(alive)
    for c in alive:
        c.f.intent_rule = ft.intent_rule(c.f)
        if ft.is_ad(c.f):
            src_stats[c.msg.source_id]["ad"] += 1
    _thread_stats(everything, alive, lex)

    # тема: TF-IDF по корпусу этого запуска + профиль клиента
    plem = profile_lemmas(profile)
    tfidf = TfIdf([c.f.lemmas for c in alive] + [plem], lex.stopwords)
    pvec = profile_vector(tfidf, profile)
    core = core_terms(tfidf, profile)
    post_raw: dict = {}
    raws: list = []
    for c in alive:
        r = topic_score(tfidf, c.f.lemmas, pvec, core)
        if c.post_text and (c.f.question or c.f.intent_hits):
            key = (c.msg.source_id, c.msg.post_id)
            if key not in post_raw:
                post_raw[key] = topic_score(tfidf, tp.lemmas(c.post_text), pvec, core)
            r = max(r, 0.7 * post_raw[key])   # «сколько стоит?» под профильным постом
        raws.append(r)
    norm = list(raws)
    if embedder is not None and alive:
        try:
            ptext = f"{profile.offer}. {profile.audience}. {', '.join(profile.topic_terms[:30])}"
            embs = embedder.encode([c.msg.text[:500] for c in alive])
            pemb = embedder.encode([ptext])[0]
            enorm = normalize_scores([float(x) for x in embs @ pemb], EMB_FLOOR)
            norm = [0.5 * a + 0.5 * b for a, b in zip(norm, enorm)]
            log.info("релевантность", "тема = 0.5·TF-IDF + 0.5·эмбеддинги")
        except Exception as e:  # noqa: BLE001
            log.warn("релевантность", f"эмбеддинги не сработали: {e}")

    for c, r_raw, r in zip(alive, raws, norm):
        c.r_raw, c.r = r_raw, r
        c.age_h = max(0.0, (now - c.msg.date).total_seconds() / 3600.0)
        c.fresh = math.exp(-c.age_h / params.tau_hours)
        c.t = 0.0 if c.closed else 1.0 / (1 + c.n_comp)
        c.pre = c.f.intent_rule * c.r * c.fresh * max(c.t, 0.1)

    keep: list = []
    for c in alive:
        if c.msg.sender_id in opt_out:
            c.drop = "автор просит не писать в ЛС"
        elif c.msg.reply_to and lex.competitor.find(c.f.lemmas) and not c.f.intent_strong:
            c.drop = "ответ исполнителя в чужой ветке"
        elif c.f.ad_dup:
            c.drop = f"реклама: тот же текст в {c.f.dup_sources} чатах"
        elif c.f.intent_rule < params.min_intent:
            c.drop = "продавец / реклама (правила)" if (c.f.seller_hits or c.f.invite_hits) \
                else "нет признаков запроса"
        elif c.r < params.min_rel:
            c.drop = "не по теме клиента"
        elif c.closed:
            c.drop = "автор закрыл запрос (уже нашёл)"
        if c.drop:
            dropped.append(c)
        else:
            keep.append(c)
    keep.sort(key=lambda c: -c.pre)
    log.step("Похожи на запрос по теме (правила)", len(keep))
    return Prep(cands=keep, dropped=dropped, src_stats=dict(src_stats), opt_out=opt_out)


def apply_verdicts(prep: Prep, verdicts: dict, params, lex: tp.Lexicon) -> None:
    """Объединяет правила и LLM. verdicts: {msg.key: Verdict}."""
    keep: list = []
    for c in prep.cands:
        v = verdicts.get(c.msg.key)
        c.verdict = v
        i_rule = c.f.intent_rule
        if v is None:
            c.i, c.rel = i_rule, c.r
            c.intent_type = ft.intent_type_rule(c.f, lex)
        else:
            if v.role == "seller":
                c.i = 0.0
            elif v.role == "buyer" and v.is_request:
                c.i = max(i_rule, 0.6 + 0.4 * v.specific)
            elif v.is_request:
                c.i = max(0.5 * i_rule, 0.45 + 0.2 * v.specific)
            else:
                c.i = 0.5 * i_rule
            c.rel = 0.5 * c.r + 0.5 * v.topic
            c.intent_type = v.intent if v.intent != "none" else ft.intent_type_rule(c.f, lex)
        if v is not None and v.role == "seller":
            c.drop = "продавец (LLM)"
            st = prep.src_stats.setdefault(c.msg.source_id, {"text": 0, "ad": 0})
            st["ad"] += 1
        elif c.i < params.min_intent:
            c.drop = "не запрос (LLM)" if v is not None else "нет признаков запроса"
        elif c.rel < params.min_rel:
            c.drop = "не по теме (LLM)" if v is not None else "не по теме клиента"
        if c.drop:
            prep.dropped.append(c)
        else:
            c.base = c.i * c.rel * c.fresh * c.t
            keep.append(c)
    prep.cands = keep


def compute_cqi(sources: dict, cands: list, src_stats: dict, days: int) -> dict:
    weeks = max(days / 7.0, 1.0 / 7.0)
    per: dict = defaultdict(list)
    for c in cands:
        if c.i >= REQUEST_I and c.rel >= REQUEST_R:
            per[c.msg.source_id].append(c)
    out: dict = {}
    for sid in sources:
        reqs = per.get(sid, [])
        by_author = {c.msg.sender_id: c for c in reqs}
        n_auth = len(by_author)
        a_share = (sum(1 for c in by_author.values() if c.author and c.author.username) / n_auth
                   if n_auth else 0.0)
        rel = sum(c.rel for c in reqs) / len(reqs) if reqs else 0.0
        comp = sum(c.n_comp for c in reqs) / len(reqs) if reqs else 0.0
        st = src_stats.get(sid, {"text": 0, "ad": 0})
        ad = st["ad"] / st["text"] if st["text"] else 0.0
        u = n_auth / weeks
        y = u * a_share * rel / (1.0 + comp) * (1.0 - min(ad, 1.0))
        out[sid] = {"read": st["text"], "requests_week": len(reqs) / weeks, "authors_week": u,
                    "username_share": a_share, "rel": rel, "comp": comp, "ad": ad, "cqi": y}
    mx = max((m["cqi"] for m in out.values()), default=0.0)
    for m in out.values():
        m["cqi_n"] = m["cqi"] / mx if mx > 0 else 0.0
    return out


def aggregate(prep: Prep, sources: dict, params, log, exclusion_check=None):
    """Лучший запрос на автора + исключения. Возвращает (люди, cqi по источникам)."""
    cqi = compute_cqi(sources, prep.cands, prep.src_stats, params.days)
    best: dict = {}
    reqs: Counter = Counter()
    for c in prep.cands:
        sid = c.msg.sender_id
        if c.i >= REQUEST_I and c.rel >= REQUEST_R:
            reqs[sid] += 1
        if sid not in best or c.base > best[sid].base:
            best[sid] = c
    people = list(best.values())
    for c in people:
        c.n_requests = max(1, reqs[c.msg.sender_id])
        c.cqi_n = cqi.get(c.msg.source_id, {}).get("cqi_n", 0.0)
    log.step("Уникальных авторов-кандидатов", len(people))

    if exclusion_check is not None and people:
        try:
            excluded = exclusion_check(people)
        except Exception as e:  # noqa: BLE001
            excluded = {}
            log.warn("исключения", f"не удалось проверить базу рассылок: {e}")
        kept = []
        for c in people:
            reason = excluded.get(c.msg.sender_id)
            if reason:
                c.drop = reason
                prep.dropped.append(c)
            else:
                kept.append(c)
        log.step("Исключено: уже писали или стоп-лист", len(people) - len(kept))
        people = kept

    people.sort(key=lambda c: -(c.base * (0.7 + 0.3 * c.cqi_n)))
    return people, cqi


def status_factor(author: Optional[Author], age_h: float, now) -> float:
    """E: увидит ли человек сообщение. Свежий пост сам по себе говорит об активности."""
    by_msg = 1.0 if age_h <= 72 else (0.8 if age_h <= 7 * 24 else 0.5)
    if author is None:
        return max(0.5, by_msg)
    s = author.status
    if s in ("online", "recently"):
        st = 1.0
    elif s == "last_week":
        st = 0.8
    elif s == "last_month":
        st = 0.5
    elif s == "offline" and author.was_online:
        try:
            days = (now - parse_iso(author.was_online)).total_seconds() / 86400
        except ValueError:
            days = 999
        st = 1.0 if days <= 3 else (0.8 if days <= 7 else (0.5 if days <= 30 else 0.3))
    else:
        st = 0.4   # статус скрыт
    return max(st, by_msg)


def _age_str(h: float) -> str:
    if h < 1:
        return "меньше часа"
    if h < 48:
        return f"{h:.0f} ч"
    return f"{h / 24:.0f} дн"


def explain(c: Cand) -> str:
    parts = []
    v = c.verdict
    if v is not None:
        s = f"LLM: {ROLE_RU.get(v.role, v.role)}"
        if v.intent != "none":
            s += f", {INTENT_RU.get(v.intent, v.intent)}"
        if v.why:
            s += f" — {v.why}"
        parts.append(s)
    if c.f.intent_hits:
        parts.append("маркеры запроса: " + ", ".join(f"«{h}»" for h in c.f.intent_hits[:3]))
    if c.f.question:
        parts.append("вопрос")
    if c.f.invite_hits and c.f.intent_hits:
        parts.append("ждёт предложений в ЛС")
    if c.f.specificity >= 0.4:
        parts.append("есть конкретика (бюджет/сроки)")
    parts.append(f"тема {c.rel:.2f}")
    parts.append(f"{_age_str(c.age_h)} назад")
    parts.append("конкурентов в ветке нет" if c.n_comp == 0 else f"конкурентов в ветке: {c.n_comp}")
    if c.a >= 0.8:
        parts.append(f"ЛПР (био: «{c.a_hit}»)" if c.a_hit else "ЛПР (по оценке LLM)")
    if c.n_requests > 1:
        parts.append(f"запросов от автора: {c.n_requests}")
    if c.f.dup_sources > 1 and not c.f.ad_dup:
        parts.append(f"продублировал запрос в {c.f.dup_sources} чатах")
    return "; ".join(parts)


def finalize(people: list, lex: tp.Lexicon, params, now) -> tuple[list, list]:
    """Итоговый PQI. Возвращает (с username, без username), по убыванию PQI."""
    for c in people:
        au = c.author
        c.e = status_factor(au, c.age_h, now) * (1.0 if au and au.name else 0.85)
        hits = lex.decision_maker.find(tp.lemmas(au.about)) if au and au.about else []
        if hits:
            c.a, c.a_hit = 1.0, hits[0]
        elif c.verdict is not None and c.verdict.dm >= 0.6:
            c.a = 0.8
        else:
            c.a = 0.3
        c.bonus = 1.0 + 0.1 * min(c.n_requests - 1, 3)
        c.pqi = min(100.0, 100.0 * c.base * c.e * (0.5 + 0.5 * c.a)
                    * (0.7 + 0.3 * c.cqi_n) * c.bonus)
        c.tier = "A" if c.pqi >= params.tier_a else ("B" if c.pqi >= params.tier_b else "C")
        c.why = explain(c)
        w = []
        if c.verdict is None:
            w.append("проверено только правилами")
        if not (au and au.username):
            w.append("нет username — написать нельзя")
        if c.e < 0.6:
            w.append("давно не был в сети")
        if c.f.seller_hits:
            w.append("есть фразы продавца: " + ", ".join(c.f.seller_hits[:2]))
        if c.n_comp >= 2:
            w.append("много конкурентов в ветке")
        if c.cqi_n == 0:
            w.append("в источнике мало других запросов")
        c.warnings = w
    people.sort(key=lambda c: -c.pqi)
    with_u = [c for c in people if c.author and c.author.username]
    no_u = [c for c in people if not (c.author and c.author.username)]
    return with_u[:params.top], no_u[:params.top]
