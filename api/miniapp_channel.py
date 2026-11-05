"""
api/miniapp_channel.py — что покупатель продвигает и готовый текст рассылки.

Покупатель Mini App ничего не пишет сам: при первом входе он коротко говорит, что
продвигает, одним из трёх способов — канал, продукт или нишу («@mychannel»,
«маникюр в Москве», «онлайн-школа английского»). Если это канал, сервис читает
название, описание и свежие посты (как умный парсинг строит профиль клиента,
parsing/smart/profile.py). Если продукт или ниша, текст пишется по этой фразе.
Дальше бесплатная нейронка пишет первое сообщение, а людей под этот канал или нишу по одному
на каждое нажатие ищет умный парсинг (api/miniapp_parse.py).

Текст можно поправить в профиле. Если нейронка недоступна, текст собирается шаблоном.
"""
import asyncio
import logging
import re

from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameNotOccupiedError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel

from data import miniapp_store as store
from optimizer import health
from .client_pool import pool
from .llm import complete

log = logging.getLogger(__name__)

HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
TME_RE = re.compile(r"(?:^|//)(?:www\.)?t(?:elegram)?\.me/", re.IGNORECASE)
FETCH_TIMEOUT_SEC = 60
GENERATE_TIMEOUT_SEC = 60
MAX_LEN = 600
MAX_TOPIC_LEN = 300
MIN_TOPIC_LEN = 3

_tasks: dict[int, asyncio.Task] = {}


class ChannelError(Exception):
    """Понятная покупателю причина, почему ввод не подошёл."""


def normalize(raw: str) -> str:
    """@name, t.me/name, https://t.me/name → name. ChannelError, если это не похоже на имя канала."""
    s = (raw or "").strip()
    s = re.sub(r"^(?:https?://)?(?:www\.)?t(?:elegram)?\.me/", "", s, flags=re.IGNORECASE)
    if s.startswith("s/"):
        s = s[2:]
    handle = s.split("?")[0].strip("/").lstrip("@")
    if not HANDLE_RE.match(handle):
        raise ChannelError("Не похоже на канал. Пришлите ссылку вида t.me/имя или @имя")
    return handle


def parse(raw: str) -> tuple[str, str, bool]:
    """Что написал покупатель: ('channel', имя, строго) или ('topic', описание, False).

    «Строго» значит, что человек явно прислал канал (@имя или ссылку t.me): не нашли такой канал —
    это ошибка. Просто слово латиницей («yoga») пробуем как канал, а если такого нет, считаем нишей.
    """
    s = (raw or "").strip()
    if not s:
        raise ChannelError("Напишите, что вы продвигаете: канал, продукт или нишу")
    explicit = s.startswith("@") or bool(TME_RE.search(s))
    try:
        return "channel", normalize(s), explicit
    except ChannelError:
        if explicit:
            raise
    if len(s) < MIN_TOPIC_LEN:
        raise ChannelError("Напишите чуть подробнее: канал, продукт или нишу")
    return "topic", re.sub(r"\s+", " ", s)[:MAX_TOPIC_LEN], False


