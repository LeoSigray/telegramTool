"""Профиль клиента: что он продаёт и как его покупатели пишут о потребности.

Строится из описания и постов канала клиента:
  • без ИИ: частые термины (по леммам, с отсевом стоп-слов);
  • с ИИ (1 вызов бесплатного LLM): оффер, аудитория, фразы покупателя,
    поисковые запросы, анти-портрет, типовые фразы продавцов-конкурентов.

Результат сохраняется в data/smart_profiles/<канал>.json. Его можно поправить
руками: следующий запуск возьмёт правленую версию (пока не указан --rebuild-profile).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field

from . import textproc as tp
from .llm_judge import llm_complete, parse_json_loose


@dataclass
class Profile:
    channel: str
    title: str = ""
    about: str = ""
    offer: str = ""
    audience: str = ""
    brief: str = ""
    topic_terms: list = field(default_factory=list)
    buyer_phrases: list = field(default_factory=list)
    search_queries: list = field(default_factory=list)
    anti: list = field(default_factory=list)
    seller_phrases: list = field(default_factory=list)
    community_queries: list = field(default_factory=list)  # где общаются покупатели (для поиска чатов)
    query_pool: list = field(default_factory=list)         # полный пул запросов запуска (порциями в раундах)
    queries_version: int = 0                               # версия генератора запросов
    product_keywords: list = field(default_factory=list)   # как покупатель называет сам товар/услугу
    audience_vocab: list = field(default_factory=list)     # частые слова из комментариев аудитории клиента
    keywords_checked: bool = False                         # слова и фразы проверены поиском в Telegram
    keyword_hits: dict = field(default_factory=dict)       # слово/фраза → сколько сообщений нашлось
    languages: list = field(default_factory=list)          # языки покупателей
    posts_sample: list = field(default_factory=list)
    built_with: str = "правила"

    def hash(self) -> str:
        core = json.dumps([self.offer, self.audience, self.topic_terms, self.anti],
                          ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(core.encode("utf-8")).hexdigest()[:12]

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "Profile":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _clean_list(items, limit: int, max_len: int = 80) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items or []:
        if not isinstance(x, str):
            continue
        s = re.sub(r"\s+", " ", x).strip().strip("«»\"'").strip()
        if not s or len(s) > max_len or s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
        if len(out) >= limit:
            break
    return out


def top_terms(docs: list[str], stopwords: set[str], limit: int = 30) -> list[str]:
    """Частые термины канала: леммы и биграммы, встречающиеся хотя бы в двух постах."""
    df: Counter = Counter()
    tf: Counter = Counter()
    for d in docs:
        lem = [t for t in tp.lemmas(d) if len(t) > 2 and t not in stopwords and not t.isdigit()]
        terms = lem + [a + " " + b for a, b in zip(lem, lem[1:])]
        tf.update(terms)
        df.update(set(terms))
    min_df = 2 if len(docs) >= 5 else 1
    scored = [(df[t] * math.log(1 + tf[t]) * (1.3 if " " in t else 1.0), t)
              for t in df if df[t] >= min_df]
    scored.sort(reverse=True)
    out: list[str] = []
    for _, t in scored:
        # биграмма вытесняет одиночные слова, которые в ней уже есть
        if " " not in t and any(t in o.split() for o in out if " " in o):
            continue
        out.append(t)
        if len(out) >= limit:
            break
    return out


def default_queries(terms: list[str]) -> list[str]:
    return _clean_list(terms, 10)


def default_buyer_phrases(terms: list[str]) -> list[str]:
    phrases = []
    for t in terms[:4]:
        phrases += [f"ищу {t}", f"нужен {t}", f"посоветуйте {t}", f"кто делает {t}"]
    return phrases


_PROFILE_PROMPT = """Ниже описание и посты Telegram-канала компании. Канал — витрина её услуг или продукта.
Задача: понять, что компания продаёт, и как её ПОТЕНЦИАЛЬНЫЕ КЛИЕНТЫ пишут о своей потребности в чатах.

