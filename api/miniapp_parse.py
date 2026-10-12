"""
api/miniapp_parse.py — умный парсинг под канал или нишу каждого покупателя.

Когда покупатель говорит, что продвигает (канал, продукт или нишу), для него запускается
умный парсинг (parsing/smart): он строит профиль, ищет чаты, где общаются клиенты именно
этой ниши, читает их и ранжирует людей по интересу. Найденные люди становятся «клиентами
покупателя» (mini_leads); каждое нажатие большой кнопки пишет следующему из них
(api/miniapp_send.py). Общая база контактов (Excel) здесь не используется.

Парсинг идёт внутри сервиса на клиенте из пула аккаунтов (не отдельным процессом):
отдельный процесс упирался бы в те же файлы сессий (database is locked). Одновременно идёт
один поиск; он ограничен по времени (MINIAPP_PARSE_MINUTES). Когда клиенты у покупателя
почти кончились, поиск запускается заново: уже найденные и уже получавшие сообщения
исключаются, а зёрна прошлых запусков ускоряют поиск.
"""
import asyncio
import logging
import os

from data import analytics as an
from data import miniapp_store as store
from optimizer import health
from parsing.smart.run import run_smart_parse
from parsing.smart.settings import Params
from . import miniapp_bot as bot
from .client_pool import pool

log = logging.getLogger(__name__)

PEOPLE = int(os.getenv("MINIAPP_PARSE_PEOPLE", "50") or 50)          # сколько людей искать за один поиск
MAX_MINUTES = float(os.getenv("MINIAPP_PARSE_MINUTES", "40") or 40)  # потолок времени одного поиска
DAYS = int(os.getenv("MINIAPP_PARSE_DAYS", "30") or 30)              # окно свежести сообщений
TOPUP_MIN_NEW = 5                  # меньше стольких свободных клиентов: пора искать ещё
TOPUP_INTERVAL_SEC = 6 * 3600      # и не чаще, чем раз в столько
MAX_RUNS = 12                      # предел поисков на одного покупателя
IDLE_SEC = 5.0

_current: dict = {"tg_id": None}


def _client():
    """Аккаунт для чтения чатов: заданный MINIAPP_PARSE_ACCOUNT, иначе самый свободный из пула."""
    want = os.getenv("MINIAPP_PARSE_ACCOUNT", "").strip()
    if want and want in pool.clients:
        return want, pool.clients[want]
    ready = [(an.sends_last_hours(n, 24), n) for n in pool.list_active() if health.is_available(n)]
    if ready:
        name = min(ready)[1]
        return name, pool.clients[name]
    return None


def _params(user: dict) -> Params:
    """Параметры умного парсинга под этого покупателя: его канал или его ниша, а не общий канал по умолчанию."""
    kind, value, tg = user.get("channel_kind") or "channel", user["channel"], user["tg_id"]
    common = dict(people=PEOPLE, days=DAYS, max_minutes=MAX_MINUTES,
                  rebuild_profile=int(user.get("parse_runs") or 0) == 0)   # первый поиск строит профиль заново
    if kind == "topic":
        # канала нет: условное имя для профиля и истории, а суть берётся из слов покупателя
        return Params(channel=f"topic-{tg}", topic=value, brief=value, **common)
    return Params(channel=value, **common)


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


async def _run(user: dict) -> None:
    tg = user["tg_id"]
    picked = _client()
    if picked is None:
        log.warning("[miniapp-parse] нет свободного аккаунта для поиска клиентов (покупатель %s)", tg)
        store.parse_finish(tg, "failed")
        return
    name, client = picked
    params = _params(user)
    found: list[dict] = []
    log.info("[miniapp-parse] ищем клиентов: %s %r через %s (покупатель %s)",
             user.get("channel_kind"), params.topic or params.channel, name, tg)
    before = await asyncio.to_thread(store.leads_new, tg)
    try:
        await run_smart_parse(params, client=client, echo=False,
                              on_people=lambda people: found.extend(_to_leads(people)), handle_signals=False)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("[miniapp-parse] поиск клиентов не удался (покупатель %s)", tg)
        if found:
            await asyncio.to_thread(store.add_leads, tg, found)
        store.parse_finish(tg, "failed")
        return
    added = await asyncio.to_thread(store.add_leads, tg, found)
    store.parse_finish(tg, "done")
    log.info("[miniapp-parse] нашли %d клиентов, добавлено %d (покупатель %s)", len(found), added, tg)
    if added and before == 0:
        await bot.send(tg, f"Нашли для вас клиентов: {added}. Можно отправлять сообщения.", button="Открыть")


async def worker_loop() -> None:
    """Раз в несколько секунд берёт следующий поиск из очереди; время от времени ставит повторные."""
    recovered = store.parse_recover()
    if recovered:
        log.info("[miniapp-parse] вернули в очередь прерванные поиски: %d", recovered)
    missing = store.parse_enqueue_missing()
    if missing:
        log.info("[miniapp-parse] поставили поиск клиентов тем, кто подключился раньше: %d", missing)
    last_topup = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            if loop.time() - last_topup > 300:
                last_topup = loop.time()
                for tg in store.parse_topup_candidates(TOPUP_MIN_NEW, TOPUP_INTERVAL_SEC, MAX_RUNS):
                    store.parse_enqueue(tg)
            user = store.parse_claim()
            if user is None:
                await asyncio.sleep(IDLE_SEC)
                continue
            _current["tg_id"] = user["tg_id"]
            try:
                await _run(user)
            finally:
                _current["tg_id"] = None
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("[miniapp-parse] сбой воркера поиска")
            await asyncio.sleep(10)
