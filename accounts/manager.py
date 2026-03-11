import os
import glob

from telethon import TelegramClient

from config import CONFIG, SESSIONS_DIR
from proxy_manager import get_telethon_proxy


def get_session_files():
    """Возвращает список .session файлов из папки sessions/."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    pattern = os.path.join(SESSIONS_DIR, "*.session")
    return sorted(glob.glob(pattern))


def list_accounts():
    """Выводит список доступных аккаунтов."""
    sessions = get_session_files()
    if not sessions:
        print("Нет доступных аккаунтов (папка sessions/ пуста).")
        return []

    print(f"\nДоступные аккаунты ({len(sessions)}):")
    for i, path in enumerate(sessions, 1):
        name = os.path.splitext(os.path.basename(path))[0]
        print(f"  {i}. {name}")
    return sessions


def create_client(session_path):
    """
    Создает TelegramClient из .session файла с прокси (если настроен).
    session_path — полный путь к .session файлу.
    """
    session_name = os.path.splitext(session_path)[0]  # без .session
    proxy = get_telethon_proxy()

    client = TelegramClient(
        session_name,
        CONFIG["TELEGRAM_API_ID"],
        CONFIG["TELEGRAM_API_HASH"],
        proxy=proxy,
    )
    return client


async def check_account(session_path):
    """Проверяет работоспособность аккаунта. Возвращает (ok, info)."""
    client = create_client(session_path)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return False, "Не авторизован"
        me = await client.get_me()
        name = me.first_name or ""
        if me.last_name:
            name += f" {me.last_name}"
        info = f"{name} (@{me.username or 'N/A'}, id={me.id})"
        await client.disconnect()
        return True, info
    except Exception as e:
        return False, str(e)