Название канала: {title}
Описание канала: {about}
Бриф от владельца: {brief}

Посты (фрагменты):
{posts}

Верни СТРОГО JSON-объект без markdown:
{{
  "offer": "что продаёт компания, одно предложение",
  "audience": "кто покупатель: роль, тип и размер бизнеса, одно предложение",
  "topic_terms": ["15-30 ключевых слов и коротких фраз предметной области"],
  "buyer_phrases": ["12-20 коротких фраз, как покупатель пишет запрос в чате, например: ищу подрядчика на ..., посоветуйте сервис для ..."],
  "search_queries": ["8-12 запросов из 1-3 слов по теме: товары, услуги, ниша"],
  "community_queries": ["до 50 запросов из 1-3 слов для поиска ЧАТОВ, где общаются ПОКУПАТЕЛИ (не продавцы): по роли покупателя, его бизнесу, площадкам и профессиональным сообществам. Пример для CRM-интегратора: чат предпринимателей, селлеры wildberries, владельцы интернет-магазинов, маркетологи чат. Запрещено называть сам товар, бренды и технологии продукта: покупатели обычно не называют так свои чаты"],
  "product_keywords": ["8-15 отдельных слов, которыми покупатель называет САМ ТОВАР или услугу, в разных написаниях (кириллицей и латиницей), например: айфон, iphone, макбук, macbook, apple, смартфоны, электроника"],
  "languages": ["коды языков покупателей, например ru"],
  "anti": ["3-8 пунктов: кого НЕ считать клиентом"],
  "seller_phrases": ["5-10 фраз, которыми в этой нише рекламируют себя исполнители-конкуренты"]
}}"""


async def build_profile(client, params, lex: tp.Lexicon, log, use_llm: bool) -> Profile:
    from telethon.tl.functions.channels import GetFullChannelRequest

    ent = await client.get_entity(params.channel)
    title = getattr(ent, "title", "") or ""
    about = ""
    try:
        full = await client(GetFullChannelRequest(ent))
        about = full.full_chat.about or ""
    except Exception as e:  # noqa: BLE001
        log.warn("профиль", f"не удалось получить описание канала: {e}")

    posts: list[str] = []
    async for m in client.iter_messages(ent, limit=params.profile_posts, wait_time=params.wait_time):
        if m.message and m.message.strip():
            posts.append(m.message.strip())
    log.info("профиль", f"канал «{title}»: описание {len(about)} симв., постов с текстом {len(posts)}")

    brief = params.brief_text()
    terms = top_terms(posts + [about, brief], lex.stopwords)
    prof = Profile(
        channel=params.channel, title=title, about=about, brief=brief,
        topic_terms=terms,
        search_queries=default_queries(terms),
        buyer_phrases=default_buyer_phrases(terms),
        posts_sample=[p[:600] for p in posts[:30]],
    )

    if use_llm:
        flat = [re.sub(r"\s+", " ", p)[:400] for p in posts[:25]]
        sample = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(flat))
        prompt = _PROFILE_PROMPT.format(title=title, about=about or "—", brief=brief or "—",
                                        posts=sample or "—")
        try:
            data = parse_json_loose(await llm_complete(prompt, max_tokens=2500))
            if not isinstance(data, dict):
                raise ValueError("ожидался JSON-объект")
            prof.offer = str(data.get("offer", "") or "")[:300]
            prof.audience = str(data.get("audience", "") or "")[:300]
            prof.topic_terms = _clean_list(list(data.get("topic_terms") or []) + terms, 40)
            prof.buyer_phrases = _clean_list(data.get("buyer_phrases"), 20) or prof.buyer_phrases
            prof.search_queries = _clean_list(data.get("search_queries"), 12, 40) or prof.search_queries
            prof.anti = _clean_list(data.get("anti"), 8, 120)
            prof.seller_phrases = _clean_list(data.get("seller_phrases"), 10)
            prof.community_queries = _clean_list(data.get("community_queries"), 50, 40)
            prof.queries_version = QUERIES_VERSION
            prof.product_keywords = _clean_list(data.get("product_keywords"), 20, 25)
            prof.languages = _clean_list(data.get("languages"), 3, 5)
            prof.built_with = "LLM + правила"
            log.info("профиль", f"LLM: оффер «{prof.offer[:80]}», фраз покупателя "
                               f"{len(prof.buyer_phrases)}, запросов {len(prof.search_queries)}")
        except Exception as e:  # noqa: BLE001
            log.warn("профиль", f"LLM не помог ({str(e)[:150]}); профиль собран правилами")

    log.info("профиль", f"термины: {', '.join(prof.topic_terms[:12])}")
    return prof


QUERIES_VERSION = 3   # поднимается при смене генератора: старые пулы запросов пересоздаются


def reset_if_outdated(prof: Profile, log) -> bool:
    """Пулы запросов и слова товара, созданные старым генератором, создаются заново."""
    if prof.queries_version >= QUERIES_VERSION:
        return False
    prof.community_queries, prof.buyer_phrases, prof.product_keywords = [], [], []
    prof.keywords_checked, prof.keyword_hits = False, {}
    prof.queries_version = QUERIES_VERSION
    log.info("профиль", "генератор ключевых слов обновился — слова и запросы создаются заново")
    return True

_COMMON_BLOCK = """Компания продаёт: {offer}
Её покупатели: {audience}
Тема: {terms}
Язык покупателей: {langs}.
Так пишут её реальные покупатели (частые слова из их комментариев): {vocab}
"""

_REAL_WORDS = """ГЛАВНОЕ ПРАВИЛО: только слова и фразы, которые живые люди РЕАЛЬНО пишут в переписке.
Нельзя выдумывать термины и маркетинговые неологизмы («нейрокурсы», «AI-онбординг», «нейрообучение»),
нельзя склеивать слова через дефис, нельзя смешивать языки. Лучше простое и частое, чем красивое и редкое.
"""

_QUERIES_PROMPT = _COMMON_BLOCK + """
Нужно найти в Telegram как можно больше ЧАТОВ и ГРУПП, где общаются именно ПОКУПАТЕЛИ этой компании (не продавцы и не конкуренты).
Поиск Telegram ищет только по НАЗВАНИЯМ чатов и плохо работает с длинными запросами, поэтому нужны ОДНО-ДВУХСЛОВНЫЕ названия.
""" + _REAL_WORDS + """

