"""Поиск чатов через YouTube: обучающие видео → Telegram-каналы их авторов.

Авторы обучающих видео почти всегда ставят в описание ссылку на свой Telegram-канал (часто
с курсами, обсуждением и чатом учеников). Зрители таких видео — те, кто учится теме клиента,
то есть его возможные покупатели. Поэтому:
  1. нейросеть пишет запросы, по которым покупатели клиента ищут обучающие видео
     («как …», «урок …», «… для начинающих»);
  2. выдача YouTube (без ключа: страница поиска) → видео → описание видео;
  3. из описания берём ссылки t.me/…; если в описании ссылок нет — смотрим ссылки на странице
     «О канале» автора (один раз на автора);
  4. очки Telegram-канала — число РАЗНЫХ авторов YouTube, которые на него ссылаются
     (один автор — один голос, сколько бы видео у него ни было).
Дальше как обычно: проверка в Telegram, проба, граф от зёрен (из канала автора граф находит
чат курса по ссылкам и пересылкам). Ссылки-приглашения t.me/+… не используем: в чаты не вступаем.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from collections import defaultdict

from .websearch import HEADERS, WebBlocked, WebFinder, _usable

YT = "https://www.youtube.com"
COOKIES = {"CONSENT": "YES+1", "SOCS": "CAI"}     # без них в части стран — страница согласия
VIDEOS_ONLY = "EgIQAQ%3D%3D"                       # фильтр выдачи «только видео»
VIDEO_ID_RE = re.compile(r'"videoId":"([\w-]{11})"')
DESC_RE = re.compile(r'"shortDescription":"((?:[^"\\]|\\.)*)"')
OWNER_RE = re.compile(r'"ownerChannelName":"((?:[^"\\]|\\.)*)"')
CHANNEL_ID_RE = re.compile(r'"channelId":"(UC[\w-]{22})"')
VIEWS_RE = re.compile(r'"viewCount":"(\d+)"')
# в описаниях часто пишут без https://: «t.me/канал», «telegram.me/канал/123»
TG_LINK_RE = re.compile(r"(?<![\w.])(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?"
                        r"([A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_])", re.I)
INVITE_RE = re.compile(r"(?:t|telegram)\.me/(?:\+|joinchat/)[\w-]+", re.I)


def _unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s


def links_in(text: str) -> list:
    """Имена Telegram из текста (описание видео, страница канала), без ботов и служебных."""
    out = []
    for m in TG_LINK_RE.finditer(text):
        n = m.group(1).lower()
        if _usable(n) and n not in out:
            out.append(n)
    return out


def parse_video(html: str) -> dict:
    """Автор, его id, просмотры и описание со страницы видео."""
    d = DESC_RE.search(html)
    o = OWNER_RE.search(html)
    c = CHANNEL_ID_RE.search(html)
    v = VIEWS_RE.search(html)
    return {"desc": _unescape(d.group(1)) if d else "", "author": _unescape(o.group(1)) if o else "",
            "channel_id": c.group(1) if c else "", "views": int(v.group(1)) if v else 0}


class YouTubeFinder(WebFinder):
    """Как WebFinder (кеш, очки за весь запуск), только источник — YouTube."""

    label = "YouTube"

    def __init__(self, store, params, log) -> None:
        super().__init__(store, params, log)
        self.authors: dict = defaultdict(dict)     # tg-имя → {channel_id: (автор, видео, просмотры)}
        self.videos_seen: set = set()
        self.invites = 0

    async def _get(self, http, url: str, **params) -> str:
        r = await http.get(url, params=params or None)
        if r.status_code == 429 or "consent.youtube.com" in str(r.url):
            raise WebBlocked(f"YouTube ответил {r.status_code}")
        r.raise_for_status()
        return r.text

    async def _search_videos(self, http, q: str) -> list:
        key = f"yt:q:{q.lower()}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached["ids"]
        html = await self._get(http, f"{YT}/results", search_query=q, sp=VIDEOS_ONLY)
        ids = list(dict.fromkeys(VIDEO_ID_RE.findall(html)))[:self.params.yt_videos]
        self._cache_put(key, {"ids": ids})
        await asyncio.sleep(random.uniform(1.5, 3.0))
        return ids

    async def _video(self, http, vid: str) -> dict:
        key = f"yt:v:{vid}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached["video"]
        info = parse_video(await self._get(http, f"{YT}/watch", v=vid))
        info["links"] = links_in(info["desc"])
        info["invites"] = len(INVITE_RE.findall(info["desc"]))
        info["desc"] = info["desc"][:600]
        self._cache_put(key, {"video": info})
        return info

    async def _channel_links(self, http, channel_id: str) -> list:
        """Ссылки со страницы «О канале» автора — когда в описании видео их нет."""
        key = f"yt:c:{channel_id}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached["links"]
        try:
            html = await self._get(http, f"{YT}/channel/{channel_id}/about")
            # ссылки на странице канала лежат в JSON как текст «t.me/…» или в redirect-адресах
            text = html.replace("\\u002F", "/").replace("%2F", "/").replace("%3A", ":")
            links = links_in(text)
        except Exception:  # noqa: BLE001
            links = []
        self._cache_put(key, {"links": links})
        return links

    async def run(self, queries: list) -> list:
        import httpx

        touched: set = set()
        n_videos = n_links = 0
        sem = asyncio.Semaphore(3)
        async with httpx.AsyncClient(headers=HEADERS, cookies=COOKIES, timeout=20,
                                     follow_redirects=True) as http:
            checked_channels: set = set()
            for q in queries:
                try:
                    ids = await self._search_videos(http, q)
                except Exception as e:  # noqa: BLE001
                    self.blocked = True
                    self.log.warn("YouTube", f"поиск недоступен ({str(e)[:80]}) — в этом раунде остановлен")
                    break
                ids = [v for v in ids if v not in self.videos_seen]
                self.videos_seen.update(ids)

                async def one(vid):
                    async with sem:
                        try:
                            info = await self._video(http, vid)
                        except Exception as e:  # noqa: BLE001
                            self.log.info("YouTube", f"видео {vid} не открылось ({str(e)[:60]})")
                            return None
                        await asyncio.sleep(random.uniform(0.5, 1.2))
                        return vid, info
                for res in await asyncio.gather(*(one(v) for v in ids)):
                    if res is None:
                        continue
                    vid, info = res
                    n_videos += 1
                    self.invites += info.get("invites", 0)
                    links, cid = info["links"], info["channel_id"]
                    if not links and cid and cid not in checked_channels:
                        checked_channels.add(cid)
                        links = await self._channel_links(http, cid)
                    for name in links:
                        key = cid or info["author"] or vid       # один автор — один голос
                        if key in self.authors[name]:
                            continue
                        self.authors[name][key] = (info["author"], vid, info["views"])
                        self.sites[name][key] = (1.0, f"{YT}/watch?v={vid}",
                                                 f"{info['author']}: {info['desc'][:200]}")
                        self.score[name] = float(len(self.authors[name]))
                        touched.add(name)
                        n_links += 1
        ranked = sorted(touched, key=lambda n: (-self.score[n], -self._views(n)))
        self.log.info("YouTube", f"запросов {len(queries)}, видео {n_videos}, ссылок на Telegram "
                                 f"{n_links}, разных каналов {len(ranked)}"
                      + (f", закрытых приглашений (не берём) {self.invites}" if self.invites else "")
                      + ("; чаще всего: " + ", ".join(f"@{n} ({int(self.score[n])} авт.)" for n in ranked[:6])
                         if ranked else ""))
        return ranked

    def _views(self, name: str) -> int:
        return max((v[2] for v in self.authors.get(name, {}).values()), default=0)

    def via(self, name: str) -> str:
        names = [v[0] for v in self.authors.get(name, {}).values() if v[0]]
        return f"YouTube ({len(self.authors.get(name, {}))} авт.): " + ", ".join(names[:3])


# ── запросы для YouTube ─────────────────────────────────────────────────────

_YT_PROMPT = """Компания продаёт: {offer}
Её покупатели: {audience}
Тема: {terms}
Так пишут её реальные покупатели: {vocab}

