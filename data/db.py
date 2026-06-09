"""
data/db.py — централизованное SQLite-хранилище.

Содержит:
  • sessions  — бинарные .session файлы (BLOB)
  • proxy     — SOCKS5 прокси строка
  • folder_links — ссылки на Telegram-папки

Использование:
  from data.db import init_db, load_sessions_to_disk, save_session_from_file, ...

При первом запуске вызови migrate_from_files() чтобы перенести
существующие sessions/, proxy.txt, folder_links.txt → БД.
"""

import glob
import os
import sqlite3

# --- пути ---
_HERE = os.path.dirname(__file__)
DB_PATH = os.path.join(_HERE, "database.db")


# ──────────────────────────────────────────────────────────────────────────
#  Соединение
# ──────────────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    os.makedirs(_HERE, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")   # безопаснее при конкурентном доступе
    return c


# ──────────────────────────────────────────────────────────────────────────
#  Инициализация
# ──────────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Создаёт таблицы если не существуют. Безопасно вызывать повторно."""
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                name         TEXT PRIMARY KEY,
                session_data BLOB NOT NULL,
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS proxy (
                id    INTEGER PRIMARY KEY CHECK (id = 1),
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS folder_links (
                id  INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE
            );
        """)


# ──────────────────────────────────────────────────────────────────────────
#  Sessions
# ──────────────────────────────────────────────────────────────────────────

def save_session_from_file(path: str) -> bool:
    """
    Читает .session файл с диска и сохраняет/обновляет в БД.
    Возвращает True при успехе.
    """
    try:
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, "rb") as f:
            data = f.read()
        with _conn() as c:
            c.execute(
                """
                INSERT INTO sessions (name, session_data, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                    session_data = excluded.session_data,
                    updated_at   = datetime('now')
                """,
                (name, data),
            )
        return True
    except Exception as e:
        print(f"[db] Ошибка сохранения сессии {path}: {e}")
        return False


def load_sessions_to_disk(sessions_dir: str) -> list[str]:
    """
    Выгружает все сессии из БД в папку sessions_dir.
    Возвращает список путей к .session файлам.
    """
    os.makedirs(sessions_dir, exist_ok=True)
    paths: list[str] = []
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT name, session_data FROM sessions"
            ).fetchall()
        for row in rows:
            path = os.path.join(sessions_dir, f"{row['name']}.session")
            with open(path, "wb") as f:
                f.write(row["session_data"])
            paths.append(path)
    except Exception as e:
        print(f"[db] Ошибка загрузки сессий: {e}")
    return paths


def sync_session_to_db(name: str, sessions_dir: str) -> bool:
    """
    Читает обновлённый .session файл с диска и синхронизирует обратно в БД.
    Вызывать после отключения Telethon-клиента чтобы сохранить кэш сущностей.
    """
    path = os.path.join(sessions_dir, f"{name}.session")
    if not os.path.exists(path):
        return False
    return save_session_from_file(path)


def sync_all_to_db(sessions_dir: str) -> int:
    """
    Синхронизирует все .session файлы из sessions_dir обратно в БД.
    Вызывается при завершении работы. Возвращает количество обновлённых.
    """
    pattern = os.path.join(sessions_dir, "*.session")
    count = 0
    for path in glob.glob(pattern):
        if save_session_from_file(path):
            count += 1
    return count


def delete_session(name: str, sessions_dir: str) -> None:
    """Удаляет сессию из БД и с диска."""
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE name = ?", (name,))
    path = os.path.join(sessions_dir, f"{name}.session")
    if os.path.exists(path):
        os.remove(path)


def list_session_names() -> list[str]:
    """Возвращает имена всех сессий из БД."""
    with _conn() as c:
        rows = c.execute(
            "SELECT name FROM sessions ORDER BY name"
        ).fetchall()
    return [row["name"] for row in rows]


def session_count() -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) FROM sessions").fetchone()
    return row[0] if row else 0


# ──────────────────────────────────────────────────────────────────────────
#  Proxy
# ──────────────────────────────────────────────────────────────────────────

def get_proxy() -> str | None:
    """Возвращает прокси строку или None."""
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT value FROM proxy WHERE id = 1"
            ).fetchone()
        return row["value"] if row else None
    except Exception:
        return None


def set_proxy(value: str) -> None:
    """Сохраняет прокси строку в БД."""
    with _conn() as c:
        c.execute(
            """
            INSERT INTO proxy (id, value) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET value = excluded.value
            """,
            (value,),
        )


def clear_proxy() -> None:
    """Удаляет прокси из БД."""
    with _conn() as c:
        c.execute("DELETE FROM proxy WHERE id = 1")


# ──────────────────────────────────────────────────────────────────────────
#  Folder links
# ──────────────────────────────────────────────────────────────────────────

def get_folder_links() -> list[str]:
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT url FROM folder_links ORDER BY id"
            ).fetchall()
        return [row["url"] for row in rows]
    except Exception:
        return []


def save_folder_links(links: list[str]) -> None:
    with _conn() as c:
        c.execute("DELETE FROM folder_links")
        c.executemany(
            "INSERT OR IGNORE INTO folder_links (url) VALUES (?)",
            [(url,) for url in links],
        )


# ──────────────────────────────────────────────────────────────────────────
#  Миграция из файлов
# ──────────────────────────────────────────────────────────────────────────

def migrate_from_files(sessions_dir: str, data_dir: str) -> None:
    """
    Одноразовая миграция: переносит данные из файловой системы в БД.
    Вызывается при первом запуске (если БД пуста).
    Безопасно вызывать повторно — не перезаписывает уже существующие записи.
    """
    # Сессии
    pattern = os.path.join(sessions_dir, "*.session")
    session_files = glob.glob(pattern)
    sess_count = 0
    if session_files:
        existing = set(list_session_names())
        for path in session_files:
            name = os.path.splitext(os.path.basename(path))[0]
            if name not in existing:
                if save_session_from_file(path):
                    sess_count += 1
        if sess_count:
            print(f"[db] Мигрировано сессий: {sess_count}")

    # Прокси
    proxy_file = "proxy.txt"
    if os.path.exists(proxy_file) and get_proxy() is None:
        try:
            with open(proxy_file) as f:
                val = f.read().strip()
            if val:
                set_proxy(val)
                print(f"[db] Мигрирован прокси: {val}")
        except Exception as e:
            print(f"[db] Ошибка миграции прокси: {e}")

    # Folder links
    fl_file = os.path.join(data_dir, "folder_links.txt")
    if os.path.exists(fl_file) and not get_folder_links():
        try:
            with open(fl_file) as f:
                lines = [l.strip() for l in f.read().splitlines() if l.strip()]
            if lines:
                save_folder_links(lines)
                print(f"[db] Мигрировано folder_links: {len(lines)}")
        except Exception as e:
            print(f"[db] Ошибка миграции folder_links: {e}")