ВАЖНО: покупатели называют свои чаты по СВОЕЙ роли и бизнесу, а не по товару, который покупают.
ЗАПРЕЩЕНО использовать название товара, бренды и технологии продукта (iPhone, MacBook, CRM и т.п.).
Пиши так: роль и бизнес покупателя (селлеры, байеры, закупщики, перекупы, владельцы магазинов, предприниматели, ИП), площадки и сообщества (маркетплейсы, Wildberries, Ozon, Авито), форматы названий (чат, клуб, сообщество, форум, оптовики).

Дай ровно {n} РАЗНЫХ запросов из 1-2 слов, охватив разные углы.
{avoid}
Верни СТРОГО JSON-массив из {n} строк без markdown."""

_PHRASES_PROMPT = _COMMON_BLOCK + """
Нужны КОРОТКИЕ ФРАЗЫ (2-3 слова), которыми покупатель пишет запрос в Telegram-чате, когда ему нужен этот товар или услуга. По ним Telegram будет искать сообщения во всех публичных чатах: длинная фраза почти никогда не находится, поэтому 2-3 простых слова.
Хорошо: «ищу курс нейросети», «посоветуйте курс midjourney», «куплю айфон оптом», «нужен сайт», «кто делает сайты».
Плохо: «где обучают нейросетям для дизайнеров интерьера онлайн», «занимаетесь ли вы нейроробототехникой».
""" + _REAL_WORDS + """
{avoid}
Дай ровно {n} фраз. Верни СТРОГО JSON-массив из {n} строк без markdown."""

_GENERIC_WORDS = {"чат", "chat", "группа", "клуб", "сообщество", "форум", "канал", "опт", "оптом",
                  "для", "по", "и", "в", "на", "от", "из", "the", "for"}


def _fallback_queries(prof: Profile, used: set, n: int) -> list:
    """Без LLM: шаблоны вокруг терминов клиента. Детерминированно, без повторов."""
    base = list(prof.topic_terms[:12]) + list(prof.search_queries[:8])
    templates = ["{t} чат", "{t} сообщество", "{t} клуб", "{t} обсуждение", "{t} форум",
                 "{t} предприниматели", "{t} закупки", "чат {t}"]
    out: list = []
    seen = set(used)
    for tpl in templates:
        for t in base:
            q = " ".join(tpl.format(t=t).split())
            if q.lower() not in seen and len(q) <= 40:
                seen.add(q.lower())
                out.append(q)
                if len(out) >= n:
                    return out
    return out


def _fallback_phrases(prof: Profile, used: set, n: int) -> list:
    out: list = []
    seen = set(used)
    for tpl in ("ищу {t}", "где купить {t}", "нужен {t}", "кто продаёт {t}", "посоветуйте {t}", "куплю {t}"):
        for t in prof.topic_terms[:14]:
            q = tpl.format(t=t)
            if q.lower() not in seen and len(q) <= 50:
                seen.add(q.lower())
                out.append(q)
                if len(out) >= n:
                    return out
    return out


async def generate_queries(prof: Profile, use_llm: bool, log, used: set, good_titles: list,
                           n: int = 50, langs: str = "ru", kind: str = "chats") -> list:
    """Новая порция из n запросов. kind: chats — названия чатов, phrases — фразы покупателя.
    Не повторяет used. good_titles — названия найденных удачных чатов (подсказка нейросети)."""
    fresh: list = []
    if use_llm and (prof.offer or prof.topic_terms):
        avoid = ""
        if used:
            avoid += "Уже использованы, НЕ повторяй: " + "; ".join(sorted(used)[:80]) + ".\n"
        if good_titles:
            avoid += "Удачные найденные чаты (ищи похожие по духу): " + "; ".join(good_titles[:12]) + ".\n"
        prompt = (_PHRASES_PROMPT if kind == "phrases" else _QUERIES_PROMPT).format(
            offer=prof.offer or "—", audience=prof.audience or "—",
            terms=", ".join(prof.topic_terms[:20]) or "—", langs=langs or "ru", n=n, avoid=avoid,
            vocab=", ".join(prof.audience_vocab[:40]) or "—")
        try:
            data = parse_json_loose(await llm_complete(prompt, max_tokens=3000))
            items = data if isinstance(data, list) else (data.get("queries") if isinstance(data, dict) else [])
            fresh = _clean_list(items, n * 2, 50 if kind == "phrases" else 40)
        except Exception as e:  # noqa: BLE001
            log.warn("профиль", f"LLM не дал запросы ({str(e)[:120]})")
    seen = set(used)
    out: list = []
    for q in fresh:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    if len(out) < n:
        out += (_fallback_phrases if kind == "phrases" else _fallback_queries)(prof, seen, n - len(out))
    return out[:n]


async def ensure_query_pool(prof: Profile, use_llm: bool, log, params) -> bool:
    """Пулы на запуск: названия чатов и фразы покупателя, до queries_target / phrases_target.
    Пулы старой версии генератора пересоздаются. Возвращает True, если профиль изменился."""
    changed = False
    if len(prof.community_queries) < params.queries_target:
        have = {q.lower() for q in prof.community_queries}
        extra = await generate_queries(prof, use_llm, log, have, [],
                                       params.queries_target - len(prof.community_queries),
                                       params.languages, "chats")
        prof.community_queries = _clean_list(prof.community_queries + extra, params.queries_target, 40)
        changed = True
    if params.search_messages and len(prof.buyer_phrases) < params.phrases_target:
        have = {q.lower() for q in prof.buyer_phrases}
        extra = await generate_queries(prof, use_llm, log, have, [],
                                       params.phrases_target - len(prof.buyer_phrases),
                                       params.languages, "phrases")
        prof.buyer_phrases = _clean_list(prof.buyer_phrases + extra, params.phrases_target, 50)
        changed = True
    log.info("профиль", f"пулы запросов: названий чатов {len(prof.community_queries)}, "
                        f"фраз покупателя {len(prof.buyer_phrases)} ({'нейросеть' if use_llm else 'шаблоны'})")
    log.info("профиль", "названия чатов: " + ", ".join(prof.community_queries[:10])
             + (" …" if len(prof.community_queries) > 10 else ""))
    log.info("профиль", "фразы покупателя: " + "; ".join(prof.buyer_phrases[:5])
             + (" …" if len(prof.buyer_phrases) > 5 else ""))
    return changed


_KEYWORDS_PROMPT = """Компания продаёт: {offer}
Тема: {terms}
Название канала: {title}
Так пишут её реальные покупатели (частые слова из их комментариев): {vocab}

