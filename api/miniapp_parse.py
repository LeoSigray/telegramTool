"""
api/miniapp_parse.py — умный парсинг под канал или нишу покупателя, по человеку на нажатие.

Поиск не идёт сам по себе: его запускает нажатие большой кнопки. Нажал — умный парсинг
(parsing/smart) ищет подходящих людей именно под канал, продукт или нишу этого покупателя.
Люди отдаются по мере нахождения: как только человек прошёл все проверки, он сразу попадает в
список клиентов и ему уходит сообщение (api/miniapp_send.py), не дожидаясь конца поиска. Нажал
несколько раз подряд — один поиск ищет столько людей, сколько нажатий ждёт (до MAX_BATCH).
Общая база контактов (Excel) не используется.

Парсинг идёт внутри сервиса на клиенте из пула (отдельный процесс упирался бы в те же файлы
сессий: database is locked), по одному поиску за раз. Прошлые поиски не пропадают: профиль
покупателя, прочитанные чаты и «зёрна» лежат в базе парсера, поэтому следующий поиск быстрее.

Telegram ограничивает, сколько @имён аккаунт может открыть за сутки (ResolveUsername). Когда
аккаунт упирается в этот лимит, он помечается и поиск переходит на другой аккаунт.
"""
import asyncio
import logging
import os
import re
import time
from datetime import datetime

from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import ResolveUsernameRequest

from data import analytics as an
from data import miniapp_store as store
from optimizer import health
from parsing.smart.run import run_smart_parse
from parsing.smart.settings import OUT_DIR, Params
from .client_pool import pool

log = logging.getLogger(__name__)

MAX_MINUTES = float(os.getenv("MINIAPP_PARSE_MINUTES", "15") or 15)  # потолок одного поиска
DAYS = int(os.getenv("MINIAPP_PARSE_DAYS", "30") or 30)              # окно свежести сообщений
MAX_BATCH = 5                   # сколько людей ищем за один поиск, если нажали несколько раз подряд
RESERVE = 10                    # запасные кандидаты: если лучший уже найден раньше, берём следующего
ACCOUNT_TRIES = 3               # сколько аккаунтов пробовать, если поиск прервал лимит Telegram
IDLE_SEC = 3.0
_FLOOD_RE = re.compile(r"FloodWait (\d+) с")


def _flooded_until(name: str) -> float:
    raw = store.state_get(f"parse_flood:{name}")
    try:
        return float(raw or 0)
    except ValueError:
        return 0.0


