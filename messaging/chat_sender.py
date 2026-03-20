import asyncio
import random
import os

from telethon import TelegramClient
from telethon.tl.functions.channels import JoinChannelRequest
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
)

from config import (
    CHAT_DELAY_MIN, CHAT_DELAY_MAX, CHAT_LIMIT_PER_ACCOUNT,
    JOIN_DELAY_MIN, JOIN_DELAY_MAX, JOIN_LIMIT_PER_ACCOUNT,
)
from accounts.manager import get_session_files, create_client
from data.excel_manager import load_chats, load_chats_by_category, log_chat


# ========================================================================
#  Хелперы
# ========================================================================

def _resolve_chat(target: dict):
    """Возвращает идентификатор чата для Telethon."""
    chat_id = target.get("chat_id")
    username = target.get("username")
    if chat_id:
        return int(chat_id)
    if username:
        return f"@{username}" if not username.startswith("@") else username
    return None


async def _join_chat(client: TelegramClient, username: str) -> tuple[bool, str]:
    """
    Вступает в чат по username.
    Возвращает (success, error).
    """
    try:
        # Если это invite-ссылка (t.me/+hash или t.me/joinchat/hash)
        if username.startswith("+"):
            await client(ImportChatInviteRequest(username[1:]))
        else:
            entity = await client.get_entity(f"@{username}")
            await client(JoinChannelRequest(entity))
        return True, ""
    except UserAlreadyParticipantError:
        return True, ""  # уже в чате — ОК
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


async def _send_message(client: TelegramClient, username: str, message: str) -> tuple[bool, str]:
    """Отправляет сообщение в чат. Возвращает (success, error)."""
    try:
        entity = await client.get_entity(f"@{username}")
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


# ========================================================================
#  Рассылка по категориям (вступление + отправка)
# ========================================================================

async def run_category_sending(message: str, chats: list[dict]):
    """
    Рассылка по чатам из категорий.
    Для каждого чата: вступить -> подождать -> написать.
    Лимит JOIN_LIMIT_PER_ACCOUNT чатов на аккаунт.
    """
    if not chats:
        print("Список чатов пуст.")
        return

    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов в sessions/")
        return

    total = len(chats)
    limit = JOIN_LIMIT_PER_ACCOUNT

    print(f"\nРассылка: {total} чатов, {len(sessions)} аккаунтов")
    print(f"Лимит: {limit} чатов/аккаунт")
    print(f"Задержка вступления: {JOIN_DELAY_MIN}-{JOIN_DELAY_MAX} сек")
    print(f"Задержка отправки: {CHAT_DELAY_MIN}-{CHAT_DELAY_MAX} сек\n")

    remaining = list(chats)
    total_sent = 0

    for session_path in sessions:
        if not remaining:
            break

        session_name = os.path.splitext(os.path.basename(session_path))[0]
        client = create_client(session_path)

        try:
            await client.start()
            print(f"[{session_name}] Подключен")
        except Exception as e:
            print(f"[{session_name}] Не удалось подключиться: {e}")
            continue

        sent = 0
        stop_account = False
        batch = remaining[:limit]

        for chat in batch:
            if stop_account:
                break

            uname = chat["username"]
            cat = chat.get("category", "")

            # 1. Вступаем
            print(f"  [{session_name}] Вступаю в @{uname}...")
            join_ok, join_err = await _join_chat(client, uname)

            if not join_ok:
                if "FloodWait" in join_err or join_err == "PeerFlood" or join_err == "too_many_channels":
                    print(f"  [{session_name}] {join_err} — переключаю аккаунт")
                    log_chat(session_name, uname, "failed", f"join: {join_err}")
                    stop_account = True
                    break
                else:
                    print(f"  [{session_name}] Не удалось вступить в @{uname}: {join_err}")
                    log_chat(session_name, uname, "skipped", f"join: {join_err}")
                    remaining.remove(chat)
                    continue

            # Задержка после вступления
            delay = random.uniform(JOIN_DELAY_MIN, JOIN_DELAY_MAX)
            print(f"  [{session_name}] Вступил. Жду {delay:.0f} сек...")
            await asyncio.sleep(delay)

            # 2. Отправляем сообщение
            send_ok, send_err = await _send_message(client, uname, message)

            if send_ok:
                print(f"  [{session_name}] Отправлено в @{uname} [{cat}]")
                log_chat(session_name, uname, "sent")
                sent += 1
                total_sent += 1
                remaining.remove(chat)
            else:
                if "FloodWait" in send_err or send_err == "PeerFlood":
                    print(f"  [{session_name}] {send_err} — переключаю аккаунт")
                    log_chat(session_name, uname, "failed", send_err)
                    stop_account = True
                    break
                else:
                    print(f"  [{session_name}] Ошибка @{uname}: {send_err}")
                    log_chat(session_name, uname, "skipped", send_err)
                    remaining.remove(chat)

            # Задержка между чатами
            if not stop_account:
                delay = random.uniform(CHAT_DELAY_MIN, CHAT_DELAY_MAX)
                print(f"  Задержка {delay:.0f} сек...")
                await asyncio.sleep(delay)

        try:
            await client.disconnect()
        except Exception:
            pass

        print(f"[{session_name}] Готово: {sent}/{limit}\n")

    print(f"\nИтого: отправлено {total_sent}/{total}")
    if remaining:
        print(f"Осталось: {len(remaining)} (не хватило аккаунтов)")


