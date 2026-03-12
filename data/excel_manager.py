import os
from datetime import datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

from config import EXCEL_FILE


def _ensure_excel():
    """Создает Excel файл с примерами, если его нет."""
    if os.path.exists(EXCEL_FILE):
        return

    os.makedirs(os.path.dirname(EXCEL_FILE), exist_ok=True)
    wb = Workbook()

    # ── Лист Users ──────────────────────────────────────────────────────────
    ws_users = wb.active
    ws_users.title = "Users"
    _header(ws_users, ["username", "user_id", "comment"])
    # Примеры — просто заполни username (@ не обязателен)
    ws_users.append(["durov", "", "пример по username"])
    ws_users.append(["", "123456789", "пример по user_id"])
    ws_users.column_dimensions["A"].width = 25
    ws_users.column_dimensions["B"].width = 18
    ws_users.column_dimensions["C"].width = 30

    # ── Лист Chats ──────────────────────────────────────────────────────────
    ws_chats = wb.create_sheet("Chats")
    _header(ws_chats, ["username", "chat_id", "comment"])
    ws_chats.append(["telegram", "", "пример по username"])
    ws_chats.append(["", "-1001234567890", "пример по chat_id"])
    ws_chats.column_dimensions["A"].width = 25
    ws_chats.column_dimensions["B"].width = 20
    ws_chats.column_dimensions["C"].width = 30

    # ── Логи ────────────────────────────────────────────────────────────────
    for sheet_name in ("DM_Log", "Chat_Log", "Invite_Log"):
        ws = wb.create_sheet(sheet_name)
        _header(ws, ["timestamp", "account", "target", "status", "error"])
        ws.column_dimensions["A"].width = 20
        ws.column_dimensions["B"].width = 20
        ws.column_dimensions["C"].width = 25
        ws.column_dimensions["D"].width = 15
        ws.column_dimensions["E"].width = 35

    wb.save(EXCEL_FILE)
    print(f"Создан Excel файл с примерами: {EXCEL_FILE}")
    print("  -> Заполни листы 'Users' и 'Chats', затем запусти рассылку.")


def _header(ws, columns):
    """Добавляет шапку с форматированием."""
    ws.append(columns)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2F5496")
        cell.alignment = Alignment(horizontal="center")


def _normalize_username(val) -> str | None:
    """Нормализует username: убирает @ если есть, возвращает None если пусто."""
    if not val:
        return None
    s = str(val).strip()
    if not s:
        return None
    return s.lstrip("@")


def load_users():
    """
    Загружает список юзеров из листа Users.
    Колонки: username | user_id | comment  (порядок не важен, ищем по заголовку)
    Возвращает list of dict.
    """
    _ensure_excel()
    wb = load_workbook(EXCEL_FILE, read_only=True)
    ws = wb["Users"]

    # Определяем индексы колонок по заголовку (первая строка)
    headers = [str(c.value).strip().lower() if c.value else "" for c in next(ws.iter_rows(min_row=1, max_row=1))]
    idx_username = _col_idx(headers, ["username", "юзернейм", "user"])
    idx_uid      = _col_idx(headers, ["user_id", "id", "userid"])

    users = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        username = _normalize_username(row[idx_username] if idx_username is not None else None)
        user_id  = row[idx_uid] if idx_uid is not None else None

        if user_id:
            try:
                user_id = int(str(user_id).strip())
            except ValueError:
                user_id = None

        if username or user_id:
            users.append({
                "user_id": user_id,
                "username": username,
            })

    wb.close()
    return users


def load_chats():
    """
    Загружает список чатов из листа Chats.
    Колонки: username | chat_id | comment
    """
    _ensure_excel()
    wb = load_workbook(EXCEL_FILE, read_only=True)
    ws = wb["Chats"]

    headers = [str(c.value).strip().lower() if c.value else "" for c in next(ws.iter_rows(min_row=1, max_row=1))]
    idx_username = _col_idx(headers, ["username", "юзернейм", "chat"])
    idx_cid      = _col_idx(headers, ["chat_id", "id", "chatid"])

    chats = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        username = _normalize_username(row[idx_username] if idx_username is not None else None)
        chat_id  = row[idx_cid] if idx_cid is not None else None

        if chat_id:
            try:
                chat_id = int(str(chat_id).strip())
            except ValueError:
                chat_id = None

        if username or chat_id:
            chats.append({
                "chat_id": chat_id,
                "username": username,
            })

    wb.close()
    return chats


def _col_idx(headers: list, names: list):
    """Возвращает индекс первой колонки с именем из списка names."""
    for name in names:
        if name in headers:
            return headers.index(name)
    return None


def log_action(sheet_name, account, target, status, error=""):
    """
    Записывает лог действия в соответствующий лист.
    Если файл занят другой программой — сохраняет во временный файл рядом.
    """
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

    # Пробуем сохранить напрямую
    try:
        wb.save(EXCEL_FILE)
        wb.close()
        return
    except PermissionError:
        pass

    # Файл занят (открыт в Excel/PyCharm) — сохраняем рядом как log_pending.xlsx
    wb.close()
    pending = os.path.join(os.path.dirname(EXCEL_FILE), "log_pending.xlsx")
    try:
        if os.path.exists(pending):
            # Объединяем с уже накопленным pending файлом
            existing = load_workbook(pending)
            if sheet_name in existing.sheetnames:
                ex_ws = existing[sheet_name]
            else:
                ex_ws = existing.create_sheet(sheet_name)
                ex_ws.append(["timestamp", "account", "target", "status", "error"])
            ex_ws.append([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(account), str(target), status, str(error) if error else "",
            ])
            existing.save(pending)
            existing.close()
        else:
            wb2 = load_workbook(EXCEL_FILE)
            wb2[sheet_name].append([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(account), str(target), status, str(error) if error else "",
            ])
            wb2.save(pending)
            wb2.close()
        print(f"  [лог] targets.xlsx занят — запись сохранена в log_pending.xlsx")
    except Exception as e:
        print(f"  [лог] Не удалось записать лог: {e}")


def log_dm(account, target, status, error=""):
    log_action("DM_Log", account, target, status, error)


def log_chat(account, target, status, error=""):
    log_action("Chat_Log", account, target, status, error)


def log_invite(account, target, status, error=""):
    log_action("Invite_Log", account, target, status, error)