def _clean_line(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[✅✔☑️•·▪️▫️➡️→\-–—]+", " ", s or "")).strip()


def _one_line(x) -> str:
    return re.sub(r"\s+", " ", str(x)).strip()


TECH_WORKS = "Сейчас ведутся технические работы. Попробуйте подключить канал через несколько минут"


def _candidate_clients(limit: int = 4) -> list:
    """Несколько клиентов на попытку: один может быть ослепшим или с оборванным коннектом.

    Раньше брался строго первый, и его личная поломка превращалась в «канала не существует» —
    покупатель видел, что виноват он, хотя виноват наш аккаунт.
    """
    out, seen = [], set()
    for name in pool.list_active():
        if health.is_available(name) and name in pool.clients:
            out.append(pool.clients[name])
            seen.add(name)
        if len(out) >= limit:
            return out
    for name, client in pool.clients.items():     # пул на отдыхе: читать канал всё же можно
        if name not in seen:
            out.append(client)
        if len(out) >= limit:
            break
    if not out:
        raise ChannelError(TECH_WORKS)
    return out


# «Канала нет» мы сказать не можем — нечем проверить. Ослепший аккаунт отвечает «No user has
# X as username» про живой канал: на @linktek один аккаунт ответил так, а соседний в ту же
# минуту прочитал «LINKTECH». Пока нет отдельного аккаунта под проверки, любая неудача — наша
# техническая проблема, и говорить надо о ней, а не отправлять покупателя искать опечатку.
_MAYBE_GONE = (UsernameNotOccupiedError,)


def _looks_missing(e: Exception) -> bool:
    return isinstance(e, _MAYBE_GONE) or (
        isinstance(e, ValueError) and ("No user has" in str(e) or "Cannot find any entity" in str(e)))


async def fetch_profile(handle: str) -> dict:
    ent, client, last = None, None, None
    tried = missing = 0
    for cand in _candidate_clients():
        tried += 1
        try:
            ent, client = await cand.get_entity(handle), cand
            break
        except FloodWaitError:
            raise ChannelError("Telegram просит подождать. Попробуйте через минуту") from None
        except UsernameInvalidError:
            raise ChannelError("Проверьте ссылку на канал: такое имя не подходит") from None
        except Exception as e:  # noqa: BLE001 — любая другая причина: пробуем следующий аккаунт
            last = e
            if _looks_missing(e):
                missing += 1
                log.info("[miniapp] канал %s: аккаунт не видит имени (%s попытка)", handle, tried)
            else:
                log.warning("[miniapp] канал %s: аккаунт не справился: %s: %s", handle, type(e).__name__, e)
    if ent is None:
        log.warning("[miniapp] канал %s: не прочитал ни один из %d аккаунтов "
                    "(из них «нет имени»: %d), последняя ошибка: %s", handle, tried, missing, last)
        raise ChannelError(TECH_WORKS)
    if not isinstance(ent, Channel):
        raise ChannelError("Это не канал. Пришлите ссылку на ваш канал или группу")
    assert client is not None                     # ent получен — значит и клиент, который его прочитал
    about = ""
    try:
        full = await client(GetFullChannelRequest(ent))
        about = (getattr(full.full_chat, "about", "") or "").strip()
    except Exception as e:  # noqa: BLE001 — описание не обязательно
        log.info("[miniapp] описание канала %s не получено: %s", handle, e)
    posts: list[str] = []
    try:
        for m in await client.get_messages(ent, limit=15):
            t = (getattr(m, "message", "") or "").strip()
            if len(t) >= 40:
                posts.append(t[:500])
            if len(posts) >= 8:
                break
    except Exception as e:  # noqa: BLE001 — без постов обойдёмся описанием
        log.info("[miniapp] посты канала %s не получены: %s", handle, e)
    return {"kind": "channel", "channel": handle, "title": (ent.title or "").strip(),
            "about": about, "posts_sample": posts}


def _topic_profile(topic: str) -> dict:
    return {"kind": "topic", "channel": "", "title": "", "about": topic, "posts_sample": []}


def _fallback_text(p: dict) -> str:
    if p.get("kind") == "topic":
        return (f"Здравствуйте! Хочу рассказать вам о том, чем занимаюсь: {p['about']}. "
                "Думаю, вам может быть интересно. Расскажу подробнее?")[:MAX_LEN]
    title = _clean_line((p.get("title") or "").split("|")[0]) or p["channel"]
    about = [_clean_line(x) for x in (p.get("about") or "").splitlines()]
    about = [x for x in about if len(x) > 15]
    pitch = f" Пишу о том, {about[0][0].lower() + about[0][1:]}." if about else ""
    return (f"Здравствуйте! Веду канал «{title}».{pitch} "
            "Думаю, вам может быть интересно. Расскажу подробнее?")[:MAX_LEN]


_RULES = (
    "Требования:\n"
    "- 2–3 коротких предложения, на «вы», живой разговорный язык, без канцелярита и без эмодзи\n"
    "- Начни с «Здравствуйте!», назови одну конкретную пользу для читателя, "
    "в конце — мягкий вопрос или приглашение ответить\n"
    "{channel_rule}"
    "- Ничего не выдумывай: цифры, цены, обещания результата и названия, которых нет выше, не используй\n"
    "- Не пиши, что текст сгенерирован\n\n"
    "Верни только сам текст сообщения, без кавычек, markdown и пояснений."
)


def _prompt(p: dict) -> str:
    if p.get("kind") == "topic":
        return (
            "Ты пишешь ПЕРВОЕ сообщение в личку Telegram от человека, который продвигает свой продукт "
            "или нишу, потенциальному клиенту.\n\n"
            f"Чем занимается автор (его слова): {p['about']}\n\n"
            + _RULES.format(channel_rule="- Не называй каналы и не вставляй ссылки\n"))
    title = (p.get("title") or "").strip() or p["channel"]
    about = (p.get("about") or "").strip()
    posts = "\n".join("- " + _one_line(x)[:260] for x in (p.get("posts_sample") or [])[:5])
    return (
        "Ты пишешь ПЕРВОЕ сообщение в личку Telegram от владельца канала человеку, "
        "которому этот канал может быть полезен.\n\n"
        f"Канал: «{title}»\n"
        f"Описание канала:\n{about or '—'}\n"
        f"Свежие посты (только чтобы понять тему):\n{posts or '—'}\n\n"
        + _RULES.format(channel_rule="- Назови канал по названию, без ссылок и без @\n"))


def _sanitize(raw: str) -> str:
    s = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL).strip()
    s = re.sub(r"^```\w*\s*|\s*```$", "", s).strip()
    s = s.replace("‑", "-").replace(" ", " ")
    s = re.sub(r"[*_`#]+", "", s)
    return s.strip().strip("«»\"'").strip()[:MAX_LEN].strip()


