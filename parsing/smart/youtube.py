"""Поиск чатов через YouTube: обучающие видео → Telegram-каналы их авторов.

Авторы обучающих видео почти всегда ставят в описание ссылку на свой Telegram-канал (часто
с курсами, обсуждением и чатом учеников). Зрители таких видео — те, кто учится теме клиента,
то есть его возможные покупатели. Поэтому:
  1. нейросеть пишет запросы, по которым покупатели клиента ищут обучающие видео
     («как …», «урок …», «… для начинающих»);
  2. выдача YouTube → видео → описание видео;
  3. из описания берём ссылки t.me/…; если в описании ссылок нет — смотрим другое видео того же
     автора, и только потом страницу «О канале» (она тяжёлая);
  4. очки Telegram-канала — число РАЗНЫХ авторов YouTube, которые на него ссылаются
     (один автор — один голос, сколько бы видео у него ни было).
Дальше как обычно: проверка в Telegram, проба, граф от зёрен (из канала автора граф находит
чат курса по ссылкам и пересылкам). Ссылки-приглашения t.me/+… не используем: в чаты не вступаем.

## Откуда скорость

Раньше модуль читал HTML-страницы: страница поиска ~1,4 МБ и КАЖДАЯ страница видео ~1,4 МБ,
по 3 за раз и со сном внутри семафора. Четыре запроса занимали около минуты, а за запуск
раундов до 12 — отсюда «ищет вечно».

Теперь используется тот же JSON-API, которым пользуется сам сайт (InnerTube):

* `/youtubei/v1/search` отдаёт выдачу как JSON и, главное, сразу говорит АВТОРА каждого видео;
* `/youtubei/v1/player` отдаёт описание одного видео за ~11 КБ вместо ~1,4 МБ.

А раз очки считаются по авторам («один автор — один голос»), описания достаточно прочитать
у ОДНОГО видео на автора, а не у всех: 80 видео выдачи — это обычно ~50 авторов, и дочитывать
второе видео нужно только тем, у кого в первом ссылки не нашлось.

Если YouTube поменяет JSON или ответит отказом, модуль сам возвращается к старому разбору HTML
(`_search_videos_html`, `_video_html`) — он никуда не делся и остаётся запасным путём.
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
VIDEOS_ONLY = "EgIQAQ%3D%3D"                       # фильтр выдачи «только видео» (для адреса)
VIDEOS_ONLY_RAW = "EgIQAQ=="                       # он же для JSON-API
VIDEO_ID_RE = re.compile(r'"videoId":"([\w-]{11})"')
DESC_RE = re.compile(r'"shortDescription":"((?:[^"\\]|\\.)*)"')
OWNER_RE = re.compile(r'"ownerChannelName":"((?:[^"\\]|\\.)*)"')
CHANNEL_ID_RE = re.compile(r'"channelId":"(UC[\w-]{22})"')
VIEWS_RE = re.compile(r'"viewCount":"(\d+)"')
# в описаниях часто пишут без https://: «t.me/канал», «telegram.me/канал/123»
TG_LINK_RE = re.compile(r"(?<![\w.])(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?"
                        r"([A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_])", re.I)
INVITE_RE = re.compile(r"(?:t|telegram)\.me/(?:\+|joinchat/)[\w-]+", re.I)

# ── JSON-API сайта (InnerTube) ──────────────────────────────────────────────
# Ключ и версия клиента — те же, что страница YouTube подставляет сама; это не секрет
# и не личный ключ, никакой регистрации не нужно.
INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
INNERTUBE_CLIENT_VERSION = "2.20240726.00.00"
API_HEADERS = {**HEADERS, "Content-Type": "application/json",
               "X-YouTube-Client-Name": "1",
               "X-YouTube-Client-Version": INNERTUBE_CLIENT_VERSION,
               "Origin": YT}

SEARCH_CONCURRENCY = 4      # запросов выдачи одновременно
VIDEO_CONCURRENCY = 8       # описаний видео одновременно
VIDS_PER_AUTHOR = 3         # сколько видео автора держим про запас, если в первом ссылок нет
ABOUT_CAP = 40              # сколько страниц «О канале» читать за раунд (она дороже описания)
API_RETRIES = 2


def _client_ctx(langs: str = "ru", client: str = "WEB") -> dict:
    """Контекст клиента для JSON-API. MWEB отдаёт страницу канала вдвое компактнее WEB."""
    lang = (langs or "ru").split(",")[0].strip() or "ru"
    return {"client": {"clientName": client, "clientVersion": INNERTUBE_CLIENT_VERSION,
                       "hl": lang, "gl": lang.upper()}}


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
    """Автор, его id, просмотры и описание со страницы видео (запасной путь, по HTML)."""
    d = DESC_RE.search(html)
    o = OWNER_RE.search(html)
    c = CHANNEL_ID_RE.search(html)
    v = VIEWS_RE.search(html)
    return {"desc": _unescape(d.group(1)) if d else "", "author": _unescape(o.group(1)) if o else "",
            "channel_id": c.group(1) if c else "", "views": int(v.group(1)) if v else 0}


def authors_from_search(data: dict, cap: int = 0) -> list:
    """Из JSON выдачи — список (videoId, channelId автора, имя автора) в порядке выдачи.

    Выдача приходит деревом из карточек разного вида, поэтому обходим её целиком и берём
    каждый узел, у которого есть videoId и подпись автора со ссылкой на его канал.
    """
    out: list = []
    seen: set = set()

    def owner(node: dict) -> tuple:
        by = node.get("ownerText") or node.get("longBylineText") or node.get("shortBylineText") or {}
        cid = name = ""
        for run in (by.get("runs") or []):
            ep = ((run.get("navigationEndpoint") or {}).get("browseEndpoint") or {})
            bid = str(ep.get("browseId") or "")
            if bid.startswith("UC"):
                cid = bid
            if run.get("text"):
                name = run["text"]
        return cid, name

    def walk(node) -> None:
        if cap and len(out) >= cap:
            return
        if isinstance(node, dict):
            vid = node.get("videoId")
            if isinstance(vid, str) and len(vid) == 11 and vid not in seen and (
                    "ownerText" in node or "longBylineText" in node or "shortBylineText" in node):
                cid, name = owner(node)
                seen.add(vid)
                out.append((vid, cid, name))
                if cap and len(out) >= cap:
                    return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out[:cap] if cap else out


def _cap_by_author(items: list, max_authors: int, per_author: int) -> list:
    """Оставить не больше max_authors разных авторов и по per_author видео у каждого.

    Лимит в этом модуле стоит на авторах, потому что голос даёт автор, а не видео: вся выдача
    приходит одним ответом, так что лишние видео в нём бесплатны, а каждый новый автор — это
    отдельный запрос за описанием.
    """
    out: list = []
    counts: dict = {}          # именно dict: defaultdict создавал бы автора раньше проверки
    for vid, cid, name in items:
        key = cid or name or vid
        seen = counts.get(key, 0)
        if seen == 0 and len(counts) >= max_authors:
            continue           # это новый автор, а их уже набрано сколько нужно
        if seen >= per_author:
            continue
        counts[key] = seen + 1
        out.append((vid, cid, name))
    return out


def video_from_player(data: dict) -> dict:
    """Автор, его id, просмотры и описание из ответа /youtubei/v1/player."""
    vd = data.get("videoDetails") or {}
    try:
        views = int(vd.get("viewCount") or 0)
    except (TypeError, ValueError):
        views = 0
    return {"desc": vd.get("shortDescription") or "", "author": vd.get("author") or "",
            "channel_id": vd.get("channelId") or "", "views": views}


class YouTubeFinder(WebFinder):
    """Как WebFinder (кеш, очки за весь запуск), только источник — YouTube."""

    label = "YouTube"

    def __init__(self, store, params, log) -> None:
        super().__init__(store, params, log)
        self.authors: dict = defaultdict(dict)     # tg-имя → {channel_id: (автор, видео, просмотры)}
        self.videos_seen: set = set()
        self.authors_seen: set = set()             # каналы авторов, уже разобранные в прошлых раундах
        self.invites = 0
        self.api_ok = True                         # JSON-API отвечает; False — идём по HTML
        self.bytes_down = 0                        # для замеров (tests/bench_youtube.py)
        self.http_calls = 0
        self.self_authors = 0                      # сколько авторов опознано как сам клиент
        self.wrong_lang = 0                        # сколько авторов отсеяно по языку
        self.langs: set = {x.strip().lower() for x in (params.languages or "ru").split(",")
                           if x.strip()}
        # имена, по которым узнаём канал самого клиента: его видео вполне попадутся в выдаче,
        # ведь он эксперт по своей же теме
        self.own: set = {(params.channel or "").lower()} - {""}

    def add_own(self, names) -> None:
        """Дополнить список «это сам клиент» (его чат обсуждения, другие его проекты)."""
        self.own |= {str(n).lower().lstrip("@") for n in names if n}

    def foreign(self, info: dict) -> bool:
        """Автор ведёт канал не на нашем языке — его зрители не наша аудитория.

        Запросы мы пишем по-русски и просим у YouTube русскую выдачу (hl/gl), но он всё
        равно подмешивает соседние языки: по «обучение дизайну» приезжают украинские,
        казахские и английские каналы. Их Telegram-чаты потом честно проходят пробу
        (там грамотный русский или своя кириллица), поэтому отсекать дешевле здесь —
        до резолва имён в Telegram.

        Если языка не видно (описание пустое или слишком короткое) — не судим и берём.
        """
        from .probe import detect_lang

        lang = detect_lang(f"{info.get('author', '')} {info.get('desc', '')}")
        return bool(lang) and lang not in self.langs

    # ── сеть ────────────────────────────────────────────────────────────────
    async def _get(self, http, url: str, **params) -> str:
        r = await http.get(url, params=params or None)
        self.http_calls += 1
        self.bytes_down += len(r.content)
        if r.status_code == 429 or "consent.youtube.com" in str(r.url):
            raise WebBlocked(f"YouTube ответил {r.status_code}")
        r.raise_for_status()
        return r.text

    async def _api(self, http, endpoint: str, payload: dict) -> dict:
        """POST к JSON-API сайта. 429 — короткая пауза и ещё попытка."""
        last = None
        for attempt in range(API_RETRIES + 1):
            r = await http.post(f"{YT}/youtubei/v1/{endpoint}",
                                params={"key": INNERTUBE_KEY, "prettyPrint": "false"},
                                headers=API_HEADERS, json=payload)
            self.http_calls += 1
            self.bytes_down += len(r.content)
            if r.status_code == 429:
                last = WebBlocked("YouTube ответил 429")
                await asyncio.sleep(1.5 * (attempt + 1) + random.random())
                continue
            if r.status_code != 200:
                raise WebBlocked(f"YouTube API ответил {r.status_code}")
            try:
                return r.json()
            except ValueError as e:
                raise WebBlocked(f"YouTube API вернул не JSON ({str(e)[:40]})") from None
        raise last or WebBlocked("YouTube API недоступен")

    # ── выдача ──────────────────────────────────────────────────────────────
    async def _search(self, http, q: str) -> list:
        """Видео по запросу как список (videoId, channelId автора, имя автора).

        Автор известен уже из выдачи — поэтому описания читаются по одному видео на автора,
        а не по каждому. Если JSON-API не ответил, откатываемся на разбор HTML (авторы
        тогда неизвестны, и видео читаются как раньше — по одному).
        """
        key = f"yt:s:{q.lower()}"
        cached = self._cache_get(key)
        if cached is not None:
            return [tuple(x) for x in cached["items"]]
        items: list = []
        if self.api_ok:
            try:
                data = await self._api(http, "search", {
                    "context": _client_ctx(self.params.languages),
                    "query": q, "params": VIDEOS_ONLY_RAW})
                # лимит считаем по АВТОРАМ, а не по видео: вся выдача пришла одним ответом,
                # лишние видео уже бесплатны, а дорого обходится только новый автор
                items = _cap_by_author(authors_from_search(data), self.params.yt_videos,
                                       VIDS_PER_AUTHOR)
            except Exception as e:  # noqa: BLE001 — API сменился или закрыт: идём по HTML
                self.api_ok = False
                self.log.info("YouTube", f"JSON-API недоступен ({str(e)[:60]}) — читаю страницы")
        if not items:
            items = [(v, "", "") for v in await self._search_videos_html(http, q)]
        self._cache_put(key, {"items": [list(x) for x in items]})
        return items

    async def _search_videos_html(self, http, q: str) -> list:
        """Запасной путь: id видео со страницы выдачи."""
        html = await self._get(http, f"{YT}/results", search_query=q, sp=VIDEOS_ONLY)
        ids = list(dict.fromkeys(VIDEO_ID_RE.findall(html)))[:self.params.yt_videos]
        await asyncio.sleep(random.uniform(1.0, 2.0))
        return ids

    # совместимость: старое имя метода (его подменяют тесты и внешний код)
    async def _search_videos(self, http, q: str) -> list:
        return [v for v, _c, _n in await self._search(http, q)]

    # ── описание видео ──────────────────────────────────────────────────────
    async def _video(self, http, vid: str) -> dict:
        key = f"yt:v:{vid}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached["video"]
        info = None
        if self.api_ok:
            try:
                info = video_from_player(await self._api(http, "player", {
                    "context": _client_ctx(self.params.languages), "videoId": vid}))
            except Exception:  # noqa: BLE001 — одно видео не отдалось: пробуем страницей
                info = None
        if info is None or not (info["desc"] or info["channel_id"]):
            info = await self._video_html(http, vid)
        info["links"] = links_in(info["desc"])
        info["invites"] = len(INVITE_RE.findall(info["desc"]))
        info["desc"] = info["desc"][:600]
        self._cache_put(key, {"video": info})
        return info

    async def _video_html(self, http, vid: str) -> dict:
        return parse_video(await self._get(http, f"{YT}/watch", v=vid))

    async def _channel_links(self, http, channel_id: str) -> list:
        """Ссылки со страницы «О канале» автора — когда в его видео их нет.
        Страница тяжёлая (~1–2 МБ), поэтому читается последней и с лимитом ABOUT_CAP."""
        key = f"yt:c:{channel_id}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached["links"]
        links: list = []
        try:
            if self.api_ok:
                data = await self._api(http, "browse", {
                    "context": _client_ctx(self.params.languages, "MWEB"),
                    "browseId": channel_id, "params": "EgVhYm91dPIGBAoCEgA%3D"})
                raw = json.dumps(data, ensure_ascii=False)
            else:
                raw = await self._get(http, f"{YT}/channel/{channel_id}/about")
            links = links_in(raw.replace("\\u002F", "/").replace("%2F", "/").replace("%3A", ":"))
        except Exception:  # noqa: BLE001
            links = []
        self._cache_put(key, {"links": links})
        return links

    # ── раунд ───────────────────────────────────────────────────────────────
    def _take(self, name: str, key: str, info: dict, vid: str, touched: set) -> bool:
        """Засчитать голос автора за Telegram-канал. Один автор — один голос."""
        if key in self.authors[name]:
            return False
        self.authors[name][key] = (info["author"], vid, info["views"])
        self.sites[name][key] = (1.0, f"{YT}/watch?v={vid}",
                                 f"{info['author']}: {info['desc'][:200]}")
        self.score[name] = float(len(self.authors[name]))
        touched.add(name)
        return True

    async def run(self, queries: list) -> list:
        import httpx

        touched: set = set()
        n_videos = n_links = 0
        async with httpx.AsyncClient(headers=HEADERS, cookies=COOKIES, timeout=20,
                                     follow_redirects=True) as http:
            # 1) выдача по всем запросам сразу: запросы друг от друга не зависят
            ssem = asyncio.Semaphore(SEARCH_CONCURRENCY)

            async def search_one(q):
                async with ssem:
                    try:
                        return await self._search(http, q)
                    except Exception as e:  # noqa: BLE001
                        return e

            results = await asyncio.gather(*(search_one(q) for q in queries))
            failed = [r for r in results if isinstance(r, Exception)]
            if failed and len(failed) == len(queries):
                # ни один запрос не прошёл — поиск действительно закрыт
                self.blocked = True
                self.log.warn("YouTube", f"поиск недоступен ({str(failed[0])[:80]}) — "
                                         "в этом раунде остановлен")
                return []
            if failed:
                self.log.info("YouTube", f"запросов не прошло {len(failed)} из {len(queries)} "
                                         f"({str(failed[0])[:60]}) — остальные отработали")

            # 2) группируем видео по автору: описания читаем по одному видео на автора
            by_author: dict = {}
            loose: list = []        # видео без известного автора (запасной путь по HTML)
            for res in results:
                if isinstance(res, Exception):
                    continue
                for vid, cid, name in res:
                    if vid in self.videos_seen:
                        continue
                    if cid:
                        if cid in self.authors_seen:
                            self.videos_seen.add(vid)
                            continue
                        by_author.setdefault(cid, {"name": name, "vids": []})["vids"].append(vid)
                    else:
                        loose.append(vid)
            for cid in by_author:
                self.authors_seen.add(cid)

            vsem = asyncio.Semaphore(VIDEO_CONCURRENCY)

            async def video_one(vid):
                async with vsem:
                    try:
                        return vid, await self._video(http, vid)
                    except Exception as e:  # noqa: BLE001
                        self.log.info("YouTube", f"видео {vid} не открылось ({str(e)[:60]})")
                        return vid, None

            async def harvest(vids: list) -> list:
                return [r for r in await asyncio.gather(*(video_one(v) for v in vids))
                        if r[1] is not None]

            # первая волна: по одному видео на автора + все «ничьи» видео
            first = [a["vids"][0] for a in by_author.values()] + loose
            self.videos_seen.update(first)
            got = await harvest(first)

            empty_authors: dict = {}      # автор → остальные его видео, если ссылок не нашлось
            for vid, info in got:
                n_videos += 1
                self.invites += info.get("invites", 0)
                cid = info["channel_id"]
                key = cid or info["author"] or vid
                if self.foreign(info):
                    self.wrong_lang += 1
                    continue
                if info["links"]:
                    # Автор, который ссылается на канал клиента, — это сам клиент (или его
                    # сотрудник). Его подписчики клиенту уже известны, а чат курса полон
                    # оплативших учеников: это не лиды. Поэтому не берём у него НИ ОДНОЙ
                    # ссылки, а не только сам канал — тот и так отсеялся бы по exclude.
                    if self.own & set(info["links"]):
                        self.self_authors += 1
                        self.log.info("YouTube", f"автор «{info['author'][:40]}» ссылается на канал "
                                                 "клиента — это он сам, его ссылки не берём")
                        continue
                    for name in info["links"]:
                        n_links += self._take(name, key, info, vid, touched)
                elif cid:
                    spare = by_author.get(cid, {}).get("vids", [])[1:]
                    empty_authors[cid] = (spare, info, vid)

            # вторая волна: у кого в первом видео ссылок нет — смотрим остальные его видео
            second = [v for vids, _i, _v in empty_authors.values() for v in vids]
            if second:
                self.videos_seen.update(second)
                for vid, info in await harvest(second):
                    n_videos += 1
                    self.invites += info.get("invites", 0)
                    cid = info["channel_id"]
                    key = cid or info["author"] or vid
                    if self.foreign(info):
                        self.wrong_lang += 1
                        empty_authors.pop(cid, None)
                        continue
                    if info["links"]:
                        if self.own & set(info["links"]):
                            self.self_authors += 1
                            empty_authors.pop(cid, None)
                            continue
                        for name in info["links"]:
                            n_links += self._take(name, key, info, vid, touched)
                        empty_authors.pop(cid, None)

            # третья волна: страница «О канале» — дорогая, поэтому только у немногих
            still = [cid for cid in empty_authors if cid][:ABOUT_CAP]
            if still:
                async def about_one(cid):
                    async with vsem:
                        return cid, await self._channel_links(http, cid)
                for cid, links in await asyncio.gather(*(about_one(c) for c in still)):
                    _spare, info, src_vid = empty_authors[cid]
                    if self.own & set(links):
                        self.self_authors += 1
                        continue
                    for name in links:
                        n_links += self._take(name, cid, info, src_vid, touched)

        ranked = sorted(touched, key=lambda n: (-self.score[n], -self._views(n)))
        self.log.info("YouTube", f"запросов {len(queries)}, авторов {len(by_author) or len(loose)}, "
                                 f"видео {n_videos}, ссылок на Telegram {n_links}, "
                                 f"разных каналов {len(ranked)}"
                      + (f", закрытых приглашений (не берём) {self.invites}" if self.invites else "")
                      + (f", авторов-самого-клиента пропущено {self.self_authors}"
                         if self.self_authors else "")
                      + (f", не на «{','.join(sorted(self.langs))}» пропущено {self.wrong_lang}"
                         if self.wrong_lang else "")
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
