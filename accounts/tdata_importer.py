"""
Конвертер tdata (Telegram Desktop) -> Telethon .session
Работает без opentele, только tgcrypto (уже в requirements.txt).
"""

import os
import shutil
import sqlite3
import struct
import hashlib
import zipfile
import tempfile

try:
    import tgcrypto
    _TGCRYPTO_OK = True
except ImportError:
    _TGCRYPTO_OK = False

from config import SESSIONS_DIR, TDATA_DIR

# DC IP-адреса (production)
DC_IPS = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}


# ─────────────────────────── TDF низкоуровневый разбор ───────────────────────

def _parse_tdf_raw(path: str) -> bytes:
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:4] != b"TDF$":
        raise ValueError(f"Не TDF файл: {path}")
    return bytes(raw[8:-4])   # обрезаем magic + version + crc32


def _qt_read_bytes(data: bytes, pos: int):
    """Читает QByteArray: 4 байта длина (big-endian) + данные."""
    length = struct.unpack_from(">I", data, pos)[0]
    return bytes(data[pos + 4: pos + 4 + length]), pos + 4 + length


def _create_local_key(passcode: bytes, salt: bytes) -> bytes:
    """PBKDF2-SHA512 деривация ключа из соли и пасскода."""
    h = hashlib.sha512(salt)
    h.update(passcode)
    h.update(salt)
    iterations = 1 if not passcode else 100000
    return hashlib.pbkdf2_hmac("sha512", h.digest(), salt, iterations, 256)


def _prepare_aes(key256: bytes, msg_key: bytes):
    """Вычисляет AES-ключ и IV по алгоритму MTProto oldmtp (receive side)."""
    x = 8
    a = hashlib.sha1(msg_key[:16] + key256[x: x + 32]).digest()
    b = hashlib.sha1(key256[x + 32: x + 48] + msg_key[:16] + key256[x + 48: x + 64]).digest()
    c = hashlib.sha1(key256[x + 64: x + 96] + msg_key[:16]).digest()
    d = hashlib.sha1(msg_key[:16] + key256[x + 96: x + 128]).digest()
    aes_key = a[:8] + b[8:20] + c[4:16]
    aes_iv  = a[8:20] + b[:8] + c[16:20] + d[:8]
    return aes_key, aes_iv


def _decrypt_local(encrypted: bytes, key256: bytes) -> bytes:
    """Расшифровывает блок данных tdata."""
    aes_key, aes_iv = _prepare_aes(key256, encrypted[:16])
    decrypted = tgcrypto.ige256_decrypt(encrypted[16:], aes_key, aes_iv)
    if hashlib.sha1(decrypted).digest()[:16] != encrypted[:16]:
        raise ValueError("SHA mismatch — неверный ключ или повреждённый файл")
    data_len = struct.unpack_from("<I", decrypted)[0]
    return bytes(decrypted[4:data_len])


# ─────────────────────────── Разбор MTP авторизации ─────────────────────────

def _parse_mtp_auth(data: bytes):
    """
    Разбирает сериализованную MtpAuthorization и возвращает
    (user_id, dc_id, list_of_(dc, auth_key_bytes)).
    """
    pos = 0
    uid = struct.unpack_from(">i", data, pos)[0]; pos += 4
    dc  = struct.unpack_from(">i", data, pos)[0]; pos += 4

    # Wide IDs tag: оба поля == -1 → затем uint64 userId + int32 dcId
    if uid == -1 and dc == -1:
        uid = struct.unpack_from(">Q", data, pos)[0]; pos += 8
        dc  = struct.unpack_from(">i", data, pos)[0]; pos += 4

    key_count = struct.unpack_from(">i", data, pos)[0]; pos += 4
    keys = []
    for _ in range(key_count):
        key_dc = struct.unpack_from(">i", data, pos)[0]; pos += 4
        auth_key = data[pos: pos + 256]; pos += 256
        keys.append((key_dc, auth_key))

    return uid, dc, keys


# ─────────────────────────── Создание Telethon сессии ────────────────────────

