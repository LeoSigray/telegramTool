"""
data/gsheets_manager.py
Экспорт спарсенных чатов в Google Sheets.

Требования:
  pip install gspread google-auth pandas

Настройка:
  1. Создай Service Account в Google Cloud Console
  2. Скачай credentials.json
  3. Дай доступ Service Account к таблице (Editor)
  4. Укажи SPREADSHEET_ID ниже
"""

import time
from typing import Optional

from data.excel_manager import get_category_names, get_category_stats, load_chats_by_category

# ==========================
# НАСТРОЙКИ — ЗАПОЛНИ ЗДЕСЬ
# ==========================
SPREADSHEET_ID = "1pVtuc8Iqf1qWJDdkBYKhhVL7Pds83Jcb95-Rdrl-_M8"          # ID таблицы из URL: /spreadsheets/d/{ID}/
CREDENTIALS_FILE = "credentials.json"  # путь к файлу сервисного аккаунта

SHEET_COLS = ["ID", "Название", "Username", "Ссылка", "Тип", "Подписчиков", "Категория", "Город"]
SHEETNAME_MAXLEN = 30


def _check_deps():
    """Проверяет наличие нужных зависимостей."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        import pandas as pd
        return True
    except ImportError as e:
        print(f"Отсутствует зависимость: {e}")
        print("Установи: pip install gspread google-auth pandas")
        return False


def _get_client():
    """Создаёт клиент Google Sheets."""
    import gspread
    from google.oauth2.service_account import Credentials

    import os
    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(
            f"Файл {CREDENTIALS_FILE} не найден.\n"
            "Создай Service Account в Google Cloud Console и скачай credentials.json"
        )

    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scope)
    return gspread.authorize(creds)


def _sanitize_name(name: str) -> str:
    import re
    name = (name or "Другое").strip()
    name = re.sub(r"[\[\]\*:/\\\?\n]", " ", name)
    return name[:SHEETNAME_MAXLEN]


def _ensure_worksheet(sh, title: str):
    """Получает или создаёт лист с нужным именем."""
    title = _sanitize_name(title)
    try:
        return sh.worksheet(title)
    except Exception:
        time.sleep(0.5)
        return sh.add_worksheet(title=title, rows="5000", cols="10")


def _write_sheet(sh, sheet_name: str, rows: list):
    """Записывает строки в лист, предварительно очищая его."""
    ws = _ensure_worksheet(sh, sheet_name)
    try:
        ws.clear()
    except Exception:
        pass
    if rows:
        ws.update("A1", rows)
    time.sleep(0.3)  # анти-рейтлимит Google API


def _chats_to_rows(chats: list, category: str) -> list:
    """Конвертирует список чатов из excel_manager в строки для Google Sheets."""
    from parsing.categories import detect_city

    rows = [SHEET_COLS]  # заголовок
    for chat in chats:
        # chat может быть dict или объект с атрибутами
        if isinstance(chat, dict):
            chat_id = str(chat.get("chat_id", ""))
            username = str(chat.get("username", ""))
            title = str(chat.get("title", "") or chat.get("comment", ""))
        else:
            chat_id = str(getattr(chat, "chat_id", ""))
            username = str(getattr(chat, "username", ""))
            title = str(getattr(chat, "title", "") or getattr(chat, "comment", ""))

        link = f"https://t.me/{username.lstrip('@')}" if username else ""
        city = detect_city(title)

        rows.append([
            chat_id,
            title,
            username,
            link,
            "Канал/Группа",
            "",
            category,
            city,
        ])
    return rows


def export_to_gsheets(selected_categories: Optional[list] = None) -> dict:
    """
    Экспортирует чаты из Excel в Google Sheets.
    selected_categories=None → все категории.
    Возвращает статистику по категориям.
    """
    if not _check_deps():
        return {}

    if not SPREADSHEET_ID:
        print("Ошибка: заполни SPREADSHEET_ID в data/gsheets_manager.py")
        return {}

    gc = _get_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    categories = selected_categories or get_category_names()
    if not categories:
        print("Нет категорий для экспорта. Сначала запусти парсинг.")
        return {}

    stats = {}
    all_rows = [SHEET_COLS]

    print(f"\nЭкспорт в Google Sheets (таблица: {SPREADSHEET_ID[:20]}...)")
    print("-" * 40)

    for cat in categories:
        chats = load_chats_by_category([cat])
        if not chats:
            print(f"  [{cat}] пусто — пропуск")
            continue

        rows = _chats_to_rows(chats, cat)
        _write_sheet(sh, cat, rows)
        count = len(rows) - 1  # без заголовка
        print(f"  [{cat}] → {count} чатов")
        stats[cat] = count
        all_rows += rows[1:]  # добавляем в общий лист без заголовка

    # Лист "ВСЁ" — все категории вместе
    if all_rows:
        _write_sheet(sh, "ВСЁ", all_rows)
        print(f"\n  [ВСЁ] → {len(all_rows) - 1} чатов")

    return stats


def export_to_gsheets_interactive():
    """Интерактивный экспорт в Google Sheets из меню."""
    print("\n--- Экспорт в Google Sheets ---")

    if not _check_deps():
        return

    if not SPREADSHEET_ID:
        print("Сначала заполни SPREADSHEET_ID в файле data/gsheets_manager.py")
        print("и положи credentials.json рядом с main.py")
        return

    categories = get_category_names()
    if not categories:
        print("Нет данных для экспорта. Сначала запусти парсинг (пункт 5).")
        return

    stats = get_category_stats()
    print("Категории:")
    for i, cat in enumerate(categories, 1):
        print(f"  {i}. {cat} ({stats.get(cat, 0)})")

    print("\n  0. Все категории")
    sel = input("\nНомера через запятую (или 0=все): ").strip()

    if sel == "0" or not sel:
        selected = None
    else:
        selected = []
        for part in sel.split(","):
            try:
                idx = int(part.strip()) - 1
                if 0 <= idx < len(categories):
                    selected.append(categories[idx])
            except ValueError:
                pass
        if not selected:
            print("Ничего не выбрано.")
            return

    try:
        result = export_to_gsheets(selected)
        total = sum(result.values())
        print(f"\nГотово! Экспортировано {total} чатов в {len(result)} листов.")
        print(f"Таблица: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")
    except Exception as e:
        print(f"Ошибка экспорта: {e}")
