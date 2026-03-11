import os
from datetime import datetime

from openpyxl import Workbook, load_workbook

from config import EXCEL_FILE


def _ensure_excel():
    """Создает Excel файл с нужными листами, если его нет."""
    if os.path.exists(EXCEL_FILE):
        return

    os.makedirs(os.path.dirname(EXCEL_FILE), exist_ok=True)
    wb = Workbook()

    # Лист Users — список юзеров для ЛС и инвайтинга
    ws_users = wb.active
    ws_users.title = "Users"
    ws_users.append(["user_id", "username", "comment"])

    # Лист Chats — список чатов для рассылки
    ws_chats = wb.create_sheet("Chats")
    ws_chats.append(["chat_id", "username", "comment"])

    # Логи
    for sheet_name in ("DM_Log", "Chat_Log", "Invite_Log"):
        ws = wb.create_sheet(sheet_name)
        ws.append(["timestamp", "account", "target", "status", "error"])

    wb.save(EXCEL_FILE)
    print(f"Создан Excel файл: {EXCEL_FILE}")


def load_users():
    """Загружает список юзеров из листа Users. Возвращает list of dict."""
    _ensure_excel()
    wb = load_workbook(EXCEL_FILE, read_only=True)
    ws = wb["Users"]

    users = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        user_id, username, comment = row[0], row[1], row[2] if len(row) > 2 else ""
        if user_id or username:
            users.append({
                "user_id": int(user_id) if user_id else None,
                "username": str(username).strip() if username else None,
                "comment": comment or "",
            })
    wb.close()
    return users


def load_chats():
    """Загружает список чатов из листа Chats. Возвращает list of dict."""
    _ensure_excel()
    wb = load_workbook(EXCEL_FILE, read_only=True)
    ws = wb["Chats"]

    chats = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        chat_id, username, comment = row[0], row[1], row[2] if len(row) > 2 else ""
        if chat_id or username:
            chats.append({
                "chat_id": int(chat_id) if chat_id else None,
                "username": str(username).strip() if username else None,
                "comment": comment or "",
            })
    wb.close()
    return chats


def log_action(sheet_name, account, target, status, error=""):
    """Записывает лог действия в соответствующий лист."""
    _ensure_excel()
    wb = load_workbook(EXCEL_FILE)
    ws = wb[sheet_name]
    ws.append([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        str(account),
        str(target),
        status,
        str(error) if error else "",
    ])
    wb.save(EXCEL_FILE)
    wb.close()


def log_dm(account, target, status, error=""):
    log_action("DM_Log", account, target, status, error)


def log_chat(account, target, status, error=""):
    log_action("Chat_Log", account, target, status, error)


def log_invite(account, target, status, error=""):
    log_action("Invite_Log", account, target, status, error)
