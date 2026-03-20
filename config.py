import os

# --- ОСНОВНАЯ КОНФИГУРАЦИЯ ---

CONFIG = {
    "LZT_TOKEN": "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzUxMiJ9.eyJzdWIiOjUzODI2MTksImlzcyI6Imx6dCIsImlhdCI6MTc2NDI0MTYzOSwianRpIjoiODg3NjY2Iiwic2NvcGUiOiJiYXNpYyByZWFkIHBvc3QgY29udmVyc2F0ZSBwYXltZW50IGludm9pY2UgY2hhdGJveCBtYXJrZXQiLCJleHAiOjE5MjE5MjE2Mzl9.C1rARs5slMCMZ4nbc4qFqnJnNvVb0bWDaOGmqIXRrjiRslUNygLeWc7QBxTMs8mJ-EUH2szpo5JNmoyXsE7SeVtwyx5KW6h-BZPEGNVZCk36jRoBkwott0vaf8_RGJOizAVoMd7cJs_JSBgGv64sXQSFBZSgjpTLqwo3kjy0WDI",
    "LZT_MARKET_ID": 24,
    "LZT_API_BASE_URL": "https://prod-api.lzt.market",
    "TELEGRAM_API_ID": 31955472,
    "TELEGRAM_API_HASH": "636863cb9b1482ed12e0649d26fad94f",
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

DM_LIMIT_PER_ACCOUNT = 20
CHAT_LIMIT_PER_ACCOUNT = 10
INVITE_LIMIT_PER_ACCOUNT = 40

PARSE_DELAY_MIN = 2
PARSE_DELAY_MAX = 5

# Рассылка по чатам с вступлением
JOIN_DELAY_MIN = 30       # задержка между вступлениями в чаты (сек)
JOIN_DELAY_MAX = 90
JOIN_LIMIT_PER_ACCOUNT = 5  # сколько чатов вступить + написать за сессию на 1 аккаунт

# --- ПРОКСИ ---

PROXY_FILE = "proxy.txt"


def load_proxy():
    if not os.path.exists(PROXY_FILE):
        return None
    with open(PROXY_FILE, "r") as f:
        line = f.read().strip()
    return line or None


def save_proxy(proxy_string):
    with open(PROXY_FILE, "w") as f:
        f.write(proxy_string.strip())


def load_folder_links() -> list[str]:
    """Загружает ссылки на папки из data/folder_links.txt (по одной на строку)."""
    if not os.path.exists(FOLDER_LINKS_FILE):
        return []
    with open(FOLDER_LINKS_FILE, "r", encoding="utf-8") as f:
        lines = f.read().strip().splitlines()
    return [l.strip() for l in lines if l.strip() and "t.me/addlist/" in l]


def save_folder_links(links: list[str]):
    """Сохраняет ссылки на папки в data/folder_links.txt."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(FOLDER_LINKS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(links) + "\n")