# ========================================================================
#  Старая рассылка (по листу Chats, без вступления)
# ========================================================================

async def run_chat_sending(message):
    """Рассылка по чатам из листа Chats (без вступления, для уже вступленных)."""
    chats = load_chats()
    if not chats:
        print("Список чатов пуст. Заполните лист 'Chats' в data/targets.xlsx")
        return

    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов в sessions/")
        return

    print(f"\nРассылка: {len(chats)} чатов, {len(sessions)} аккаунтов")
    print(f"Лимит: {CHAT_LIMIT_PER_ACCOUNT} сообщ./аккаунт, задержка: {CHAT_DELAY_MIN}-{CHAT_DELAY_MAX} сек\n")

    remaining = list(chats)

    for session_path in sessions:
        if not remaining:
            break

        session_name = os.path.splitext(os.path.basename(session_path))[0]
        client = create_client(session_path)

        try:
            await client.start()
            print(f"[{session_name}] Подключен")
        except Exception as e:
            print(f"[{session_name}] Не удалось подключиться: {e}")
            continue

        sent_count = 0
        skip_account = False

        for target in remaining[:CHAT_LIMIT_PER_ACCOUNT]:
            if skip_account:
                break

            recipient = _resolve_chat(target)
            target_str = target.get("username") or str(target.get("chat_id"))

            if not recipient:
                remaining.remove(target)
                continue

            try:
                entity = await client.get_entity(recipient)
                await client.send_message(entity, message)
                print(f"  [{session_name}] Отправлено в {target_str}")
                log_chat(session_name, target_str, "sent")
                sent_count += 1
                remaining.remove(target)
            except FloodWaitError as e:
                print(f"  [{session_name}] FloodWait: {e.seconds} сек.")
                log_chat(session_name, target_str, "failed", f"FloodWait {e.seconds}s")
                skip_account = True
                break
            except PeerFloodError:
                print(f"  [{session_name}] PeerFlood — ограничен.")
                log_chat(session_name, target_str, "failed", "PeerFlood")
                skip_account = True
                break
            except (ChatWriteForbiddenError, ChannelPrivateError, SlowModeWaitError) as e:
                err = type(e).__name__
                print(f"  [{session_name}] {err} -> {target_str}")
                log_chat(session_name, target_str, "skipped", err)
                remaining.remove(target)
            except Exception as e:
                print(f"  [{session_name}] Ошибка -> {target_str}: {e}")
                log_chat(session_name, target_str, "skipped", str(e))
                remaining.remove(target)

            delay = random.uniform(CHAT_DELAY_MIN, CHAT_DELAY_MAX)
            print(f"  Задержка {delay:.0f} сек...")
            await asyncio.sleep(delay)

        try:
            await client.disconnect()
        except Exception:
            pass

        print(f"[{session_name}] Отправлено: {sent_count}\n")

    total_sent = len(chats) - len(remaining)
    print(f"\nИтого: {total_sent}/{len(chats)}")
