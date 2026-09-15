import socks
from urllib.parse import urlparse, parse_qs

from config import load_proxy
from data.db import get_account_proxy


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


def _to_telethon_tuple(proxy_string: str | None):
    """socks5://user:pass@host:port → tuple для Telethon, или None."""
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


def parse_mtproxy_link(url: str) -> tuple[str, int, str] | None:
    """
    tg://proxy?server=host&port=443&secret=HEX  (или https://t.me/proxy?...)
    → (host, port, secret_hex) или None, если это не MTProxy-ссылка.

    ⚠ Секреты с префиксом 'ee' — это FakeTLS (маскировка под HTTPS к домену,
    зашитому в хвосте секрета). Telethon (network/connection/tcpmtproxy.py,
    normalize_secret()) обрезает 'ee' и домен и берёт только первые 16 байт —
    сам TLS-хэндшейк не эмулирует. Проверено на практике (см. чат/README):
    официальный протокол это не то же самое, что "просто прокси" — сервер
    ждёт настоящий TLS ClientHello с нужным SNI, поэтому такие секреты часто
    не соединяются через Telethon, даже если формально распознаются.
    """
    if not (url.startswith("tg://proxy") or url.startswith("tg://socks")
            or "t.me/proxy" in url):
        return None
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    server = (qs.get("server") or [None])[0]
    port = (qs.get("port") or [None])[0]
    secret = (qs.get("secret") or [None])[0]
    if not (server and port and secret):
        return None
    try:
        port = int(port)
    except ValueError:
        return None
    return server, port, secret


def client_kwargs_for(account: str) -> dict:
    """
    kwargs для TelegramClient(...) конкретного аккаунта: персональный прокси
    (SOCKS5 или MTProxy), иначе общий (load_proxy()), иначе {} (без прокси).

    Один IP на все аккаунты сразу — сильный сигнал для антиспама Telegram,
    что это ферма, а не разные люди. Персональный прокси на аккаунт — способ
    этого избежать.
    """
    raw = get_account_proxy(account) or load_proxy()
    if not raw:
        return {}

    mt = parse_mtproxy_link(raw)
    if mt:
        server, port, secret = mt
        from telethon.network.connection import ConnectionTcpMTProxyRandomizedIntermediate
        return {
            "connection": ConnectionTcpMTProxyRandomizedIntermediate,
            "proxy": (server, port, secret),
        }

    tup = _to_telethon_tuple(raw)
    return {"proxy": tup} if tup else {}


def get_telethon_proxy():
    """Общий SOCKS5-прокси (один на все аккаунты без персонального) — tuple или None."""
    return _to_telethon_tuple(load_proxy())


def get_telethon_proxy_for(account: str):
    """
    Обратная совместимость: только proxy-tuple (без connection=), только
    SOCKS5-часть персонального/общего прокси аккаунта. Для полного набора
    kwargs (в т.ч. connection= для MTProxy) используй client_kwargs_for().
    """
    raw = get_account_proxy(account) or load_proxy()
    if raw and parse_mtproxy_link(raw):
        return None  # MTProxy — не SOCKS5-tuple, зовите client_kwargs_for()
    return _to_telethon_tuple(raw)