def _mark_flood(name: str, seconds: int) -> None:
    store.state_set(f"parse_flood:{name}", str(time.time() + seconds))
    log.warning("[miniapp-parse] %s: Telegram ограничил поиск на %d мин, берём другой аккаунт", name, seconds // 60)


def _client(exclude: set[str]):
    """Аккаунт для поиска: заданный MINIAPP_PARSE_ACCOUNT, иначе тот, что дольше всех не искал,
    без действующего ограничения Telegram на поиск."""
    now = time.time()
    want = os.getenv("MINIAPP_PARSE_ACCOUNT", "").strip()
    if want and want in pool.clients and want not in exclude and _flooded_until(want) < now:
        return want, pool.clients[want]
    ready = []
    for n in pool.list_active():
        if n in exclude or not health.is_available(n) or _flooded_until(n) >= now:
            continue
        used = float(store.state_get(f"parse_used:{n}") or 0)
        ready.append((used, an.sends_last_hours(n, 24), n))
    if not ready:
        return None
    name = min(ready)[2]
    return name, pool.clients[name]


def _params(user: dict, people: int = 1) -> Params:
    """Поиск людей под этого покупателя: его канал или его ниша, а не канал по умолчанию."""
    kind, value, tg = user.get("channel_kind") or "channel", user["channel"], user["tg_id"]
    common = dict(people=people, reserve=RESERVE, days=DAYS, max_minutes=MAX_MINUTES,
                  rebuild_profile=int(user.get("parse_runs") or 0) == 0)   # первый поиск строит профиль заново
    if kind == "topic":
        # канала нет: условное имя для профиля и истории, а суть берётся из слов покупателя
        params = Params(channel=f"topic-{tg}", topic=value, brief=value, **common)
    else:
        params = Params(channel=value, **common)
    params.out = os.path.join(OUT_DIR, f"{params.channel}_{datetime.now():%Y%m%d-%H%M%S}.xlsx")
    return params


def _to_leads(people: list) -> list[dict]:
    out = []
    for c in people:
        au = getattr(c, "author", None)
        if not au or not au.username:
            continue
        quote = " ".join((c.msg.text or "").split())[:200] if getattr(c.msg, "msg_id", 0) else ""
        out.append({"username": au.username, "user_id": c.msg.sender_id, "name": au.name,
                    "tier": getattr(c, "tier", "C"), "interest": getattr(c, "interest", 0.0),
                    "why": quote or getattr(c, "why", "")})
    return out


def _flood_seconds(log_path: str) -> int:
    """Самое долгое ожидание, которое Telegram потребовал за поиск (по журналу парсера)."""
    try:
        with open(log_path, encoding="utf-8") as f:
            return max((int(x) for x in _FLOOD_RE.findall(f.read())), default=0)
    except OSError:
        return 0


def _take(tg: int, people: list, need: int, got: int) -> int:
    """Новые люди из найденных: кого ещё не находили, не писали и не просили не беспокоить.
    Сразу кладёт их в список клиентов, чтобы сообщение ушло, не дожидаясь конца поиска. Возвращает, сколько взято."""
    leads = _to_leads(people)
    known = store.known_leads(tg)
    contacted = an.already_contacted([ld["username"].lower() for ld in leads]) if leads else set()
    fresh = []
    for ld in leads:
        uname = ld["username"].lower()
        if len(fresh) + got >= need:
            break
        if uname not in known and uname not in contacted and not an.is_suppressed(uname):
            fresh.append(ld)
            known.add(uname)
    taken = store.add_leads(tg, fresh)
    for ld in fresh:
        log.info("[miniapp-parse] нашли @%s (покупатель %s)", ld["username"].lower(), tg)
    return taken


async def find_people(user: dict, need: int) -> int:
    """Ищет need новых людей для покупателя. Каждый найденный сразу попадает в его список клиентов
    (по мере нахождения, а не в конце). Возвращает, сколько нашли."""
    tg = user["tg_id"]
    tried: set[str] = set()
    got = 0
    for _ in range(ACCOUNT_TRIES):
        picked = _client(tried)
        if picked is None:
            log.warning("[miniapp-parse] нет свободного аккаунта для поиска (покупатель %s)", tg)
            return got
        name, client = picked
        tried.add(name)
        store.state_set(f"parse_used:{name}", str(time.time()))
        # дешёвая проверка: если аккаунт уже упёрся в лимит на @имена, весь поиск на нём пустой
        try:
            await client(ResolveUsernameRequest(user["channel"] if user.get("channel_kind") == "channel" else "telegram"))
        except FloodWaitError as e:
            _mark_flood(name, e.seconds)
            continue
        except Exception:  # noqa: BLE001 — канал не нашёлся и т. п.: это не про лимит, ищем дальше
            pass

        params = _params(user, need - got)

        def on_found(people: list) -> None:
            nonlocal got
            got += _take(tg, people, need, got)

        log.info("[miniapp-parse] ищем %d чел.: %s %r через %s (покупатель %s)",
                 need - got, user.get("channel_kind"), params.topic or params.channel, name, tg)
        try:
            await run_smart_parse(params, client=client, echo=False, on_found=on_found,
                                  on_people=on_found, handle_signals=False)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("[miniapp-parse] поиск не удался (покупатель %s)", tg)
        flood = _flood_seconds(os.path.splitext(params.out)[0] + ".log")
        if flood > params.max_flood_wait:
            _mark_flood(name, flood)
        if got >= need or flood <= params.max_flood_wait:
            return got               # нашли всех или поиск прошёл целиком: новых людей больше нет
    return got


async def worker_loop() -> None:
    """Ищет людей для покупателей, у которых нажатий больше, чем найденных людей."""
    stale = store.parse_recover()
    if stale:
        log.info("[miniapp-parse] прерванных перезапуском поисков: %d (повторятся по нажатию)", stale)
    while True:
        tg = None
        try:
            tg = store.next_search()
            if tg is None:
                await asyncio.sleep(IDLE_SEC)
                continue
            user = store.get_user(tg)
            need = min(store.search_need(tg), MAX_BATCH)
            upto = store.newest_queued_tap(tg)
            store.parse_set(tg, "running")
            got = await find_people(user, need)
            store.parse_set(tg, "done" if got else "failed")
            if got < need:
                _give_up(tg, need - got, upto, "не нашли подходящего человека")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("[miniapp-parse] сбой поиска")
            if tg is not None:       # иначе то же нажатие запускало бы тот же сбой снова и снова
                store.parse_set(tg, "failed")
                _give_up(tg, 1, None, "поиск не удался")
            await asyncio.sleep(10)


def _give_up(tg: int, count: int, upto: int | None, why: str) -> None:
    """Людей не нашли: столько же нажатий не уйдёт, сообщения возвращаются на баланс."""
    store.fail_waiting(tg, count, why, upto)
