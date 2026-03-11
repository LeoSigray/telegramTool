import socks
from urllib.parse import urlparse

from config import load_proxy


def parse_proxy(proxy_string):
    """
    Парсит SOCKS5 прокси строку в dict для Telethon.
    Формат: socks5://user:pass@host:port или socks5://host:port
    """
    if not proxy_string:
        return None

    parsed = urlparse(proxy_string)

    if parsed.scheme not in ("socks5", "socks5h"):
        raise ValueError(f"Поддерживается только SOCKS5, получено: {parsed.scheme}")

    proxy_dict = {
        "proxy_type": python_socks_type(),
        "addr": parsed.hostname,
        "port": parsed.port or 1080,
    }

    if parsed.username:
        proxy_dict["username"] = parsed.username
    if parsed.password:
        proxy_dict["password"] = parsed.password

    return proxy_dict


def python_socks_type():
    """Возвращает тип прокси для python-socks (используется Telethon)."""
    return socks.SOCKS5


def get_telethon_proxy():
    """Возвращает прокси tuple для Telethon или None."""
    proxy_string = load_proxy()
    if not proxy_string:
        return None

    parsed = urlparse(proxy_string)
    # Telethon принимает tuple: (type, addr, port, rdns, username, password)
    return (
        socks.SOCKS5,
        parsed.hostname,
        parsed.port or 1080,
        True,  # rdns
        parsed.username,
        parsed.password,
    )
