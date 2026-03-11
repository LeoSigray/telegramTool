import os
import shutil
import zipfile
import tempfile

from config import SESSIONS_DIR, TDATA_DIR


def _find_tdata_dirs(root):
    """
    Рекурсивно ищет папки tdata (содержат файл key_datas).
    Возвращает list of (account_name, tdata_path).
    """
    found = []

    for entry in os.scandir(root):
        if not entry.is_dir():
            continue

        tdata_path = entry.path
        key_file = os.path.join(tdata_path, "key_datas")

        # Папка сама по себе является tdata/
        if os.path.isfile(key_file):
            # Имя аккаунта = родительская папка (номер телефона)
            parent_name = os.path.basename(os.path.dirname(tdata_path))
            account_name = parent_name if parent_name else entry.name
            found.append((account_name, tdata_path))
            continue

        # Внутри папки ищем подпапку tdata/
        nested = os.path.join(tdata_path, "tdata")
        if os.path.isdir(nested) and os.path.isfile(os.path.join(nested, "key_datas")):
            account_name = entry.name  # номер телефона
            found.append((account_name, nested))
            continue

        # Рекурсия на один уровень вглубь
        sub = _find_tdata_dirs(tdata_path)
        found.extend(sub)

    return found


def _import_single(account_name, tdata_path):
    """Конвертирует одну tdata папку в Telethon session."""
    try:
        from opentele.tl import TelegramClient as OpenTeleClient
        from opentele.api import UseCurrentSession
    except (ImportError, BaseException):
        print("\n[ОШИБКА] opentele не установлена или несовместима.")
        print("─" * 50)
        print("Для импорта tdata временно установи совместимые версии:")
        print("  pip uninstall telethon opentele -y")
        print('  pip install opentele==1.0.5 "telethon==1.24.0"')
        print("\nПосле импорта всех аккаунтов верни обратно:")
        print("  pip install -r requirements.txt")
        print("─" * 50)
        return False

    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(TDATA_DIR, exist_ok=True)

    tdata_copy = os.path.join(TDATA_DIR, account_name)
    if os.path.exists(tdata_copy):
        shutil.rmtree(tdata_copy)
    shutil.copytree(tdata_path, tdata_copy)

    session_path = os.path.join(SESSIONS_DIR, account_name)

    try:
        OpenTeleClient.FromTDesktop(
            tdata_copy,
            session=session_path,
            flag=UseCurrentSession,
        )
        print(f"  Импортирован: {account_name} -> sessions/{account_name}.session")
        return True
    except Exception as e:
        print(f"  Ошибка [{account_name}]: {e}")
        return False


def import_tdata_interactive():
    """
    Импорт аккаунтов из tdata.
    Принимает:
      - .zip архив (с одним или несколькими аккаунтами)
      - папку с одним аккаунтом (содержит key_datas)
      - папку с несколькими аккаунтами (подпапки с номерами телефонов)
    """
    path = input("Путь к zip-архиву или папке tdata: ").strip().strip('"')

    if not path:
        print("Путь не указан.")
        return

    if not os.path.exists(path):
        print(f"Файл/папка не найдены: {path}")
        return

    tmp_dir = None

    try:
        # --- Если zip — распаковываем во временную папку ---
        if zipfile.is_zipfile(path):
            print("Обнаружен zip-архив. Распаковываю...")
            tmp_dir = tempfile.mkdtemp(prefix="tdata_import_")
            with zipfile.ZipFile(path, "r") as zf:
                zf.extractall(tmp_dir)
            search_root = tmp_dir
        elif os.path.isdir(path):
            search_root = path
        else:
            print("Неподдерживаемый формат. Укажи .zip или папку.")
            return

        # --- Ищем все tdata внутри ---
        accounts = _find_tdata_dirs(search_root)

        # Если не нашли вложенных — возможно это и есть tdata папка
        if not accounts:
            key_file = os.path.join(search_root, "key_datas")
            if os.path.isfile(key_file):
                name = input("Имя для аккаунта (например номер телефона): ").strip() or "imported"
                accounts = [(name, search_root)]

        if not accounts:
            print("tdata папки не найдены. Убедитесь что архив содержит файл key_datas.")
            return

        print(f"\nНайдено аккаунтов: {len(accounts)}")
        for name, tpath in accounts:
            print(f"  {name}  ({tpath})")

        confirm = input("\nИмпортировать все? (y/n): ").strip().lower()
        if confirm != "y":
            print("Отменено.")
            return

        print()
        ok = 0
        for account_name, tdata_path in accounts:
            if _import_single(account_name, tdata_path):
                ok += 1

        print(f"\nИмпортировано: {ok}/{len(accounts)} аккаунтов")
        if ok > 0:
            print("Проверить аккаунты: меню -> Управление аккаунтами -> Проверить аккаунты")

    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