Какими словами покупатель называет САМ ТОВАР или услугу в обычной переписке в чатах?
Нужны разные написания: кириллицей и латиницей, бренды, названия инструментов, разговорные варианты.
Хорошо (для курсов по нейросетям): нейросеть, нейросети, chatgpt, midjourney, промпт, ии.
Хорошо (для оптовой техники): айфон, iphone, макбук, macbook, apple, техника.
Плохо: нейрокурсы, AI-онбординг, нейрообучение, web-курсы — так никто не пишет.
""" + _REAL_WORDS + """
Верни СТРОГО JSON-массив из 8-15 слов (по одному слову, иногда два) без markdown.
Без общих слов вроде «цена», «оптом», «доставка», «курс», «онлайн»."""

_GENERIC_KW = {"оптом", "опт", "оптовый", "цена", "доставка", "заказ", "купить", "продажа", "поставщик",
               "магазин", "товар", "услуга", "компания", "платформа", "логистика", "закупка",
               "ai", "ии", "курс", "курсы", "онлайн", "web", "бизнес", "обучение", "интенсив",
               "практикум", "старт", "программа", "школа", "проект", "сервис"}


def clean_product_keywords(words) -> list:
    """Слова товара без общих слов и без склеек через дефис («AI-онбординг» никто не пишет)."""
    out = []
    for w in words or []:
        w = " ".join(str(w).split()).strip().lower()
        if not w or "-" in w or w in _GENERIC_KW or len(w) < 2:
            continue
        if w not in out:
            out.append(w)
    return out


async def ensure_product_keywords(prof: Profile, use_llm: bool, log, lex_stop: set) -> bool:
    """Слова самого товара нужны, чтобы отличить чат, где говорят о товаре клиента, от чата про
    логистику и комиссии маркетплейса. Старым профилям дописываются одним вызовом LLM, без него
    берутся из названия, описания и оффера. Возвращает True, если профиль изменился."""
    if prof.product_keywords:
        return False
    found: list = []
    if use_llm and (prof.offer or prof.topic_terms or prof.title):
        prompt = _KEYWORDS_PROMPT.format(offer=prof.offer or "—", title=prof.title or "—",
                                         terms=", ".join(prof.topic_terms[:20]) or "—",
                                         vocab=", ".join(prof.audience_vocab[:40]) or "—")
        try:
            data = parse_json_loose(await llm_complete(prompt, max_tokens=800))
            found = _clean_list(data if isinstance(data, list) else [], 20, 25)
        except Exception as e:  # noqa: BLE001
            log.warn("профиль", f"LLM не дал слова товара ({str(e)[:100]})")
    if not found:
        text = " ".join([prof.title, prof.about, prof.offer])
        found = [w for w in tp.words(text) if len(w) >= 4 and w not in lex_stop and w not in _GENERIC_KW]
        found = _clean_list(list(dict.fromkeys(found)), 12, 25)
    prof.product_keywords = clean_product_keywords(found)
    found = prof.product_keywords
    log.info("профиль", "слова товара: " + ", ".join(found[:12]) if found
             else "слова товара определить не удалось")
    return bool(found)
