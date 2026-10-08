"""Поиск чатов через интернет: статьи, подборки и обсуждения, где люди советуют чаты.

Вместо поиска Telegram по названиям («селлеры чат» выдаёт всё подряд, включая мусор):
  1. нейросеть пишет запросы для поисковика в духе «лучшие телеграм-чаты для …»,
     «где общаются … телеграм», «подборка чатов …»;
  2. DuckDuckGo (бесплатно, без ключа) отдаёт ссылки на статьи и подборки;
  3. со страниц берём ссылки t.me/… (и @упоминания, tgstat), с текстом вокруг;
  4. чат, который советуют несколько разных сайтов, весит больше — это живой отбор людьми.
Дальше кандидаты идут в обычную проверку (язык, активность, мусор, товар клиента): статья —
подсказка, а не гарантия, в подборках много рекламы и мёртвых чатов.

Результаты поиска и страниц кешируются в SQLite (web_ttl_days), повторный запуск сеть не трогает.
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .store import iso, parse_iso

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"}
MAX_PAGE = 3_000_000

TME_RE = re.compile(r"(?:https?:)?//(?:www\.)?(?:t|telegram)\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})"
                    r"(?![A-Za-z0-9_])", re.I)
TGSTAT_RE = re.compile(r"tgstat\.(?:ru|com)/(?:[a-z]{2}/)?(?:channel|chat)/@([A-Za-z][A-Za-z0-9_]{3,31})", re.I)
AT_RE = re.compile(r"(?<![\w@./])@([A-Za-z][A-Za-z0-9_]{4,31})(?![\w.@])")
TAG_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
# служебные адреса t.me и частые не-чаты
SKIP = {"share", "joinchat", "addstickers", "addlist", "addemoji", "proxy", "socks", "iv", "login",
        "setlanguage", "boost", "contact", "telegram", "durov", "gmail", "mail", "yandex",
        "username", "channel", "chat", "example", "media", "import", "keyframes", "font"}
W_LINK, W_TGSTAT, W_AT = 1.0, 1.0, 0.4    # ссылка надёжнее голого @упоминания (это может быть автор)


class WebBlocked(Exception):
    """Поисковик не отвечает или требует капчу."""


def _norm(name: str) -> str:
    return name.lower()


def _usable(name: str) -> bool:
    n = name.lower()
    return n not in SKIP and not n.endswith("bot") and not n.isdigit()


def _plain(html: str) -> str:
    text = TAG_RE.sub(" ", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&laquo;", "«"),
                 ("&raquo;", "»"), ("&mdash;", "—"), ("&ndash;", "–")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text)


def extract_chats(html: str) -> dict:
    """username → (вес, текст вокруг). Ссылки t.me и tgstat — вес 1, @упоминания — 0.4."""
    out: dict = {}

    def put(name, w, ctx):
        if not _usable(name):
            return
        k = _norm(name)
        if k not in out or out[k][0] < w:
            out[k] = (w, ctx)
    for rx, w in ((TME_RE, W_LINK), (TGSTAT_RE, W_TGSTAT)):
        for m in rx.finditer(html):
            ctx = _plain(html[max(0, m.start() - 400):m.end() + 200])[-220:]
            put(m.group(1), w, ctx.strip())
    text = _plain(html)
    for m in AT_RE.finditer(text):
        put(m.group(1), W_AT, text[max(0, m.start() - 160):m.end() + 60].strip())
    return out


def _domain(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


async def ddg_search(http, query: str, n: int) -> list:
    """Ссылки из выдачи DuckDuckGo (HTML-версия, без ключа)."""
    r = await http.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "ru-ru"})
    if r.status_code != 200 or "anomaly" in r.text[:5000].lower():
        raise WebBlocked(f"DuckDuckGo ответил {r.status_code}")
    urls = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', r.text):
        u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
        if u.startswith("http") and "duckduckgo.com" not in u and u not in urls:
            urls.append(u)
    return urls[:n]


async def ddg_lite_search(http, query: str, n: int) -> list:
    r = await http.post("https://lite.duckduckgo.com/lite/", data={"q": query, "kl": "ru-ru"})
    if r.status_code != 200:
        raise WebBlocked(f"DuckDuckGo Lite ответил {r.status_code}")
    urls = []
    for href in re.findall(r'href="([^"]+)"[^>]*class=.result-link', r.text):
        u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
        if u.startswith("http") and "duckduckgo.com" not in u and u not in urls:
            urls.append(u)
    return urls[:n]


async def ddgs_lib_search(http, query: str, n: int) -> list:
    """Библиотека ddgs (pip install ddgs): сама перебирает несколько поисковиков."""
    from ddgs import DDGS

    def go():
        return [r.get("href") for r in DDGS().text(query, region="ru-ru", max_results=n) if r.get("href")]
    return await asyncio.to_thread(go)


async def brave_search(http, query: str, n: int) -> list:
    """Brave Search API — бесплатный тариф, ключ BRAVE_API_KEY в .env."""
    r = await http.get("https://api.search.brave.com/res/v1/web/search",
                       params={"q": query, "count": min(n, 20), "search_lang": "ru"},
                       headers={"X-Subscription-Token": os.getenv("BRAVE_API_KEY", "").strip(),
                                "Accept": "application/json"})
    if r.status_code != 200:
        raise WebBlocked(f"Brave API ответил {r.status_code}")
    return [x["url"] for x in (r.json().get("web") or {}).get("results", []) if x.get("url")][:n]


async def google_cse_search(http, query: str, n: int) -> list:
    """Google Programmable Search — 100 запросов в день бесплатно, ключи GOOGLE_CSE_KEY и GOOGLE_CSE_CX."""
    r = await http.get("https://www.googleapis.com/customsearch/v1",
                       params={"key": os.getenv("GOOGLE_CSE_KEY", "").strip(),
                               "cx": os.getenv("GOOGLE_CSE_CX", "").strip(),
                               "q": query, "num": min(n, 10), "hl": "ru"})
    if r.status_code != 200:
        raise WebBlocked(f"Google CSE ответил {r.status_code}")
    return [x["link"] for x in r.json().get("items", []) if x.get("link")][:n]


def search_backends() -> list:
    """Цепочка поисковиков по надёжности: с ключом (если задан) → ddgs → DuckDuckGo без ключа."""
    out = []
    if os.getenv("GOOGLE_CSE_KEY", "").strip() and os.getenv("GOOGLE_CSE_CX", "").strip():
        out.append(("Google", google_cse_search))
    if os.getenv("BRAVE_API_KEY", "").strip():
        out.append(("Brave", brave_search))
    try:
        import ddgs  # noqa: F401
        out.append(("ddgs", ddgs_lib_search))
    except ImportError:
        pass
    out += [("DuckDuckGo", ddg_search), ("DuckDuckGo Lite", ddg_lite_search)]
    return out


class WebFinder:
    """Поиск кандидатов в интернете с накоплением очков за весь запуск."""

    def __init__(self, store, params, log) -> None:
        self.store, self.params, self.log = store, params, log
        self.score: dict = defaultdict(float)      # username → сумма весов по разным сайтам
        self.sites: dict = defaultdict(dict)       # username → {домен: (вес, url, контекст)}
        self.blocked = False
        self.pages_seen: set = set()
        self.backends = search_backends()
        self.fails: dict = defaultdict(int)       # поисковик → отказов подряд (2 — больше не берём)

    def _cache_get(self, key: str):
        v = self.store.llm_get(key)
        if not v:
            return None
        try:
            if datetime.now(timezone.utc) - parse_iso(v["at"]) > timedelta(days=self.params.web_ttl_days):
                return None
        except (KeyError, ValueError):
            return None
        return v

    def _cache_put(self, key: str, data: dict) -> None:
        data["at"] = iso(datetime.now(timezone.utc))
        self.store.llm_put(key, data)

    async def _search(self, http, q: str) -> list:
        key = f"web:q:{q.lower()}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached["urls"]
        last = None
        for name, fn in self.backends:
            if self.fails[name] >= 2:
                continue
            for attempt in range(2):
                try:
                    urls = await fn(http, q, self.params.web_results)
                    self.fails[name] = 0
                    self._cache_put(key, {"urls": urls, "engine": name})
                    await asyncio.sleep(random.uniform(3.0, 6.0))
                    return urls
                except Exception as e:  # noqa: BLE001
                    last = f"{name}: {str(e)[:80]}"
                    if attempt == 0 and isinstance(e, WebBlocked) and name.startswith("DuckDuckGo"):
                        await asyncio.sleep(random.uniform(15.0, 25.0))   # временный лимит — ждём
                        continue
                    break
            self.fails[name] += 1
            self.log.info("интернет", f"{last} — пробую следующий поисковик")
        raise WebBlocked(last or "нет доступных поисковиков")

    async def _page(self, http, url: str) -> dict:
        key = f"web:p:{url}"
        cached = self._cache_get(key)
        if cached is not None:
            return {k: tuple(v) for k, v in cached["chats"].items()}
        chats: dict = {}
        try:
            async with http.stream("GET", url) as r:
                ctype = r.headers.get("content-type", "")
                if r.status_code == 200 and ("html" in ctype or "text" in ctype or not ctype):
                    body = b""
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_PAGE:
                            break
                    chats = extract_chats(body.decode(r.encoding or "utf-8", errors="ignore"))
        except Exception as e:  # noqa: BLE001 — сайт недоступен, таймаут, редиректы
            self.log.info("интернет", f"{_domain(url)}: страница не скачалась ({str(e)[:60]})")
            return {}
        self._cache_put(key, {"chats": {k: list(v) for k, v in chats.items()}})
        return chats

    async def run(self, queries: list) -> list:
        """Ищет по запросам, качает страницы, копит очки. Возвращает usernames, которые
        появились или выросли в этом раунде (по убыванию очков)."""
        import httpx

        touched: set = set()
        n_pages = n_links = 0
        sem = asyncio.Semaphore(4)
        async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as http:
            for q in queries:
                try:
                    urls = await self._search(http, q)
                except Exception as e:  # noqa: BLE001
                    self.blocked = True
                    self.log.warn("интернет", f"поисковик недоступен ({str(e)[:80]}) — поиск по "
                                              "статьям в этом раунде остановлен")
                    break
                urls = [u for u in urls if u not in self.pages_seen
                        and not _domain(u).endswith(("t.me", "telegram.me", "telegram.org"))]
                self.pages_seen.update(urls)

                async def one(u):
                    async with sem:
                        return u, await self._page(http, u)
                for url, chats in await asyncio.gather(*(one(u) for u in urls)):
                    n_pages += 1
                    dom = _domain(url)
                    for name, (w, ctx) in chats.items():
                        prev = self.sites[name].get(dom)
                        if prev is None or prev[0] < w:          # один сайт — один голос
                            self.sites[name][dom] = (w, url, ctx)
                            self.score[name] = sum(v[0] for v in self.sites[name].values())
                            touched.add(name)
                            n_links += 1
        ranked = sorted(touched, key=lambda n: -self.score[n])
        self.log.info("интернет", f"запросов {len(queries)}, страниц {n_pages}, упоминаний чатов "
                                  f"{n_links}, разных чатов {len(ranked)}"
                      + ("; чаще всего советуют: " + ", ".join(
                          f"@{n} ({len(self.sites[n])} сайт.)" for n in ranked[:6]) if ranked else ""))
        return ranked

    def via(self, name: str) -> str:
        doms = sorted(self.sites.get(name, {}), key=lambda d: -self.sites[name][d][0])
        return f"интернет ({len(doms)} сайт.): " + ", ".join(doms[:3])

    def context(self, name: str) -> str:
        best = max(self.sites.get(name, {}).values(), key=lambda v: v[0], default=None)
        return best[2] if best else ""


# ── запросы для поисковика ──────────────────────────────────────────────────

_WEB_PROMPT = """Компания продаёт: {offer}
Её покупатели: {audience}
Тема: {terms}
Так пишут её реальные покупатели: {vocab}

