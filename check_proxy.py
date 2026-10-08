# -*- coding: utf-8 -*-
"""
Диагностика прокси: запусти с включённым и с выключенным VPN и сравни.

    python check_proxy.py

Показывает, откуда компьютер выходит в сеть напрямую, доходит ли он до самого
прокси, и работает ли соединение с Telegram ЧЕРЕЗ прокси. Аккаунты не трогает.
"""
import asyncio
import socket
import sys
import time
import urllib.request
from urllib.parse import urlparse

sys.path.insert(0, ".")


def tcp(host: str, port: int, timeout: float = 10) -> str:
    s = socket.socket()
    s.settimeout(timeout)
    start = time.time()
    try:
        s.connect((host, port))
        return f"доходит ({time.time() - start:.1f} с)"
    except Exception as e:  # noqa: BLE001
        return f"НЕ доходит — {type(e).__name__}: {str(e)[:70]}"
    finally:
        s.close()


def socks_hello(host: str, port: int, timeout: float = 10) -> str:
    """Отправляет прокси приветствие SOCKS5 и ждёт ответ: отвечает ли он вообще."""
    s = socket.socket()
    s.settimeout(timeout)
    start = time.time()
    try:
        s.connect((host, port))
        s.sendall(bytes([5, 1, 2]))          # SOCKS5, 1 метод: логин/пароль
        data = s.recv(2)
        took = time.time() - start
        if data == bytes([5, 2]):
            return f"прокси ответил за {took:.1f} с (просит логин/пароль) — SOCKS-трафик проходит"
        return f"ответ {data!r} за {took:.1f} с"
    except socket.timeout:
        return "прокси МОЛЧИТ после соединения — трафик SOCKS не проходит (режет провайдер/сеть)"
    except Exception as e:  # noqa: BLE001
        return f"обрыв: {type(e).__name__}: {str(e)[:60]}"
    finally:
        s.close()


def socks_steps(p, targets, timeout: float = 10) -> list[str]:
    """
    Пошаговый SOCKS5 вручную: приветствие -> логин/пароль -> CONNECT к каждой цели.
    Показывает, НА КАКОМ шаге застревает: на авторизации (политика прокси) или на
    CONNECT к конкретному адресу (например, к Telegram).
    """
    out = []
    s = socket.socket()
    s.settimeout(timeout)
    t0 = time.time()
    try:
        s.connect((p.hostname, p.port))
        s.sendall(bytes([5, 1, 2]))
        if s.recv(2) != bytes([5, 2]):
            return ["неожиданный ответ на приветствие"]
        user, pwd = (p.username or "").encode(), (p.password or "").encode()
        s.sendall(bytes([1, len(user)]) + user + bytes([len(pwd)]) + pwd)
        ans = s.recv(2)
        out.append(f"  авторизация: {'принята' if ans == bytes([1, 0]) else 'ОТКАЗ ' + repr(ans)} "
                   f"({time.time() - t0:.1f} с)")
        if ans != bytes([1, 0]):
            return out
    except socket.timeout:
        return [f"  застряли на авторизации — прокси молчит после приветствия ({timeout:.0f} с)"]
    except Exception as e:  # noqa: BLE001
        return [f"  обрыв до авторизации: {type(e).__name__}: {str(e)[:60]}"]
    finally:
        s.close()
    for name, host, port in targets:
        s = socket.socket()
        s.settimeout(timeout)
        t1 = time.time()
        try:
            s.connect((p.hostname, p.port))
            s.sendall(bytes([5, 1, 2]))
            s.recv(2)
            s.sendall(bytes([1, len(user)]) + user + bytes([len(pwd)]) + pwd)
            s.recv(2)
            s.sendall(bytes([5, 1, 0, 1]) + socket.inet_aton(host) + port.to_bytes(2, "big"))
            rep = s.recv(10)
            code = rep[1] if len(rep) > 1 else None
            out.append(f"  CONNECT к {name}: {'открыт' if code == 0 else 'отказ, код ' + str(code)} "
                       f"({time.time() - t1:.1f} с)")
        except socket.timeout:
            out.append(f"  CONNECT к {name}: ЗАСТРЯЛ — нет ответа за {timeout:.0f} с")
        except Exception as e:  # noqa: BLE001
            out.append(f"  CONNECT к {name}: обрыв {type(e).__name__}: {str(e)[:50]}")
        finally:
            s.close()
    return out


def main() -> None:
    from config import load_proxy
    import proxy_watch as pw

    raw = load_proxy()
    if not raw:
        print("Прокси не задан (Telegram → 6).")
        return
    p = urlparse(raw)
    print(f"Прокси: {p.hostname}:{p.port}\n")

    try:
        ip = urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode()
    except Exception as e:  # noqa: BLE001
        ip = f"не удалось узнать ({type(e).__name__})"
    print(f"1. Внешний IP этого компьютера (напрямую): {ip}", flush=True)
    print("   (каждый следующий пункт может занять до 15 секунд — дождись конца)", flush=True)
    print(f"2. Путь от компьютера до самого прокси:    {tcp(p.hostname, p.port)}", flush=True)
    print(f"2б. Ответ прокси на SOCKS-приветствие:      {socks_hello(p.hostname, p.port)}", flush=True)
    print("2в. Пошагово через прокси (авторизация, затем CONNECT):", flush=True)
    for line in socks_steps(p, [("обычный сайт (1.1.1.1:80)", "1.1.1.1", 80),
                                ("Telegram DC2 (149.154.167.51:443)", "149.154.167.51", 443)]):
        print(line, flush=True)
    print(f"3. Прямой доступ к серверам Telegram:       {tcp('149.154.167.51', 443)}  "
          f"(для программы не нужен — она ходит только через прокси)", flush=True)
    ok, kind, text = asyncio.run(pw.probe(raw))
    print(f"4. Telegram через прокси:                   {'работает' if ok else 'НЕ работает — ' + text}")
    print("КОНЕЦ ПРОВЕРКИ")
    print("\nЕсли пункт 2 «НЕ доходит» без VPN — провайдер не пускает компьютер до прокси, "
          "дело в маршруте до прокси, а не в аккаунтах.")


if __name__ == "__main__":
    main()
