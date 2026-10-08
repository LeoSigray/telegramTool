# -*- coding: utf-8 -*-
"""
Блокировка «один аккаунт = один процесс».

Файл сессии Telegram (sqlite) нельзя открывать из двух программ сразу: получаются
«database is locked», обрывы подключений и сброс сессии. Поэтому тот, кто подключает аккаунт
надолго (пул клиентов: прогрев, рассылка, бэкенд мессенджера), берёт файловую блокировку
sessions/<имя>.lock. Если она уже занята другим процессом — аккаунт пропускается с понятным
сообщением. ОС сама снимает блокировку, когда процесс завершился (в том числе аварийно).
"""
import os

from config import SESSIONS_DIR

_held: dict[str, int] = {}          # имя -> открытый дескриптор (держим, пока работаем с аккаунтом)


def _lock_fd(fd: int) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 1024, os.SEEK_SET)   # блокируем байт за областью с pid, чтобы pid можно было прочитать
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_fd(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 1024, os.SEEK_SET)   # блокируем байт за областью с pid, чтобы pid можно было прочитать
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def acquire(name: str, sessions_dir: str | None = None) -> bool:
    """True — аккаунт наш (или уже был нашим в этом процессе); False — им пользуется другой процесс."""
    if name in _held:
        return True
    d = sessions_dir or SESSIONS_DIR
    os.makedirs(d, exist_ok=True)
    fd = os.open(os.path.join(d, f"{name}.lock"), os.O_CREAT | os.O_RDWR)
    if not _lock_fd(fd):
        os.close(fd)
        return False
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid():>12}".encode())  # для диагностики: кто держит (фиксированная ширина)
    except OSError:
        pass
    _held[name] = fd
    return True


def release(name: str) -> None:
    fd = _held.pop(name, None)
    if fd is not None:
        _unlock_fd(fd)
        try:
            os.close(fd)
        except OSError:
            pass


def release_all() -> None:
    for name in list(_held):
        release(name)


def describe_holder(pid: int | None) -> str:
    """Кто это: «мессенджер (Диалоги)», «консольная программа (прогрев/рассылка)» (по командной строке, Linux)."""
    if not pid:
        return ""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(bytes([0]), b" ").decode("utf-8", "ignore")
    except OSError:
        return ""
    if "uvicorn" in cmd or "api.server" in cmd:
        return "мессенджер (Диалоги)"
    if "main.py" in cmd:
        return "консольная программа (прогрев/рассылка)"
    return ""


def holder_pid(name: str, sessions_dir: str | None = None) -> int | None:
    """PID процесса, который записал себя в .lock (если файл читается)."""
    try:
        with open(os.path.join(sessions_dir or SESSIONS_DIR, f"{name}.lock"), "r") as f:
            return int(f.read().strip() or 0) or None
    except (OSError, ValueError):
        return None
