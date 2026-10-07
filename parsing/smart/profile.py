"""Профиль клиента: что он продаёт и как его покупатели пишут о потребности.

Строится из описания и постов канала клиента:
  • без ИИ: частые термины (по леммам, с отсевом стоп-слов), хэштеги, ссылки
    на другие чаты и каналы (затравки для поиска источников);
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

SEED_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_/])",
                     re.IGNORECASE)
_NOT_SEEDS = {"joinchat", "addlist", "share", "proxy", "socks", "addstickers", "iv", "s", "c"}


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
    seeds: list = field(default_factory=list)
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


def extract_seeds(texts: list[str], own: str) -> list[str]:
    seeds: list[str] = []
    seen = {own.lower()} if own else set()
    for t in texts:
        for m in SEED_RE.findall(t or ""):
            u = m.lower()
            if u in _NOT_SEEDS or u in seen:
                continue
            seen.add(u)
            seeds.append(m)
        for m in tp.MENTION_RE.findall(t or ""):
            u = m[1:].lower()
            if u in seen or u.endswith("bot"):
                continue
            seen.add(u)
            seeds.append(m[1:])
    return seeds


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
  "search_queries": ["8-12 запросов из 1-3 слов для поиска профильных чатов и каналов, где сидят покупатели"],
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
        seeds=extract_seeds(posts + [about], own=params.channel),
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
            prof.built_with = "LLM + правила"
            log.info("профиль", f"LLM: оффер «{prof.offer[:80]}», фраз покупателя "
                               f"{len(prof.buyer_phrases)}, запросов {len(prof.search_queries)}")
        except Exception as e:  # noqa: BLE001
            log.warn("профиль", f"LLM не помог ({str(e)[:150]}); профиль собран правилами")

    log.info("профиль", f"термины: {', '.join(prof.topic_terms[:12])}")
    log.info("профиль", f"затравок из постов (ссылки и @упоминания): {len(prof.seeds)}")
    return prof