Нужно через обычный поисковик (Google/DuckDuckGo) найти СТАТЬИ, ПОДБОРКИ, КАТАЛОГИ и ОБСУЖДЕНИЯ на
форумах, где люди перечисляют и советуют Telegram-чаты и каналы с комментариями, в которых общаются
ПОКУПАТЕЛИ этой компании (не продавцы, не конкуренты, не боты).

Пиши запросы так, как их вбивает человек в поисковик, на языке: {langs}. Варианты формулировок:
«лучшие телеграм чаты для …», «телеграм чаты … подборка», «где общаются … в телеграм»,
«посоветуйте чат … телеграм», «сообщества … telegram», «каталог чатов …», а также с site:vc.ru,
site:habr.com, site:dzen.ru, site:pikabu.ru, site:tgstat.ru. Описывай аудиторию по её роли,
профессии, бизнесу и интересам, а не по товару компании. Не выдумывай слова.
{avoid}
Дай ровно {n} РАЗНЫХ запросов. Верни СТРОГО JSON-массив строк без markdown."""


def _fallback_web(prof, seen: set, n: int) -> list:
    roles = [t for t in (prof.topic_terms or [])[:6]] or [prof.title or prof.channel]
    tmpl = ["лучшие телеграм чаты {t} подборка", "где общаются {t} телеграм чат",
            "телеграм сообщества {t}", "site:vc.ru телеграм чаты {t}", "чаты {t} telegram каталог"]
    out = []
    for tp_ in tmpl:
        for t in roles:
            q = tp_.format(t=t)
            if q.lower() not in seen:
                seen.add(q.lower())
                out.append(q)
    return out[:n]


async def generate_web_queries(prof, use_llm: bool, log, used: set, n: int, langs: str = "ru",
                               good_titles: list | None = None) -> list:
    from .llm_judge import llm_complete, parse_json_loose
    from .profile import _clean_list

    fresh: list = []
    if use_llm and (prof.offer or prof.topic_terms):
        avoid = ""
        if used:
            avoid += "Уже использованы, НЕ повторяй: " + "; ".join(sorted(used)[:60]) + ".\n"
        if good_titles:
            avoid += "Уже найдены удачные чаты (ищи похожие): " + "; ".join(good_titles[:12]) + ".\n"
        prompt = _WEB_PROMPT.format(offer=prof.offer or "—", audience=prof.audience or "—",
                                    terms=", ".join(prof.topic_terms[:20]) or "—",
                                    vocab=", ".join(getattr(prof, "audience_vocab", [])[:30]) or "—",
                                    langs=langs or "ru", n=n, avoid=avoid)
        try:
            data = parse_json_loose(await llm_complete(prompt, max_tokens=2000))
            fresh = _clean_list(data if isinstance(data, list) else [], n * 2, 90)
        except Exception as e:  # noqa: BLE001
            log.warn("интернет", f"LLM не дал запросы для поисковика ({str(e)[:120]})")
    seen = {u.lower() for u in used}
    out = []
    for q in fresh:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    if len(out) < n:
        out += _fallback_web(prof, seen, n - len(out))
    return out[:n]


async def ensure_web_queries(prof, use_llm: bool, log, params) -> bool:
    if len(prof.web_queries) >= params.web_queries_target:
        return False
    have = {q.lower() for q in prof.web_queries}
    prof.web_queries = list(prof.web_queries) + await generate_web_queries(
        prof, use_llm, log, have, params.web_queries_target - len(prof.web_queries), params.languages)
    log.info("интернет", f"запросов для поисковика {len(prof.web_queries)}: "
                         + "; ".join(prof.web_queries[:5]) + (" …" if len(prof.web_queries) > 5 else ""))
    return True
