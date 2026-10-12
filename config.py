import os

# .env подхватываем здесь, чтобы CONFIG был заполнен независимо от точки входа
# (main.py, uvicorn api.server:app, отдельные скрипты).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- ОСНОВНАЯ КОНФИГУРАЦИЯ ---
# Секреты берём из .env (см. .env.example). В репозитории их быть не должно.

CONFIG = {
    "LZT_TOKEN": os.getenv("LZT_TOKEN", ""),
    "LZT_MARKET_ID": int(os.getenv("LZT_MARKET_ID", "24")),
    "LZT_API_BASE_URL": os.getenv("LZT_API_BASE_URL", "https://prod-api.lzt.market"),
    "TELEGRAM_API_ID": int(os.getenv("TELEGRAM_API_ID", "0")),
    "TELEGRAM_API_HASH": os.getenv("TELEGRAM_API_HASH", ""),
    "DARKSTORE_API_KEY": os.getenv("DARKSTORE_API_KEY", ""),
}

# --- ПУТИ ---

ACCOUNTS_DIR = "accounts"
SESSIONS_DIR = "sessions"
TDATA_DIR = "tdata_accounts"
DATA_DIR = "data"
EXCEL_FILE = os.path.join(DATA_DIR, "targets.xlsx")
FOLDER_LINKS_FILE = os.path.join(DATA_DIR, "folder_links.txt")

# --- ЛИМИТЫ TELEGRAM (анти-бан) ---

DM_DELAY_MIN = 30
DM_DELAY_MAX = 60

CHAT_DELAY_MIN = 60
CHAT_DELAY_MAX = 120

INVITE_DELAY_MIN = 5
INVITE_DELAY_MAX = 15

DM_LIMIT_PER_ACCOUNT = 20          # максимум за ОДИН запуск рассылки с аккаунта

# Суточный лимит ЛС на аккаунт (скользящие 24 часа, считается по базе — переживает перезапуски)
DM_DAILY_LIMIT_PER_ACCOUNT = 20

# Окно активности: рассылка идёт только днём по локальному времени аккаунта.
# Часовой пояс берётся из прогрева (если аккаунт на нём был), иначе DM_DEFAULT_TZ_H (США, восточное).
DM_WINDOW_ENABLED = False          # окно 10–22 по времени аккаунта; выключено (True — включить)
DM_WINDOW_H = (10, 22)
DM_DEFAULT_TZ_H = -5.0

# «Человеческая» отправка: пауза -> «печатает…» (длина текста / скорость) -> отправка
DM_TYPING = True
DM_PRE_TYPING_S = (1.0, 3.0)       # пауза перед началом набора
DM_TYPING_CPS = (7.0, 12.0)        # скорость набора, символов в секунду
DM_TYPING_MIN_S, DM_TYPING_MAX_S = 2.0, 18.0

# Лёгкая вариация текста: каждое сообщение с аккаунта чуть отличается (см. data/message_variants.py)
DM_VARY_TEXT = True

# Проверка доставки в тесте «один получатель»: после отправки сообщение перечитывается через эти паузы (сек)
# и ещё раз в конце через DM_VERIFY_FINAL_S — ловит случай, когда Telegram принял и тихо удалил сообщение.
DM_VERIFY_WAITS_S = (4, 25)
DM_VERIFY_FINAL_S = 60
CHAT_LIMIT_PER_ACCOUNT = 10
INVITE_LIMIT_PER_ACCOUNT = 40

PARSE_DELAY_MIN = 2
PARSE_DELAY_MAX = 5

# Нейрокомментинг
COMMENT_DELAY_MIN = 45       # между комментариями одного аккаунта (сек)
COMMENT_DELAY_MAX = 120
COMMENT_LIMIT_PER_ACCOUNT = 15   # максимум комментариев на аккаунт за один раунд
COMMENT_ROUND_DELAY = 1800       # пауза между раундами в continuous-режиме (30 мин)

# Рассылка по чатам с вступлением
JOIN_DELAY_MIN = 30       # задержка между вступлениями в чаты (сек)
JOIN_DELAY_MAX = 90
JOIN_LIMIT_PER_ACCOUNT = 5  # сколько чатов вступить + написать за сессию на 1 аккаунт

# --- ПРОКСИ ---
# Прокси и folder_links теперь хранятся в data/database.db (data/db.py).
# Функции ниже являются фасадом для обратной совместимости.


def load_proxy() -> str | None:
    from data.db import get_proxy
    return get_proxy()


def save_proxy(proxy_string: str) -> None:
    from data.db import set_proxy
    set_proxy(proxy_string.strip())


def load_folder_links() -> list[str]:
    """Загружает ссылки на папки из БД."""
    from data.db import get_folder_links
    links = get_folder_links()
    return [l for l in links if "t.me/addlist/" in l]


def save_folder_links(links: list[str]) -> None:
    """Сохраняет ссылки на папки в БД."""
    from data.db import save_folder_links as _save
    _save(links)
