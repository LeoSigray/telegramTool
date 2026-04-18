import asyncio
import random
import os

from telethon import TelegramClient
from telethon.tl.functions.channels import JoinChannelRequest, GetParticipantRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    ChannelPrivateError,
    PeerFloodError,
    SlowModeWaitError,
    UserAlreadyParticipantError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    ChannelsTooMuchError,
    UserBannedInChannelError,
    UserNotParticipantError,
    ChatAdminRequiredError,
)

from config import (
    CHAT_DELAY_MIN, CHAT_DELAY_MAX,
    JOIN_DELAY_MIN, JOIN_DELAY_MAX,
)
from accounts.manager import get_session_files, create_client
from data.excel_manager import log_chat
from data.daily_limits import (
    DAILY_JOIN_LIMIT, DAILY_SEND_LIMIT,
    get_daily_joins_left, get_daily_sends_left,
    record_join, record_send, get_account_daily_stats,
)


# ========================================================================
#  Хелперы
# ========================================================================

async def _is_member(client: TelegramClient, entity) -> bool:
    try:
        me = await client.get_me()
        await client(GetParticipantRequest(entity, me.id))
        return True
    except (UserNotParticipantError, ChatAdminRequiredError):
        return False
    except Exception:
        return False


async def _join_chat(client: TelegramClient, username: str) -> tuple[bool, str]:
    try:
        if username.startswith("+"):
            await client(ImportChatInviteRequest(username[1:]))
        else:
            entity = await client.get_entity(f"@{username}")
            await client(JoinChannelRequest(entity))
        return True, ""
    except UserAlreadyParticipantError:
        return True, ""
    except (InviteHashExpiredError, InviteHashInvalidError):
        return False, "invite_expired"
    except ChannelPrivateError:
        return False, "private"
    except ChannelsTooMuchError:
        return False, "too_many_channels"
    except UserBannedInChannelError:
        return False, "banned"
    except FloodWaitError as e:
        return False, f"FloodWait {e.seconds}s"
    except PeerFloodError:
        return False, "PeerFlood"
    except Exception as e:
        return False, str(e)


async def _send_message(client: TelegramClient, entity, message: str) -> tuple[bool, str]:
    try:
        await client.send_message(entity, message)
        return True, ""
    except FloodWaitError as e:
        return False, f"FloodWait {e.seconds}s"
    except SlowModeWaitError as e:
        return False, f"SlowMode {e.seconds}s"
    except ChatWriteForbiddenError:
        return False, "write_forbidden"
    except ChannelPrivateError:
        return False, "private"
    except PeerFloodError:
        return False, "PeerFlood"
    except Exception as e:
        return False, str(e)


def _is_fatal(error: str) -> bool:
    return "FloodWait" in error or error in ("PeerFlood", "too_many_channels")


# ========================================================================
#  Воркер одного аккаунта
# ========================================================================

