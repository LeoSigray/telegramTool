import asyncio
import random
import os

from telethon.errors import (
    FloodWaitError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
    PeerFloodError,
    ChatAdminRequiredError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.functions.channels import InviteToChannelRequest

from config import INVITE_DELAY_MIN, INVITE_DELAY_MAX, INVITE_LIMIT_PER_ACCOUNT
from accounts.manager import get_session_files, create_client
from data.excel_manager import load_users, log_invite


async def invite_user(client, session_name, target, target_entity, is_megagroup):
    """Инвайтит одного юзера. Возвращает (success, skip_account, error)."""
    user_id = target.get("user_id")
    username = target.get("username")
    target_str = username or str(user_id)

    recipient = int(user_id) if user_id else (f"@{username}" if username and not username.startswith("@") else username)
    try:
        user_entity = await client.get_entity(recipient)

        if is_megagroup:
            await client(InviteToChannelRequest(target_entity, [user_entity]))
        else:
            await client(AddChatUserRequest(target_entity, user_entity, fwd_limit=0))

        print(f"  [{session_name}] Приглашен: {target_str}")
        return True, False, ""

    except UserAlreadyParticipantError:
        print(f"  [{session_name}] {target_str} уже в группе.")
        return True, False, "already_participant"
    except FloodWaitError as e:
        print(f"  [{session_name}] FloodWait: {e.seconds} сек.")
        return False, True, f"FloodWait {e.seconds}s"
    except UserNotMutualContactError:
        print(f"  [{session_name}] {target_str} не взаимный контакт.")
        return True, False, "not_mutual_contact"
    except UserPrivacyRestrictedError:
        print(f"  [{session_name}] Приватность {target_str} запрещает инвайт.")
        return True, False, "privacy_restricted"
    except PeerFloodError:
        print(f"  [{session_name}] PeerFlood — аккаунт ограничен.")
        return False, True, "PeerFlood"
    except ChatAdminRequiredError:
        print(f"  [{session_name}] Нужны права администратора для инвайта.")
        return False, True, "admin_required"
    except Exception as e:
        print(f"  [{session_name}] Ошибка инвайта {target_str}: {e}")
        return True, False, str(e)


async def run_inviting(target_group):
    """
    Инвайтинг юзеров из Excel в указанную группу.
    target_group — chat_id (число) или @username группы.
    """
    users = load_users()
    if not users:
        print("Список юзеров пуст. Заполните лист 'Users' в data/targets.xlsx")
        return

    sessions = get_session_files()
    if not sessions:
        print("Нет доступных аккаунтов в папке sessions/")
        return

    print(f"\nНачинаю инвайтинг: {len(users)} юзеров, {len(sessions)} аккаунтов")
    print(f"Целевая группа: {target_group}")
    print(f"Лимит: {INVITE_LIMIT_PER_ACCOUNT} инвайтов/аккаунт, задержка: {INVITE_DELAY_MIN}-{INVITE_DELAY_MAX} сек\n")

    remaining = list(users)

    for session_path in sessions:
        if not remaining:
            break

        session_name = os.path.splitext(os.path.basename(session_path))[0]
        client = create_client(session_path)

        try:
            await client.start()
            print(f"[{session_name}] Подключен.")
        except Exception as e:
            print(f"[{session_name}] Не удалось подключиться: {e}")
            continue

        # Определяем тип группы
        try:
            group_entity = await client.get_entity(target_group)
            is_megagroup = hasattr(group_entity, "megagroup") and group_entity.megagroup
        except Exception as e:
            print(f"[{session_name}] Не удалось найти группу {target_group}: {e}")
            try:
                await client.disconnect()
            except Exception:
                pass
            continue

        invited_count = 0
        skip_account = False

        for target in remaining[:INVITE_LIMIT_PER_ACCOUNT]:
            if skip_account:
                break

            success, should_skip, error = await invite_user(
                client, session_name, target, group_entity, is_megagroup
            )

            target_str = target.get("username") or str(target.get("user_id"))

            if success:
                status = "skipped" if error else "invited"
                log_invite(session_name, target_str, status, error)
                if not error:
                    invited_count += 1
                remaining.remove(target)
            else:
                log_invite(session_name, target_str, "failed", error)
                if should_skip:
                    skip_account = True
                    break

            delay = random.uniform(INVITE_DELAY_MIN, INVITE_DELAY_MAX)
            print(f"  Задержка {delay:.1f} сек...")
            await asyncio.sleep(delay)

        try:
            await client.disconnect()
        except Exception:
            pass

        print(f"[{session_name}] Приглашено: {invited_count}\n")

    total = len(users) - len(remaining)
    print(f"\nИтого: приглашено {total}/{len(users)} юзеров")
