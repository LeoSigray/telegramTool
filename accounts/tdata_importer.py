import os
import shutil

from config import SESSIONS_DIR, TDATA_DIR


def import_tdata_interactive():
    """
    Интерактивный импорт tdata папки.
    Использует opentele для конвертации tdata -> Telethon session.

    ВАЖНО: opentele требует отдельной установки совместимой версии:
        pip install opentele==1.0.5 "telethon==1.24.0"
    """
    path = input("Путь к папке tdata: ").strip()

    if not path:
        print("Путь не указан.")
        return

    if not os.path.isdir(path):
        print(f"Папка не найдена: {path}")
        return

    # Если указана родительская папка — ищем вложенную tdata
    key_data = os.path.join(path, "key_datas")
    if not os.path.exists(key_data):
        tdata_subdir = os.path.join(path, "tdata")
        if os.path.isdir(tdata_subdir):
            path = tdata_subdir
            print(f"Найдена подпапка tdata, использую: {path}")

    # Пробуем импортировать opentele
    try:
        from opentele.tl import TelegramClient as OpenTeleClient
        from opentele.api import UseCurrentSession
    except (ImportError, BaseException):
        print("\n[ОШИБКА] Не удалось загрузить opentele.")
        print("─" * 50)
        print("opentele конфликтует с telethon 1.42+.")
        print("\nДля импорта tdata установи совместимые версии:")
        print("  pip uninstall telethon opentele -y")
        print('  pip install opentele==1.0.5 "telethon==1.24.0"')
        print("\nПосле импорта верни обратно:")
        print("  pip install -r requirements.txt")
        print("─" * 50)
        return

    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(TDATA_DIR, exist_ok=True)

    account_name = input("Имя для аккаунта (например номер телефона): ").strip()
    if not account_name:
        account_name = os.path.basename(os.path.dirname(path)) or "imported"

    tdata_copy = os.path.join(TDATA_DIR, account_name)

    if os.path.exists(tdata_copy):
        overwrite = input(f"Папка {tdata_copy} уже существует. Перезаписать? (y/n): ").strip().lower()
        if overwrite != "y":
            return
        shutil.rmtree(tdata_copy)

    print(f"Копирую tdata в {tdata_copy}...")
    shutil.copytree(path, tdata_copy)

    try:
        session_path = os.path.join(SESSIONS_DIR, account_name)
        OpenTeleClient.FromTDesktop(
            tdata_copy,
            session=session_path,
            flag=UseCurrentSession,
        )
        print(f"\nАккаунт импортирован: {session_path}.session")
        print("Проверить аккаунт можно через меню -> Управление аккаунтами -> Проверить аккаунты.")
    except Exception as e:
        print(f"\nОшибка импорта tdata: {e}")
        print("Убедитесь что tdata содержит корректную сессию Telegram Desktop.")