async def _account_worker(
    session_path: str,
    queue: asyncio.Queue,
    message: str,
    sent_counter: list,
    print_lock: asyncio.Lock,
) -> int:
    """
    Обрабатывает один аккаунт: тянет чаты из общей очереди до исчерпания
    дневных лимитов или фатальной ошибки. Непрочитанные чаты возвращает в очередь.
    Возвращает кол-во отправленных сообщений.
    """
    session_name = os.path.splitext(os.path.basename(session_path))[0]

    daily = get_account_daily_stats(session_name)
    if daily["joins_left"] <= 0 and daily["sends_left"] <= 0:
        async with print_lock:
            print(f"[{session_name}] Дневной лимит исчерпан — пропуск")
        return 0

    client = create_client(session_path)
    try:
        await client.start()
        async with print_lock:
            print(f"[{session_name}] Подключен (сегодня: {daily['joins']} вступлений, {daily['sends']} отправок)")
    except Exception as e:
        async with print_lock:
            print(f"[{session_name}] Не удалось подключиться: {e}")
        return 0

    sent = 0

    try:
        while True:
            # Берём чат из очереди
            try:
                chat = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            uname = chat.get("username") or ""
            chat_id = chat.get("chat_id")
            cat = chat.get("category", "")
            target_str = uname or str(chat_id)

            # Дневной лимит отправок
            if get_daily_sends_left(session_name) <= 0:
                async with print_lock:
                    print(f"  [{session_name}] Дневной лимит отправок — передаю следующему")
                queue.put_nowait(chat)  # вернуть в очередь
                break

            # Резолвим entity
            try:
                if chat_id:
                    entity = await client.get_entity(int(chat_id))
                elif uname:
                    entity = await client.get_entity(f"@{uname}")
                else:
                    continue
            except ChannelPrivateError:
                async with print_lock:
                    print(f"  [{session_name}] @{target_str} — приватный, пропуск")
                log_chat(session_name, target_str, "skipped", "private")
                continue
            except FloodWaitError as e:
                async with print_lock:
                    print(f"  [{session_name}] FloodWait {e.seconds}s — передаю следующему")
                log_chat(session_name, target_str, "failed", f"FloodWait {e.seconds}s")
                queue.put_nowait(chat)
                break
            except Exception as e:
                async with print_lock:
                    print(f"  [{session_name}] @{target_str} не найден: {e}")
                log_chat(session_name, target_str, "skipped", str(e))
                continue

            # Членство
            member = await _is_member(client, entity)

            if not member:
                if get_daily_joins_left(session_name) <= 0:
                    async with print_lock:
                        print(f"  [{session_name}] Дневной лимит вступлений ({DAILY_JOIN_LIMIT}) — передаю следующему")
                    queue.put_nowait(chat)
                    break

                async with print_lock:
                    print(f"  [{session_name}] Вступаю в @{target_str}...")
                join_ok, join_err = await _join_chat(client, uname or str(chat_id))

                if not join_ok:
                    if _is_fatal(join_err):
                        async with print_lock:
                            print(f"  [{session_name}] {join_err} — передаю следующему")
                        log_chat(session_name, target_str, "failed", f"join: {join_err}")
                        queue.put_nowait(chat)
                        break
                    else:
                        async with print_lock:
                            print(f"  [{session_name}] @{target_str}: {join_err}")
                        log_chat(session_name, target_str, "skipped", f"join: {join_err}")
                        continue

                record_join(session_name)
                daily_joins = get_account_daily_stats(session_name)["joins"]
                delay = random.uniform(JOIN_DELAY_MIN, JOIN_DELAY_MAX)
                async with print_lock:
                    print(f"  [{session_name}] Вступил (за день: {daily_joins}/{DAILY_JOIN_LIMIT}). Жду {delay:.0f}с...")
                await asyncio.sleep(delay)

            # Отправляем
            send_ok, send_err = await _send_message(client, entity, message)

            if send_ok:
                record_send(session_name)
                label = f"@{target_str}" + (f" [{cat}]" if cat else "")
                async with print_lock:
                    print(f"  [{session_name}] Отправлено → {label}")
                log_chat(session_name, target_str, "sent")
                sent += 1
                sent_counter[0] += 1
            else:
                if _is_fatal(send_err):
                    async with print_lock:
                        print(f"  [{session_name}] {send_err} — передаю следующему")
                    log_chat(session_name, target_str, "failed", send_err)
                    queue.put_nowait(chat)
                    break
                else:
                    async with print_lock:
                        print(f"  [{session_name}] @{target_str}: {send_err}")
                    log_chat(session_name, target_str, "skipped", send_err)

            delay = random.uniform(CHAT_DELAY_MIN, CHAT_DELAY_MAX)
            await asyncio.sleep(delay)

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    daily_final = get_account_daily_stats(session_name)
    async with print_lock:
        print(f"[{session_name}] Готово: {sent} отправок "
              f"(день: {daily_final['joins']}/{DAILY_JOIN_LIMIT} вступлений, "
              f"{daily_final['sends']}/{DAILY_SEND_LIMIT} отправок)\n")
    return sent


# ========================================================================
#  Основная функция рассылки
# ========================================================================

async def run_chat_sending(message: str, chats: list[dict], parallel: int = 1):
    """
    Рассылка по чатам с параллельными аккаунтами.

    parallel — сколько аккаунтов работают одновременно.
    Когда аккаунт исчерпывает лимит, следующий из пула подхватывает очередь.
    """
    if not chats:
        print("Список чатов пуст.")
        return

    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов в sessions/")
        return

    total = len(chats)
    parallel = min(parallel, len(sessions))

    print(f"\nРассылка: {total} чатов | {len(sessions)} аккаунтов | {parallel} параллельно")
    print(f"Дневной лимит: {DAILY_JOIN_LIMIT} вступлений, {DAILY_SEND_LIMIT} отправок/аккаунт")
    print(f"Задержка: {CHAT_DELAY_MIN}-{CHAT_DELAY_MAX} сек\n")

    # Общая очередь чатов
    queue: asyncio.Queue = asyncio.Queue()
    for chat in chats:
        await queue.put(chat)

    sent_counter = [0]
    print_lock = asyncio.Lock()

    # Пул сессий — каждый воркер берёт следующую сессию когда готов
    session_idx = 0
    session_lock = asyncio.Lock()

    async def next_session() -> str | None:
        nonlocal session_idx
        async with session_lock:
            if session_idx >= len(sessions):
                return None
            s = sessions[session_idx]
            session_idx += 1
            return s

    async def worker():
        """Воркер берёт сессии одну за другой пока есть чаты в очереди."""
        while not queue.empty():
            session_path = await next_session()
            if session_path is None:
                return  # сессии закончились
            await _account_worker(session_path, queue, message, sent_counter, print_lock)

    # Запускаем parallel воркеров одновременно
    tasks = [asyncio.create_task(worker()) for _ in range(parallel)]
    await asyncio.gather(*tasks)

    remaining = queue.qsize()
    print(f"\nИтого отправлено: {sent_counter[0]}/{total}")
    if remaining:
        print(f"Не обработано: {remaining} (не хватило аккаунтов с лимитом)")
