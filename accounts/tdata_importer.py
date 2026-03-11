import os
import shutil

from config import SESSIONS_DIR, TDATA_DIR


def import_tdata_interactive():
    """
    Интерактивный импорт tdata папки.
    Использует opentele для конвертации tdata -> Telethon session.
    """
    path = input("Путь к папке tdata: ").strip()

    if not path:
        print("Путь не указан.")
        return

    if not os.path.isdir(path):
        print(f"Папка не найдена: {path}")
        return

    # Проверяем наличие ключевых файлов tdata
    key_data = os.path.join(path, "key_datas")
    if not os.path.exists(key_data):
        # Возможно пользователь указал родительскую папку
        tdata_subdir = os.path.join(path, "tdata")
        if os.path.isdir(tdata_subdir):
            path = tdata_subdir
            print(f"Найдена подпапка tdata, использую: {path}")

    try:
        from opentele.tl import TelegramClient as OpenTeleClient
        from opentele.api import UseCurrentSession
    except ImportError:
        print("Библиотека opentele не установлена.")
        print("Установите: pip install opentele")
        return

    os.makedirs(SESSIONS_DIR, exist_ok=True)

    # Копируем tdata во временную папку для безопасности
    account_name = input("Имя для аккаунта (например phone или id): ").strip()
    if not account_name:
        account_name = os.path.basename(os.path.dirname(path)) or "imported"

    tdata_copy = os.path.join(TDATA_DIR, account_name)
    os.makedirs(TDATA_DIR, exist_ok=True)

    if os.path.exists(tdata_copy):
        print(f"Папка {tdata_copy} уже существует. Перезаписать? (y/n): ", end="")
        if input().strip().lower() != "y":
            return
        shutil.rmtree(tdata_copy)

    print(f"Копирую tdata в {tdata_copy}...")
    shutil.copytree(path, tdata_copy)

    try:
        # Конвертация tdata -> Telethon session
        session_path = os.path.join(SESSIONS_DIR, account_name)
        client = OpenTeleClient.FromTDesktop(
            tdata_copy,
            session=session_path,
            flag=UseCurrentSession,
        )
        print(f"Аккаунт импортирован: {session_path}.session")
        print("Для проверки используйте пункт 'Список аккаунтов' в меню.")
    except Exception as e:
        print(f"Ошибка импорта tdata: {e}")
        print("Убедитесь что tdata содержит корректную сессию.")
