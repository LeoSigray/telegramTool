import asyncio
import random
import os

from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    ChannelPrivateError,
    PeerFloodError,
    SlowModeWaitError,
)

from config import CHAT_DELAY_MIN, CHAT_DELAY_MAX, CHAT_LIMIT_PER_ACCOUNT
from accounts.manager import get_session_files, create_client
from data.excel_manager import load_chats, log_chat


async def send_to_chat(client, session_name, target, message):
    """Отправляет сообщение в чат/группу/канал. Возвращает (success, error)."""
    chat_id = target.get("chat_id")
    username = target.get("username")
    recipient = chat_id or username

    try:
        entity = await client.get_entity(recipient)
        await client.send_message(entity, message)
        target_str = f"{username or chat_id}"
        print(f"  [{session_name}] Отправлено в {target_str}")
        return True, ""
    except FloodWaitError as e:
        print(f"  [{session_name}] FloodWait: {e.seconds} сек.")
        return False, f"FloodWait {e.seconds}s"
    except SlowModeWaitError as e:
        print(f"  [{session_name}] SlowMode: ждать {e.seconds} сек. Пропускаю чат.")
        return True, f"SlowMode {e.seconds}s"
    except ChatWriteForbiddenError:
        print(f"  [{session_name}] Нет прав писать в {recipient}")
        return True, "write_forbidden"
    except ChannelPrivateError:
        print(f"  [{session_name}] Канал/группа {recipient} приватная")
        return True, "channel_private"
    except PeerFloodError:
        print(f"  [{session_name}] PeerFlood — аккаунт ограничен.")
        return False, "PeerFlood"
    except Exception as e:
        print(f"  [{session_name}] Ошибка -> {recipient}: {e}")
        return True, str(e)


async def run_chat_sending(message):
    """Рассылка по чатам/группам/каналам из Excel."""
    chats = load_chats()
    if not chats:
        print("Список чатов пуст. Заполните лист 'Chats' в data/targets.xlsx")
        return

    sessions = get_session_files()
    if not sessions:
        print("Нет доступных аккаунтов в папке sessions/")
        return

    print(f"\nНачинаю рассылку по чатам: {len(chats)} чатов, {len(sessions)} аккаунтов")
    print(f"Лимит: {CHAT_LIMIT_PER_ACCOUNT} сообщ./аккаунт, задержка: {CHAT_DELAY_MIN}-{CHAT_DELAY_MAX} сек\n")

    remaining = list(chats)

    for session_path in sessions:
        if not remaining:
            break

        session_name = os.path.splitext(os.path.basename(session_path))[0]
        client = create_client(session_path)

        try:
            await client.start()
            print(f"[{session_name}] Подключен. Рассылка по чатам...")
        except Exception as e:
            print(f"[{session_name}] Не удалось подключиться: {e}")
            continue

        sent_count = 0
        skip_account = False

        for target in remaining[:CHAT_LIMIT_PER_ACCOUNT]:
            if skip_account:
                break

            success, error = await send_to_chat(client, session_name, target, message)

            target_str = target.get("username") or str(target.get("chat_id"))

            if success:
                status = "skipped" if error else "sent"
                log_chat(session_name, target_str, status, error)
                if not error:
                    sent_count += 1
                remaining.remove(target)
            else:
                log_chat(session_name, target_str, "failed", error)
                skip_account = True
                break

            delay = random.uniform(CHAT_DELAY_MIN, CHAT_DELAY_MAX)
            print(f"  Задержка {delay:.1f} сек...")
            await asyncio.sleep(delay)

        try:
            await client.disconnect()
        except Exception:
            pass

        print(f"[{session_name}] Отправлено: {sent_count} сообщений\n")

    total_sent = len(chats) - len(remaining)
    print(f"\nИтого: отправлено в {total_sent}/{len(chats)} чатов")