async def make_text(p: dict) -> str:
    """Текст по профилю: нейронка, а при любой её проблеме шаблон."""
    try:
        raw = await asyncio.wait_for(complete(_prompt(p), task="fast", max_tokens=600),
                                     timeout=GENERATE_TIMEOUT_SEC)
        text = _sanitize(raw)
        if len(text) >= 30:
            return text
        log.warning("[miniapp] нейронка вернула слишком короткий текст, берём шаблон")
    except Exception as e:  # noqa: BLE001 — нейронка не должна ломать подключение
        log.warning("[miniapp] нейронка не написала текст, берём шаблон: %s", e)
    return _fallback_text(p)


async def _build(tg_id: int, kind: str, value: str, strict: bool) -> None:
    try:
        if kind == "channel":
            try:
                profile = await asyncio.wait_for(fetch_profile(value), timeout=FETCH_TIMEOUT_SEC)
            except ChannelError:
                if strict:
                    raise
                # просто слово, а такого канала нет: значит, это ниша
                await asyncio.to_thread(store.set_channel_building, tg_id, value, "topic")
                profile = _topic_profile(value)
        else:
            profile = _topic_profile(value)
        text = await make_text(profile)
        await asyncio.to_thread(store.set_channel_ready, tg_id, text)
        log.info("[miniapp] подключено: %s %r (покупатель %s)", profile["kind"], value[:60], tg_id)
    except ChannelError as e:
        await asyncio.to_thread(store.set_channel_failed, tg_id, str(e))
    except Exception:  # noqa: BLE001
        log.exception("[miniapp] не удалось подключить %r", value[:60])
        await asyncio.to_thread(store.set_channel_failed, tg_id,
                                "Не получилось прочитать канал. Попробуйте ещё раз")


def start_build(tg_id: int, raw: str, *, kind: str | None = None) -> tuple[str, str]:
    """Запоминает, что продвигает покупатель, и в фоне пишет текст. Возвращает (вид, значение).

    kind задан при возобновлении после перезапуска: raw уже разобран и хранится в базе."""
    if kind is None:
        kind, value, strict = parse(raw)
    else:
        value, strict = raw, True
    old = _tasks.get(tg_id)
    if old is not None and not old.done():
        raise ChannelError("Уже читаем, подождите немного")
    store.set_channel_building(tg_id, value, kind)
    task = asyncio.create_task(_build(tg_id, kind, value, strict))
    _tasks[tg_id] = task
    task.add_done_callback(lambda t, k=tg_id: _tasks.pop(k, None) if _tasks.get(k) is t else None)
    return kind, value


def resume_pending() -> int:
    """После перезапуска сервиса то, что читалось, читаем заново."""
    n = 0
    for u in store.building_channels():
        try:
            start_build(u["tg_id"], u["channel"], kind=u.get("channel_kind") or "channel")
            n += 1
        except ChannelError:
            store.set_channel_failed(u["tg_id"], "Не получилось прочитать канал. Попробуйте ещё раз")
    return n
