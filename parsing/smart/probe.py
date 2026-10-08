"""Проба чата перед чтением: смотрим последние ~100 сообщений и решаем, стоит ли он того.

Что считаем (для любого клиента одинаково, параметры в settings.py):
  • язык — доля сообщений на нужных языках (--lang), чтобы не тащить чужие чаты;
  • активность — сообщений в день и давность последнего сообщения;
  • мусор — боты, ссылки, копипаст, реклама, заработки (junk.py);
  • плотность запросов — сколько сообщений похожи на запрос покупателя ПО ТЕМЕ клиента,
    в пересчёте на неделю. По ней строится очередь чтения, а не по названию чата;
  • подсказки для графа — каналы, из которых пересылают, и ссылки t.me/@ в сообщениях.

Результат пробы сохраняется в каталог (SQLite): следующий клиент с похожей темой
получит этот чат сразу, без поиска.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import features as ft
from . import textproc as tp
from .harvest import author_from_user, to_message
from .junk import content_metrics, verdict as junk_verdict
from .keywords import product_matcher
from .relevance import TfIdf, core_terms, profile_lemmas, profile_vector, topic_score

LINK_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_/])",
                     re.IGNORECASE)
NOT_LINKS = {"joinchat", "addlist", "share", "proxy", "socks", "addstickers", "addemoji", "iv", "s",
             "c", "boost", "contact", "login", "setlanguage", "bg", "invoice"}
LAT_RE = re.compile(r"[a-z]", re.IGNORECASE)
EN_STOP = {"the", "and", "for", "you", "with", "this", "that", "are", "have", "is", "to", "of", "in",
           "need", "looking", "anyone", "price"}
REQUEST_I = 0.45
REQUEST_R = 0.30


def detect_lang(text: str) -> str:
    """Грубое определение языка сообщения: ru, uk, be, kk, uz, en, other или ''."""
    low = (text or "").lower()
    cyr = len(tp.CYR_RE.findall(low)) + sum(low.count(ch) for ch in "іїєґўәғқңөұүһ")
    lat = len(LAT_RE.findall(low))
    if cyr + lat < 3:
        return ""
    if cyr >= lat:
        if any(ch in low for ch in "әңөұүһ"):
            return "kk"
        if any(ch in low for ch in "қғҳ") or ("ў" in low and "і" not in low and "ы" not in low):
            return "uz"
        if "ў" in low:
            return "be"
        if any(ch in low for ch in "їєґі"):
            return "uk"
        return "ru"
    words = set(re.findall(r"[a-z']+", low))
    return "en" if words & EN_STOP else "other"


@dataclass
class Probe:
    msgs: list = field(default_factory=list)       # сообщения людей (Message), без постов
    posts: dict = field(default_factory=dict)      # (chat_id, post_id) → текст поста
    activity: float = 0.0                          # сообщений (комментариев) в день
    newest_age_days: float = 1e9
    lang: str = ""
    lang_share: float = 0.0
    lang_detected: int = 0
    hints: list = field(default_factory=list)      # @username из ссылок и пересылок
    fwd_ents: list = field(default_factory=list)   # каналы, из которых пересылали (сразу entity)
    from_cache: bool = False
    # заполняется в evaluate()
    requests: int = 0
    product_msgs: int = 0                          # сообщений, где речь о товаре клиента
    product_share: float = 1.0                     # их доля (1 — слов товара нет, не штрафуем)
    y_est: float = 0.0                             # запросов по теме в неделю (оценка)
    topic_share: float = 0.0                       # доля сообщений по теме клиента
    terms: list = field(default_factory=list)
    junk: dict = field(default_factory=dict)
    reason: str = ""


def _lang_stats(texts: list, languages: set) -> tuple[str, float, int]:
    langs = Counter(l for l in (detect_lang(t) for t in texts) if l)
    total = sum(langs.values())
    if not total:
        return "", 0.0, 0
    top = langs.most_common(1)[0][0]
    ok = sum(n for l, n in langs.items() if l in languages)
    return top, ok / total, total


def _hints_from(raw: list, hints: list, fwd_ents: list) -> None:
    from telethon.tl.types import Channel

    seen = {h.lower().removeprefix("fwd:") for h in hints}
    for m in raw:
        text = getattr(m, "message", "") or ""
        for u in LINK_RE.findall(text) + [x[1:] for x in tp.MENTION_RE.findall(text)]:
            if u.lower() not in NOT_LINKS and not u.lower().endswith("bot") and u.lower() not in seen:
                seen.add(u.lower())
                hints.append(u)
        fwd = getattr(m, "forward", None)
        ch = getattr(fwd, "chat", None) if fwd is not None else None
        if isinstance(ch, Channel) and ch.username and ch.username.lower() not in seen:
            seen.add(ch.username.lower())
            hints.append(f"fwd:{ch.username}")   # пересылка — самая точная связь
            fwd_ents.append(ch)


async def probe_source(client, src, ent, params, store) -> Probe:
    """Читает немного сообщений (или берёт свежую пробу из каталога)."""
    from telethon.tl.types import User

    now = datetime.now(timezone.utc)
    langs = {x.strip() for x in params.languages.split(",") if x.strip()}
    cached = store.get_probe(src.chat_id, params.probe_ttl_hours)
    if cached is not None:
        since = now - timedelta(days=params.days)
        msgs = store.load_messages([src.chat_id], since)
        p = Probe(msgs=msgs, posts=store.load_posts([src.chat_id]), activity=cached["activity"],
                  lang=cached["lang"], lang_share=cached["lang_share"],
                  lang_detected=len(msgs), hints=list(cached["hints"]), from_cache=True)
        p.newest_age_days = min(((now - m.date).total_seconds() / 86400 for m in msgs), default=1e9)
        if not msgs and cached["activity"] >= params.min_activity:
            p.newest_age_days = 0.0  # сообщения старше окна, но чат живой по прошлой пробе
        return p

    p = Probe()
    raw_all: list = []
    authors: dict = {}
    if src.kind == "channel":
        posts = [m async for m in client.iter_messages(ent, limit=20, wait_time=params.wait_time)]
        raw_all += posts
        week = now - timedelta(days=7)
        recent = [x for x in posts if x.date >= week]
        replies = sum((getattr(getattr(x, "replies", None), "replies", 0) or 0) for x in recent)
        p.activity = replies / 7.0
        p.newest_age_days = min(((now - x.date).total_seconds() / 86400 for x in posts), default=1e9)
        for x in posts:
            if x.message:
                p.posts[(src.chat_id, x.id)] = x.message
        hot = sorted([x for x in posts if (getattr(getattr(x, "replies", None), "replies", 0) or 0) > 0],
                     key=lambda x: -(x.replies.replies or 0))[:3]
        for post in hot:
            try:
                async for c in client.iter_messages(ent, reply_to=post.id, limit=40,
                                                    wait_time=params.wait_time):
                    raw_all.append(c)
                    rec = to_message(src.chat_id, c, post_id=post.id)
                    if rec:
                        p.msgs.append(rec)
            except Exception:  # noqa: BLE001 — пост без обсуждения
                continue
        store.add_messages([r for r in (to_message(src.chat_id, x, is_post=True) for x in posts) if r])
    else:
        raw = [m async for m in client.iter_messages(ent, limit=params.probe_msgs,
                                                     wait_time=params.wait_time)]
        raw_all += raw
        p.msgs = [r for r in (to_message(src.chat_id, m) for m in raw) if r]
        if raw:
            oldest = min(m.date for m in raw)
            span = max((now - oldest).total_seconds() / 86400, 1 / 24)
            p.activity = len(raw) / span
            p.newest_age_days = min((now - m.date).total_seconds() / 86400 for m in raw)

    for m in raw_all:
        u = getattr(m, "sender", None)
        if isinstance(u, User) and u.id not in authors:
            authors[u.id] = author_from_user(u)
    store.add_messages(p.msgs)
    store.upsert_authors(authors.values())
    _hints_from(raw_all, p.hints, p.fwd_ents)
    texts = [m.text for m in p.msgs] + (list(p.posts.values()) if src.kind == "channel" else [])
    p.lang, p.lang_share, p.lang_detected = _lang_stats(texts, langs)
    return p


def evaluate(probes: dict, profile, lex: tp.Lexicon, params, store) -> None:
    """Плотность запросов по теме, термины для каталога, мусор и причина отказа."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=params.days)
    langs = [x.strip() for x in params.languages.split(",") if x.strip()]
    all_lem = []
    feats: dict = {}
    for sid, p in probes.items():
        for m in p.msgs:
            f = ft.extract(m.text, lex)
            feats[(sid, m.msg_id)] = f
            all_lem.append(f.lemmas)
    tfidf = TfIdf(all_lem + [profile_lemmas(profile)], lex.stopwords)
    pvec = profile_vector(tfidf, profile)
    core = core_terms(tfidf, profile)
    # слово товара совпадает только целиком (фраза в леммах), общие слова вроде «ai» не считаются
    product = product_matcher(profile)

    for sid, p in probes.items():
        authors = store.get_authors({m.sender_id for m in p.msgs})
        on_topic = 0
        requests = 0
        product_msgs = 0
        post_has: dict = {}
        terms: Counter = Counter()
        for m in p.msgs:
            f = feats[(sid, m.msg_id)]
            about_product = bool(product.find(f.lemmas)) if product else True
            if product and not about_product and m.post_id:
                key = (sid, m.post_id)
                if key not in post_has:
                    post_has[key] = bool(product.find(tp.lemmas(p.posts.get(key, ""))))
                about_product = post_has[key]        # комментарий под постом о товаре
            product_msgs += 1 if (about_product and product) else 0
            terms.update(t for t in f.lemmas if len(t) > 2 and t not in lex.stopwords and not t.isdigit())
            r = topic_score(tfidf, f.lemmas, pvec, core)
            if m.post_id and (f.question or f.intent_hits):
                post = p.posts.get((sid, m.post_id), "")
                if post:
                    r = max(r, 0.7 * topic_score(tfidf, tp.lemmas(post), pvec, core))
            if r >= REQUEST_R:
                on_topic += 1
            if (m.date >= since and m.sender_kind == "user" and r >= REQUEST_R and about_product
                    and ft.intent_rule(f) >= REQUEST_I and not ft.is_ad(f) and not lex.junk.find(f.lemmas)):
                requests += 1
        n = len(p.msgs)
        p.topic_share = on_topic / n if n else 0.0
        p.product_msgs = product_msgs
        p.product_share = product_msgs / n if (n and product) else 1.0
        p.requests = requests
        window = max(1.0, min(params.days, max((now - min((m.date for m in p.msgs), default=now))
                                                .total_seconds() / 86400, 1.0)))
        p.y_est = requests / window * 7
        p.terms = [t for t, _ in terms.most_common(60)]
        p.junk = content_metrics(p.msgs, authors, lex).get(sid, {}) if p.msgs else {}

        if p.lang_detected >= 10 and p.lang_share < params.min_lang_share:
            p.reason = (f"язык: в основном «{p.lang}», на {', '.join(langs)} только "
                        f"{p.lang_share:.0%} сообщений")
        elif p.newest_age_days > params.days:
            p.reason = f"мёртвый чат: последнее сообщение {p.newest_age_days:.0f} дн. назад"
        elif p.activity < params.min_activity:
            p.reason = f"мёртвый чат: {p.activity:.2f} сообщ. в день"
        elif p.junk and junk_verdict(p.junk, params):
            p.reason = junk_verdict(p.junk, params)
        elif product and n >= 10 and product_msgs == 0:
            p.reason = ("в чате не упоминается товар клиента ("
                        + ", ".join(list(profile.product_keywords)[:4]) + ")")
        elif n >= 10 and requests == 0 and p.topic_share < params.min_topic_share:
            p.reason = f"не по теме клиента: по теме {p.topic_share:.0%} сообщений, запросов нет"
