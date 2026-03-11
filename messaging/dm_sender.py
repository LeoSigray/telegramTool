import asyncio
import random
import os

from telethon.errors import (
    FloodWaitError,
    UserPrivacyRestrictedError,
    PeerFloodError,
    UserBannedInChannelError,
)

from config import DM_DELAY_MIN, DM_DELAY_MAX, DM_LIMIT_PER_ACCOUNT
from accounts.manager import get_session_files, create_client
from data.excel_manager import load_users, log_dm


async def send_dm_to_user(client, session_name, target, message):
    """Отправляет ЛС одному пользователю. Возвращает (success, error)."""
    user_id = target.get("user_id")
    username = target.get("username")
    recipient = user_id or username

    try:
        entity = await client.get_entity(recipient)
        await client.send_message(entity, message)
        target_str = f"{username or user_id}"
        print(f"  [{session_name}] Отправлено -> {target_str}")
        return True, ""
    except FloodWaitError as e:
        print(f"  [{session_name}] FloodWait: {e.seconds} сек.")
        return False, f"FloodWait {e.seconds}s"
    except UserPrivacyRestrictedError:
        print(f"  [{session_name}] Приватность запрещает ЛС -> {recipient}")
        return True, "privacy_restricted"  # не retry, пропускаем
    except PeerFloodError:
        print(f"  [{session_name}] PeerFlood — аккаунт ограничен.")
        return False, "PeerFlood"
    except Exception as e:
        print(f"  [{session_name}] Ошибка -> {recipient}: {e}")
        return True, str(e)  # пропускаем юзера, но продолжаем


async def run_dm_sending(message):
    """Рассылка в ЛС по списку юзеров из Excel."""
    users = load_users()
    if not users:
        print("Список юзеров пуст. Заполните лист 'Users' в data/targets.xlsx")
        return

    sessions = get_session_files()
    if not sessions:
        print("Нет доступных аккаунтов в папке sessions/")
        return

    print(f"\nНачинаю рассылку в ЛС: {len(users)} юзеров, {len(sessions)} аккаунтов")
    print(f"Лимит: {DM_LIMIT_PER_ACCOUNT} ЛС/аккаунт, задержка: {DM_DELAY_MIN}-{DM_DELAY_MAX} сек\n")

    remaining = list(users)

    for session_path in sessions:
        if not remaining:
            break

        session_name = os.path.splitext(os.path.basename(session_path))[0]
        client = create_client(session_path)

        try:
            await client.start()
            print(f"[{session_name}] Подключен. Отправляю ЛС...")
        except Exception as e:
            print(f"[{session_name}] Не удалось подключиться: {e}")
            continue

        sent_count = 0
        skip_account = False

        for target in remaining[:DM_LIMIT_PER_ACCOUNT]:
            if skip_account:
                break

            success, error = await send_dm_to_user(client, session_name, target, message)

            target_str = target.get("username") or str(target.get("user_id"))

            if success:
                if error == "privacy_restricted":
                    log_dm(session_name, target_str, "skipped", error)
                else:
                    log_dm(session_name, target_str, "sent")
                    sent_count += 1
                remaining.remove(target)
            else:
                # FloodWait или PeerFlood — переходим к следующему аккаунту
                log_dm(session_name, target_str, "failed", error)
                skip_account = True
                break

            # Рандомная задержка
            delay = random.uniform(DM_DELAY_MIN, DM_DELAY_MAX)
            print(f"  Задержка {delay:.1f} сек...")
            await asyncio.sleep(delay)

        try:
            await client.disconnect()
        except Exception:
            pass

        print(f"[{session_name}] Отправлено: {sent_count} ЛС\n")

    total_sent = len(users) - len(remaining)
    print(f"\nИтого: отправлено {total_sent}/{len(users)} ЛС")
    if remaining:
        print(f"Не отправлено: {len(remaining)} (недостаточно аккаунтов или лимиты)")
