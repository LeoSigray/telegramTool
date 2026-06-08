"""
Конвертер уже купленных аккаунтов из accounts/*.txt в sessions/*.session
Поддерживает два формата LZT:
  1. login = hex auth_key (512 символов), dc_id = 1 (типичный для LZT)
  2. telegram_json содержит auth_key и dc_id напрямую

Запуск: python convert_accounts.py
"""

import os
import json
import sqlite3

from config import ACCOUNTS_DIR, SESSIONS_DIR

DC_IPS = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}


def parse_account_txt(file_path: str) -> dict:
    data = {}
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                key, _, value = line.partition("=")
                data[key.strip()] = value.strip()
    return data


def _make_session_file(item_id: str, dc_id: int, auth_key: bytes):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    session_path = os.path.join(SESSIONS_DIR, f"{item_id}.session")

    if os.path.exists(session_path):
        os.remove(session_path)

    db = sqlite3.connect(session_path)
    db.executescript("""
        CREATE TABLE version (version integer primary key);
        CREATE TABLE sessions (
            dc_id integer primary key,
            server_address text,
            port integer,
            auth_key blob,
            takeout_id integer
        );
        CREATE TABLE entities (
            id integer primary key,
            hash integer not null,
            username text,
            phone integer,
            name text,
            date integer
        );
        CREATE TABLE sent_files (
            md5_digest blob, file_size integer, type integer,
            id integer, hash integer,
            primary key(md5_digest, file_size, type)
        );
        CREATE TABLE update_state (
            id integer primary key, pts integer, qts integer,
            date integer, seq integer
        );
        INSERT INTO version VALUES (7);
    """)
    ip = DC_IPS.get(int(dc_id), DC_IPS[1])
    db.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        (int(dc_id), ip, 443, auth_key, None),
    )
    db.commit()
    db.close()
    return session_path


def convert_account(item_id: str, account_data: dict):
    login = account_data.get("login", "")
    telegram_json_raw = account_data.get("telegram_json", "")

    auth_key = None
    dc_id = None  # не ставим дефолт — ищем явно

    tj = {}
    if telegram_json_raw:
        try:
            tj = json.loads(telegram_json_raw)
        except Exception:
            pass

    # --- auth_key из telegram_json ---
    ak_hex = tj.get("auth_key") or tj.get("authKey") or ""
    if ak_hex:
        try:
            candidate = bytes.fromhex(ak_hex)
            if len(candidate) == 256:
                auth_key = candidate
        except Exception:
            pass

    # --- auth_key из поля login (512 hex символов = 256 байт) ---
    if auth_key is None and len(login) == 512:
        try:
            candidate = bytes.fromhex(login)
            if len(candidate) == 256:
                auth_key = candidate
        except Exception:
            pass

    if auth_key is None:
        return None

    # --- DC ID: ищем во всех местах ---
    # 1. Прямо в полях .txt файла (сохраняется новым lzt_buyer.py)
    for key in ("dc_id", "dcId", "telegram_dc"):
        if account_data.get(key):
            try:
                dc_id = int(account_data[key])
                break
            except Exception:
                pass

    # 2. В telegram_json
    if not dc_id:
        raw = tj.get("dc_id") or tj.get("dcId")
        if raw:
            try:
                dc_id = int(raw)
            except Exception:
                pass

    # 3. Дефолт для LZT — DC 1 (самый частый)
    if not dc_id:
        dc_id = 1
        print(f"  [convert] DC ID не найден для {item_id}, использую dc=1")

    return _make_session_file(item_id, dc_id, auth_key)


def convert_all_accounts() -> dict:
    stats = {"ok": [], "skip": [], "fail": []}

    if not os.path.exists(ACCOUNTS_DIR):
        print(f"Папка {ACCOUNTS_DIR}/ не найдена.")
        return stats

    txt_files = [f for f in os.listdir(ACCOUNTS_DIR) if f.endswith(".txt")]

    if not txt_files:
        print(f"В папке {ACCOUNTS_DIR}/ нет .txt файлов.")
        return stats

    print(f"Найдено аккаунтов в accounts/: {len(txt_files)}")
    print("-" * 40)

    for filename in sorted(txt_files):
        item_id = filename.replace(".txt", "")
        session_path = os.path.join(SESSIONS_DIR, f"{item_id}.session")
        txt_path = os.path.join(ACCOUNTS_DIR, filename)

        if os.path.exists(session_path):
            print(f"  [skip] {item_id} — сессия уже существует")
            stats["skip"].append(item_id)
            continue

        account_data = parse_account_txt(txt_path)
        result = convert_account(item_id, account_data)

        if result:
            print(f"  [ok]   {item_id} → sessions/{item_id}.session")
            stats["ok"].append(item_id)
        else:
            print(f"  [fail] {item_id} — не удалось извлечь auth_key")
            stats["fail"].append(item_id)

    return stats


def convert_accounts_interactive():
    print("\n--- Конвертация accounts → sessions ---")

    txt_count = len([f for f in os.listdir(ACCOUNTS_DIR) if f.endswith(".txt")]) \
        if os.path.exists(ACCOUNTS_DIR) else 0
    session_count = len([f for f in os.listdir(SESSIONS_DIR) if f.endswith(".session")]) \
        if os.path.exists(SESSIONS_DIR) else 0

    print(f"  В accounts/: {txt_count} файлов")
    print(f"  В sessions/: {session_count} файлов")

    if txt_count == 0:
        print("Нечего конвертировать.")
        return

    print(f"\nНачать конвертацию? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    print()
    stats = convert_all_accounts()

    print("-" * 40)
    print(f"Готово:")
    print(f"  Сконвертировано:       {len(stats['ok'])}")
    print(f"  Пропущено (уже есть):  {len(stats['skip'])}")
    print(f"  Ошибок:                {len(stats['fail'])}")

    if stats["ok"]:
        print("\nТеперь проверь аккаунты: меню → 1 → 4")


if __name__ == "__main__":
    print("=== Конвертация accounts → sessions ===\n")
    stats = convert_all_accounts()
    print(f"\n--- Итого ---")
    print(f"  OK:        {len(stats['ok'])}")
    print(f"  Пропущено: {len(stats['skip'])}")
    print(f"  Ошибок:    {len(stats['fail'])}")