def _make_telethon_session(session_path: str, dc_id: int, auth_key: bytes, user_id: int):
    """Создаёт .session файл в формате Telethon (SQLite), version=7."""
    full_path = f"{session_path}.session"
    if os.path.exists(full_path):
        os.remove(full_path)

    db = sqlite3.connect(full_path)
    # Создаём схему, соответствующую Telethon CURRENT_VERSION=7
    # чтобы _upgrade_database не пытался пересоздавать таблицы
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
            md5_digest blob,
            file_size integer,
            type integer,
            id integer,
            hash integer,
            primary key(md5_digest, file_size, type)
        );
        CREATE TABLE update_state (
            id integer primary key,
            pts integer,
            qts integer,
            date integer,
            seq integer
        );
        INSERT INTO version VALUES (7);
    """)

    ip = DC_IPS.get(dc_id, DC_IPS[2])
    db.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        (dc_id, ip, 443, auth_key, None),
    )
    db.commit()
    db.close()


# ─────────────────────────── Поиск tdata папок ──────────────────────────────

def _find_tdata_dirs(root: str):
    """
    Ищет все папки tdata (содержат key_datas).
    Возвращает list of (account_name, tdata_path).
    """
    found = []

    try:
        entries = list(os.scandir(root))
    except PermissionError:
        return found

    for entry in entries:
        if not entry.is_dir():
            continue

        # Папка сама по себе — tdata/
        if os.path.isfile(os.path.join(entry.path, "key_datas")):
            parent = os.path.basename(os.path.dirname(entry.path))
            name = parent if parent else entry.name
            found.append((name, entry.path))
            continue

        # Внутри — подпапка tdata/
        nested = os.path.join(entry.path, "tdata")
        if os.path.isdir(nested) and os.path.isfile(os.path.join(nested, "key_datas")):
            found.append((entry.name, nested))
            continue

        # Рекурсия
        found.extend(_find_tdata_dirs(entry.path))

    return found


# ─────────────────────────── Конвертация одного аккаунта ────────────────────

def _convert_tdata(account_name: str, tdata_path: str) -> bool:
    """Конвертирует tdata -> .session. Возвращает True при успехе."""
    try:
        # 1. Читаем local key
        raw = _parse_tdf_raw(os.path.join(tdata_path, "key_datas"))
        salt, pos = _qt_read_bytes(raw, 0)
        key_enc, _ = _qt_read_bytes(raw, pos)
        local_key = _decrypt_local(key_enc, _create_local_key(b"", salt))[:256]

        # 2. Ищем файл сессии (имя из 16 hex-символов + 's')
        session_file = None
        for fname in os.listdir(tdata_path):
            if len(fname) == 17 and fname.endswith("s") and fname[:-1].isalnum():
                session_file = os.path.join(tdata_path, fname)
                break

        if not session_file:
            print(f"  [{account_name}] Файл сессии не найден в {tdata_path}")
            return False

        # 3. Расшифровываем сессию
        raw2 = _parse_tdf_raw(session_file)
        enc_blob, _ = _qt_read_bytes(raw2, 0)
        sess_data = _decrypt_local(enc_blob, local_key)
        mtp_data, _ = _qt_read_bytes(sess_data, 4)

        # 4. Извлекаем auth_key, dc_id, user_id
        user_id, dc_id, keys = _parse_mtp_auth(mtp_data)

        if not keys:
            print(f"  [{account_name}] Нет ключей в MtpAuthorization")
            return False

        # Берём ключ для основного DC
        auth_key = next((k for dc, k in keys if dc == dc_id), keys[0][1])

        # 5. Создаём Telethon сессию
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        session_path = os.path.join(SESSIONS_DIR, account_name)
        _make_telethon_session(session_path, dc_id, auth_key, user_id)

        print(f"  [{account_name}] OK — user_id={user_id}, dc={dc_id} -> sessions/{account_name}.session")
        return True

    except Exception as e:
        print(f"  [{account_name}] Ошибка: {e}")
        return False


# ─────────────────────────── Публичная функция ──────────────────────────────

def import_tdata_interactive():
    """
    Интерактивный импорт tdata из zip-архива или папки.
    Поддерживает: zip с несколькими аккаунтами, zip с одним,
    папку tdata напрямую.
    """
    if not _TGCRYPTO_OK:
        print("Ошибка: tgcrypto не установлен. Выполни: pip install tgcrypto")
        return

    path = input("Путь к zip-архиву или папке tdata: ").strip().strip('"')
    if not path:
        print("Путь не указан.")
        return
    if not os.path.exists(path):
        print(f"Не найдено: {path}")
        return

    tmp_dir = None
    try:
        # Распаковываем zip во временную папку
        if zipfile.is_zipfile(path):
            print("Обнаружен zip-архив. Распаковываю...")
            tmp_dir = tempfile.mkdtemp(prefix="tdata_import_")
            with zipfile.ZipFile(path, "r") as zf:
                zf.extractall(tmp_dir)
            search_root = tmp_dir
        elif os.path.isdir(path):
            search_root = path
        else:
            print("Поддерживаются только .zip или папка.")
            return

        # Ищем tdata папки
        accounts = _find_tdata_dirs(search_root)

        # Возможно сам путь и есть tdata/
        if not accounts and os.path.isfile(os.path.join(search_root, "key_datas")):
            name = input("Имя для аккаунта (например номер телефона): ").strip() or "imported"
            accounts = [(name, search_root)]

        if not accounts:
            print("tdata папки не найдены (нет файла key_datas).")
            return

        print(f"\nНайдено аккаунтов: {len(accounts)}")
        for name, tpath in accounts:
            print(f"  {name}")

        if input("\nИмпортировать все? (y/n): ").strip().lower() != "y":
            print("Отменено.")
            return

        print()
        ok = sum(_convert_tdata(name, tpath) for name, tpath in accounts)
        print(f"\nИмпортировано: {ok}/{len(accounts)}")
        if ok:
            print("Проверить: меню -> Управление аккаунтами -> Проверить аккаунты")

    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
