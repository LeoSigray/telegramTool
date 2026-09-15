"""
api/channel_watch.py — детект подписок на канал.

Telegram не шлёт "юзер X подписался на канал" как событие (в отличие от
вступления в группу) — участников канала можно только периодически
СВЕРЯТЬ списком. Поэтому это не live-событие, а синк по запросу:

  1. Берём текущих участников канала (client.iter_participants).
  2. Сравниваем с тем, что видели в прошлый раз (data.analytics.channel_members).
  3. Новые ID сверяем с sends.peer_id (кому писали и звали в этот канал)
     — совпадения помечаются как подписка (sends.subscribed_at).
  4. Запоминаем новый снимок участников для следующего диффа.

Требует, чтобы хотя бы один аккаунт из пула был админом канала — иначе
Telegram отдаёт urезанный/пустой список участников.
"""
import logging

from telethon.errors import ChatAdminRequiredError, FloodWaitError

from data import analytics as an
from .client_pool import pool

log = logging.getLogger(__name__)


async def sync_channel(channel: str) -> dict:
    """Синкает один канал. Возвращает {channel, checked, new_members, matched}."""
    if not pool.clients:
        raise RuntimeError("нет ни одного подключённого аккаунта в пуле")

    last_error: Exception | None = None
    for client in pool.clients.values():
        try:
            entity = await client.get_entity(channel)
            return await _diff(client, entity, channel)
        except (ChatAdminRequiredError, FloodWaitError) as e:
            last_error = e
            continue
        except Exception as e:  # noqa: BLE001
            last_error = e
            continue

    raise RuntimeError(f"не удалось синкнуть {channel}: {last_error}")


async def _diff(client, entity, channel: str) -> dict:
    known = an.known_channel_members(channel)
    current: list[str] = []
    new_ids: list[str] = []
    joined: dict[str, str] = {}  # uid -> реальная дата вступления (participant.date)

    try:
        async for user in client.iter_participants(entity, aggressive=True):
            uid = str(user.id)
            current.append(uid)
            pdate = getattr(getattr(user, "participant", None), "date", None)
            if pdate is not None:
                try:
                    joined[uid] = pdate.isoformat()
                except (AttributeError, ValueError):
                    pass
            if uid not in known:
                new_ids.append(uid)
    except FloodWaitError as e:
        log.warning("[channel_watch] FloodWait %ss на синке %s (частичный снимок: %d)",
                   e.seconds, channel, len(current))

    matched = 0
    for uid in new_ids:
        if an.record_subscription(uid, channel, joined_at=joined.get(uid)):
            matched += 1

    # Telegram не шлёт событие подписки — раньше subscribed_at ставилось временем
    # часового синка. Теперь одноразово чиним ранее сматченные подписки этого
    # канала на реальное время вступления (для 10-минутного графика это критично).
    corrected = an.correct_subscription_dates(channel, joined)

    an.remember_channel_members(channel, current)
    log.info("[channel_watch] %s: участников=%d новых=%d совпало_с_рассылкой=%d исправлено_дат=%d",
             channel, len(current), len(new_ids), matched, corrected)
    return {"channel": channel, "checked": len(current), "new_members": len(new_ids),
            "matched": matched, "corrected_dates": corrected}


async def sync_all_recent(days: float = 30) -> list[dict]:
    """Синкает все каналы, которые фигурировали в рассылках за последние N дней."""
    out = []
    for channel in an.list_recent_channels(days=days):
        try:
            out.append(await sync_channel(channel))
        except Exception as e:  # noqa: BLE001
            log.warning("[channel_watch] пропуск %s: %s", channel, e)
            out.append({"channel": channel, "error": str(e)})
    return out
