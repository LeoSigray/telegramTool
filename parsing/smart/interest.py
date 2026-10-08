"""Интерес человека: текст (его запрос) + пересечения (в скольких чатах ниши он есть).

Что важнее — текст или число чатов?
  • Текст. Явный свежий запрос («ищу поставщика …», «скиньте прайс») — потребность прямо
    сейчас, это самый сильный сигнал.
  • Число чатов ниши — интерес к теме, но не потребность. И больше всего чатов ниши у
    продавцов и конкурентов: они вступают во все такие чаты, чтобы рекламироваться.
Поэтому пересечения — усилитель для людей с запросом и отдельный «тёплый» список для тех,
кто запроса не писал, но живёт в нише (пишет в нескольких её чатах). Продавцов отсекаем по
доле рекламы в их сообщениях, по вердикту LLM и по био — сколько бы чатов у них ни было.

Формулы (веса в settings.py):
  вес чата   w = 1 для зёрен, иначе (0.3 + 0.7·max(CQI, проба, пересечение людей с нишей))·P,
             P = min(1, 0.2 + 10·доля сообщений о товаре клиента) — чат селлеров WB, где товар
             клиента не обсуждают, весит мало, сколько бы там ни было людей
  охват      breadth = Σ w по чатам, где пишет + 0.6·Σ w по чатам, где только состоит
  близость   aff = 1 − exp(−(breadth/2.5 + сообщений_о_товаре/6))
  горячесть  hot = min(1, PQI лучшего запроса / hot_ref)
  интерес    горячий: 100·(w_hot·hot + w_aff·aff·(0.5+0.5·ЛПР)·G), G = min(1, hot/HOT_GATE) —
             близость только усиливает настоящий запрос: слабый «запрос» (PQI 3) не вытянуть
             одними чатами
             тёплый:  100·w_aff·aff·E·(0.5+0.5·ЛПР)          (E — был ли недавно в сети)
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import features as ft
from . import textproc as tp
from .models import Message
from .scoring import Cand, status_factor

TOPIC_R = 0.30           # сообщение считается «по теме клиента»
MEMBER_ONLY_W = 0.6      # «только состоит» весит меньше, чем «пишет»
HOT_GATE = 0.5           # с какой горячести близость к нише засчитывается полностью
EXCLUDE_REASONS = ("уже писали", "в стоп-листе", "уже в аудитории клиента", "автор просит не писать")


@dataclass
class PersonStats:
    active: set = field(default_factory=set)     # чаты ниши, где писал за окно
    member: set = field(default_factory=set)     # чаты ниши, где состоит (по спискам участников)
    total: int = 0                               # его сообщений за окно
    ads: int = 0                                 # из них реклама
    topic_msgs: int = 0                          # сообщений по теме клиента (не реклама)
    seller_llm: bool = False                     # LLM назвал его сообщение рекламой продавца
    best_topic: object = None                    # лучшее его сообщение по теме (Cand)


def product_shares(messages: list, product) -> dict:
    """chat_id → доля сообщений окна, где упомянут товар клиента (целым словом/фразой)."""
    if product is None:
        return {}
    total: Counter = Counter()
    hit: Counter = Counter()
    for m in messages:
        if not m.text:
            continue
        total[m.source_id] += 1
        if product.find(tp.lemmas(m.text)):
            hit[m.source_id] += 1
    return {sid: hit[sid] / n for sid, n in total.items() if n}


def share_factor(share: float) -> float:
    return min(1.0, 0.2 + 10.0 * share)


def collect_stats(prep, members: dict, sources: dict, product=None) -> dict:
    """uid → PersonStats: где пишет, где состоит, сколько рекламы и сообщений по теме.
    product — слова товара: по теме считается только сообщение, где товар упомянут."""
    stats: dict = defaultdict(PersonStats)
    seen: set = set()
    for c in list(prep.cands) + list(prep.dropped):
        m = c.msg
        if not m.msg_id or m.sender_kind != "user" or not m.sender_id or m.key in seen:
            continue
        seen.add(m.key)
        st = stats[m.sender_id]
        st.total += 1
        if m.source_id in sources:
            st.active.add(m.source_id)
        seller_v = c.verdict is not None and c.verdict.role == "seller"
        st.seller_llm = st.seller_llm or seller_v
        if ft.is_ad(c.f) or seller_v:
            st.ads += 1
        elif c.r >= TOPIC_R and (product is None or product.find(c.f.lemmas)):
            st.topic_msgs += 1
            if st.best_topic is None or c.r > st.best_topic.r:
                st.best_topic = c
    for chat_id, ids in members.items():
        if chat_id in sources:
            for u in ids:
                stats[u].member.add(chat_id)
    return stats


def chat_weights(sources: dict, cqi: dict, stats: dict, seed_ids: set, shares: dict | None = None):
    """Вес каждого чата ниши и доля его людей, которые есть и в других чатах ниши.
    shares — доля сообщений о товаре клиента по чатам (см. product_shares)."""
    people_by_chat: dict = defaultdict(set)
    for u, st in stats.items():
        for ch in st.active | st.member:
            people_by_chat[ch].add(u)
    cnt: Counter = Counter()
    for ppl in people_by_chat.values():
        cnt.update(ppl)
    overlap = {ch: sum(1 for u in ppl if cnt[u] >= 2) / len(ppl)
               for ch, ppl in people_by_chat.items() if ppl}
    top = max(overlap.values(), default=0.0) or 1.0
    weights = {}
    for sid, s in sources.items():
        q = cqi.get(sid, {}).get("cqi_n", 0.0)
        y = min(1.0, (s.y_est or 0.0) / 10.0)
        o = overlap.get(sid, 0.0) / top
        p = share_factor(shares[sid]) if shares and sid in shares else 1.0
        weights[sid] = 1.0 if sid in seed_ids else (0.3 + 0.7 * max(q, y, o)) * p
    return weights, overlap, people_by_chat


def affinity(st: PersonStats, weights: dict) -> tuple[float, float]:
    breadth = (sum(weights.get(ch, 0.3) for ch in st.active)
               + MEMBER_ONLY_W * sum(weights.get(ch, 0.3) for ch in st.member - st.active))
    return 1.0 - math.exp(-(breadth / 2.5 + st.topic_msgs / 6.0)), breadth


def _is_seller(st: PersonStats, params) -> str:
    if st.seller_llm:
        return "LLM: продаёт сам"
    if st.total >= 2 and st.ads / st.total >= params.seller_share:
        return f"реклама в {st.ads} из {st.total} его сообщений"
    return ""


def build_warm(stats: dict, hot_uids: set, excluded: set, authors: dict, sources: dict,
               weights: dict, params, now, own_ids: set) -> list:
    """«Тёплые»: запроса не писали, но живут в нише — несколько её чатов, сообщения по теме."""
    pool = []
    for uid, st in stats.items():
        if uid in hot_uids or uid in excluded or uid in own_ids:
            continue
        chats = st.active | st.member
        if len(chats) < 2:
            continue
        if not st.active and len(chats) < params.silent_min_chats:
            continue        # молчит: без сообщений нужен широкий охват ниши
        a = authors.get(uid)
        if a is None or a.is_bot or a.is_deleted or _is_seller(st, params):
            continue
        aff, _ = affinity(st, weights)
        if aff < params.min_affinity:
            continue
        pool.append((aff, uid, st, a))
    pool.sort(key=lambda x: -x[0])
    out = []
    for aff, uid, st, a in pool[:max(params.people * 3, 50)]:
        c = _warm_cand(uid, st, a, sources, weights, now)
        if c is not None:
            c.affinity = aff
            out.append(c)
    return out


def _warm_cand(uid, st: PersonStats, author, sources: dict, weights: dict, now):
    base = st.best_topic
    if base is not None:
        # есть сообщение по теме (без запроса) — по нему и будет «крючок» для сообщения
        c = Cand(msg=base.msg, source=base.source, author=author, f=base.f, post_text=base.post_text)
        c.r = c.rel = base.r
        c.age_h = max(0.0, (now - base.msg.date).total_seconds() / 3600.0)
    else:
        chats = [ch for ch in sorted(st.member | st.active, key=lambda ch: -weights.get(ch, 0.0))
                 if ch in sources]
        if not chats:
            return None
        msg = Message(source_id=chats[0], msg_id=0, date=now, sender_id=uid, sender_kind="user", text="")
        c = Cand(msg=msg, source=sources[chats[0]], author=author, f=ft.Features())
        c.age_h = 1e6          # не писал в окне: активность — только по статусу «в сети»
    c.kind = "warm"
    return c


def score_all(cands: list, stats: dict, weights: dict, sources: dict, lex, params, now):
    """Итоговый интерес. Возвращает (по убыванию интереса, отсеянные продавцы)."""
    ranked, sellers = [], []
    for c in cands:
        uid = c.msg.sender_id
        st = stats.get(uid) or PersonStats()
        a = c.author
        bio = (a.about or "") if a else ""
        bio_lem = tp.lemmas(bio) if bio else []
        seller = _is_seller(st, params)
        if not seller and c.kind == "warm" and bio_lem and lex.seller.find(bio_lem):
            seller = "в био реклама своих услуг/товаров"
        if seller:
            c.drop = f"похож на продавца: {seller}"
            sellers.append(c)
            continue
        aff, breadth = affinity(st, weights)
        c.affinity = aff
        c.chats_active = len(st.active)
        c.chats_member = len(st.member - st.active)
        c.topic_msgs = st.topic_msgs
        c.member_chats = sorted(sources[ch].label() for ch in st.member - st.active if ch in sources)
        dm = lex.decision_maker.find(bio_lem) if bio_lem else []
        if c.kind == "warm":
            c.a, c.a_hit = (1.0, dm[0]) if dm else (0.3, "")
            c.e = status_factor(a, c.age_h, now)
            c.hot = 0.0
            c.interest = 100.0 * params.w_aff * aff * c.e * (0.5 + 0.5 * c.a)
            c.eligible = aff >= params.min_affinity
            c.why = (f"тёплый: пишет в {c.chats_active} чатах ниши, состоит ещё в {c.chats_member}; "
                     f"сообщений по теме {c.topic_msgs}; запроса не писал"
                     + (f"; ЛПР (био: «{c.a_hit}»)" if c.a_hit else ""))
            c.warnings = ["нет явного запроса — писать мягко, от темы чата"]
            if not (a and a.username):
                c.warnings.append("нет username — написать нельзя")
        else:
            c.hot = min(1.0, c.pqi / params.hot_ref)
            gate = min(1.0, c.hot / HOT_GATE)
            c.interest = 100.0 * (params.w_hot * c.hot + params.w_aff * aff * (0.5 + 0.5 * c.a) * gate)
            c.eligible = c.pqi >= params.min_pqi
            if c.chats_active + c.chats_member > 1:
                c.why += (f"; в чатах ниши: пишет в {c.chats_active}, состоит ещё в {c.chats_member}"
                          + (f", сообщений по теме {c.topic_msgs}" if c.topic_msgs > 1 else ""))
        c.interest = min(100.0, c.interest)
        c.tier = "A" if c.interest >= params.interest_a else ("B" if c.interest >= params.interest_b else "C")
        ranked.append(c)
    ranked.sort(key=lambda c: -c.interest)
    return ranked, sellers


def chat_pairs(people_by_chat: dict, sources: dict, top: int = 40) -> list:
    """Пары чатов с наибольшим числом общих людей: (чат A, чат B, общих, доля от объединения)."""
    ids = [ch for ch in people_by_chat if ch in sources and people_by_chat[ch]]
    pairs = []
    for i, a in enumerate(ids):
        pa = people_by_chat[a]
        for b in ids[i + 1:]:
            pb = people_by_chat[b]
            shared = len(pa & pb)
            if shared:
                pairs.append((sources[a].label(), sources[b].label(), shared, shared / len(pa | pb)))
    pairs.sort(key=lambda x: (-x[2], -x[3]))
    return pairs[:top]