Нужно найти на YouTube ОБУЧАЮЩИЕ видео, которые смотрят ПОКУПАТЕЛИ этой компании: уроки, разборы,
инструкции, «как сделать», обзоры для начинающих и практиков. Авторы таких видео ссылаются на свои
Telegram-каналы, где собирается та же аудитория.

Пиши запросы так, как их вбивает в поиск YouTube сам покупатель, на языке: {langs}. Варианты:
«как …», «… урок», «… для начинающих», «… обучение», «… пошагово», «… разбор», «… 2026».
Описывай задачи и интересы покупателя, а не товар компании. Не выдумывай слова, 2–6 слов в запросе.
{avoid}
Дай ровно {n} РАЗНЫХ запросов. Верни СТРОГО JSON-массив строк без markdown."""


def _fallback_yt(prof, seen: set, n: int) -> list:
    terms = list((prof.topic_terms or [])[:6]) or [prof.title or prof.channel]
    tmpl = ["{t} обучение", "{t} урок для начинающих", "как {t}", "{t} пошагово", "{t} разбор"]
    out = []
    for tp_ in tmpl:
        for t in terms:
            q = tp_.format(t=t)
            if q.lower() not in seen:
                seen.add(q.lower())
                out.append(q)
    return out[:n]


async def generate_yt_queries(prof, use_llm: bool, log, used: set, n: int, langs: str = "ru",
                              good_titles: list | None = None) -> list:
    from .llm_judge import llm_complete, parse_json_loose
    from .profile import _clean_list

    fresh: list = []
    if use_llm and (prof.offer or prof.topic_terms):
        avoid = ""
        if used:
            avoid += "Уже использованы, НЕ повторяй: " + "; ".join(sorted(used)[:60]) + ".\n"
        if good_titles:
            avoid += "Уже найдены удачные Telegram-каналы (ищи похожие темы): " + "; ".join(good_titles[:12]) + ".\n"
        prompt = _YT_PROMPT.format(offer=prof.offer or "—", audience=prof.audience or "—",
                                   terms=", ".join(prof.topic_terms[:20]) or "—",
                                   vocab=", ".join(getattr(prof, "audience_vocab", [])[:30]) or "—",
                                   langs=langs or "ru", n=n, avoid=avoid)
        try:
            data = parse_json_loose(await llm_complete(prompt, max_tokens=1500))
            fresh = _clean_list(data if isinstance(data, list) else [], n * 2, 80)
        except Exception as e:  # noqa: BLE001
            log.warn("YouTube", f"LLM не дал запросы для YouTube ({str(e)[:120]})")
    seen = {u.lower() for u in used}
    out = []
    for q in fresh:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    if len(out) < n:
        out += _fallback_yt(prof, seen, n - len(out))
    return out[:n]


async def ensure_yt_queries(prof, use_llm: bool, log, params) -> bool:
    if len(prof.yt_queries) >= params.web_queries_target:
        return False
    have = {q.lower() for q in prof.yt_queries}
    prof.yt_queries = list(prof.yt_queries) + await generate_yt_queries(
        prof, use_llm, log, have, params.web_queries_target - len(prof.yt_queries), params.languages)
    log.info("YouTube", f"запросов для YouTube {len(prof.yt_queries)}: "
                        + "; ".join(prof.yt_queries[:5]) + (" …" if len(prof.yt_queries) > 5 else ""))
    return True
