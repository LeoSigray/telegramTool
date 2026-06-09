import os
import glob
import sqlite3

from telethon import TelegramClient

from config import CONFIG, SESSIONS_DIR
from proxy_manager import get_telethon_proxy


def fix_session_version(session_path: str) -> bool:
    """
    Исправляет версию .session файла до 7 (Telethon CURRENT_VERSION).
    Нужно если сессия была создана со старой версией — Telethon падал с
    'table update_state already exists' при апгрейде.
    Возвращает True если была произведена правка.
    """
    try:
        db = sqlite3.connect(session_path)
        row = db.execute("SELECT version FROM version LIMIT 1").fetchone()
        if row and row[0] < 7:
            # Дорастим схему вручную до версии 7
            old = row[0]
            c = db.cursor()
            if old <= 2:
                try:
                    c.execute("DROP TABLE IF EXISTS sent_files")
                    c.execute("""CREATE TABLE sent_files (
                        md5_digest blob, file_size integer, type integer,
                        id integer, hash integer,
                        primary key(md5_digest, file_size, type))""")
                except Exception:
                    pass
            if old <= 3:
                try:
                    c.execute("""CREATE TABLE IF NOT EXISTS update_state (
                        id integer primary key, pts integer, qts integer,
                        date integer, seq integer)""")
                except Exception:
                    pass
            if old <= 4:
                try:
                    c.execute("ALTER TABLE sessions ADD COLUMN takeout_id integer")
                except Exception:
                    pass
            if old <= 5:
                try:
                    c.execute("DELETE FROM entities")
                except Exception:
                    pass
            if old <= 6:
                try:
                    c.execute("ALTER TABLE entities ADD COLUMN date integer")
                except Exception:
                    pass
            c.execute("UPDATE version SET version = 7")
            db.commit()
            db.close()
            return True
        db.close()
    except Exception:
        pass
    return False


def migrate_all_sessions():
    """Проверяет и исправляет все .session файлы в папке sessions/."""
    for path in get_session_files():
        if fix_session_version(path):
            name = os.path.splitext(os.path.basename(path))[0]
            print(f"  [migrate] Обновлена сессия: {name}")


def get_session_files():
    """
    Возвращает список .session файлов из папки sessions/.
    Перед этим выгружает свежие сессии из БД (data/database.db) на диск,
    чтобы новые аккаунты, добавленные с другой машины, стали доступны.
    """
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    try:
        from data.db import load_sessions_to_disk
        load_sessions_to_disk(SESSIONS_DIR)
    except Exception as e:
        print(f"[db] Предупреждение: не удалось загрузить сессии из БД: {e}")
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


async def update_bio(bio: str) -> dict:
    """
    Устанавливает описание профиля (about) на всех аккаунтах.
    Возвращает {session_name: "ok"/"error: ..."}.
    """
    from telethon.tl.functions.account import UpdateProfileRequest

    sessions = get_session_files()
    if not sessions:
        return {}

    results = {}
    for session_path in sessions:
        session_name = os.path.splitext(os.path.basename(session_path))[0]
        client = create_client(session_path)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                results[session_name] = "error: не авторизован"
                continue
            await client(UpdateProfileRequest(about=bio))
            results[session_name] = "ok"
        except Exception as e:
            results[session_name] = f"error: {e}"
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    return results


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
