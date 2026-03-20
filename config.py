import os
import json

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
PARSED_EXCEL_FILE = os.path.join(DATA_DIR, "parsed_chats.xlsx")

# --- ЛИМИТЫ TELEGRAM (анти-бан) ---

# Задержки между действиями (секунды, рандом между min и max)
DM_DELAY_MIN = 30
DM_DELAY_MAX = 60

CHAT_DELAY_MIN = 60
CHAT_DELAY_MAX = 120

INVITE_DELAY_MIN = 5
INVITE_DELAY_MAX = 15

# Лимиты на аккаунт в день
DM_LIMIT_PER_ACCOUNT = 20
CHAT_LIMIT_PER_ACCOUNT = 10
INVITE_LIMIT_PER_ACCOUNT = 40

# Задержки для парсинга папок (секунды, между запросами к API)
PARSE_DELAY_MIN = 2
PARSE_DELAY_MAX = 5

# --- ПРОКСИ ---

PROXY_FILE = "proxy.txt"


def load_proxy():
    """Загружает прокси из файла proxy.txt (формат: socks5://user:pass@host:port)"""
    if not os.path.exists(PROXY_FILE):
        return None
    with open(PROXY_FILE, "r") as f:
        line = f.read().strip()
    if not line:
        return None
    return line


def save_proxy(proxy_string):
    """Сохраняет прокси в файл proxy.txt"""
    with open(PROXY_FILE, "w") as f:
        f.write(proxy_string.strip())
