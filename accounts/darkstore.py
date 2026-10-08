# -*- coding: utf-8 -*-
"""
DarkStore (dark.shopping) — клиент API и разбор выданного товара.

API (https://dark.shopping/developer): базовый адрес /api/v1/, ключ в параметре `key`,
лимит 2 запроса в секунду (при превышении — HTTP 429). Ответ: {success, data}.
Ключ выдаётся после заявки на доступ к API; хранится в .env как DARKSTORE_API_KEY.
"""
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile

import requests

BASE_URL = "https://dark.shopping/api/v1/"
MIN_INTERVAL_S = 0.55            # лимит 2 запроса/с — берём с запасом
PURCHASES_DIR = os.path.join("data", "purchases", "darkstore")


class DarkstoreError(Exception):
    pass


class DarkstoreAPI:
    def __init__(self, key: str, *, session=None, sleep=time.sleep, clock=time.monotonic):
        if not key:
            raise DarkstoreError("DARKSTORE_API_KEY не задан")
        self.key = key
        self._http = session or requests
        self._sleep, self._clock = sleep, clock
        self._last = 0.0

    # ── низкий уровень ──────────────────────────────────────────────────────
    def _throttle(self) -> None:
        wait = MIN_INTERVAL_S - (self._clock() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()

    def call(self, endpoint: str, params: dict | None = None, *, post: bool = False):
        """Вызов метода. Возвращает поле data. Повторяет при 429 и сетевых сбоях."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params["key"] = self.key
        net_fail = 0
        for attempt in range(6):
            self._throttle()
            try:
                if post:
                    r = self._http.post(BASE_URL + endpoint, data=params, timeout=40)
                else:
                    r = self._http.get(BASE_URL + endpoint, params=params, timeout=40)
            except requests.RequestException as e:
                net_fail += 1
                if net_fail >= 3:
                    raise DarkstoreError(f"DarkStore недоступен: {type(e).__name__}") from e
                self._sleep(2 * net_fail)
                continue
            if r.status_code == 429:
                self._sleep(5 * (attempt + 1))        # IP-блокировка снимается через несколько минут
                continue
            try:
                body = r.json()
            except ValueError:
                raise DarkstoreError(f"{endpoint}: ответ не JSON (HTTP {r.status_code})")
            if isinstance(body, dict) and body.get("success") is False:
                data = body.get("data")
                msg = (data.get("message") if isinstance(data, dict) else None) or str(data)
                raise DarkstoreError(f"{endpoint}: {msg}")
            if r.status_code >= 400 and not (isinstance(body, dict) and "data" in body):
                msg = body.get("message") if isinstance(body, dict) else None
                raise DarkstoreError(f"{endpoint}: HTTP {r.status_code} {msg or ''}".strip())
            return body.get("data") if isinstance(body, dict) and "data" in body else body
        raise DarkstoreError(f"{endpoint}: превышен лимит запросов (429), подожди несколько минут")

    # ── каталог ─────────────────────────────────────────────────────────────
    def categories(self) -> list[dict]:
        return _as_list(self.call("category/list"))

    def groups(self, category_id: int) -> list[dict]:
        return _as_list(self.call("group/list", {"category_id": category_id}))

    def products(self, *, category_id=None, group_id=None, name=None, price_from=None,
                 price_to=None, only_in_stock=True, per_page=100, max_pages=5) -> list[dict]:
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            data = self.call("product/list", {
                "category_id": category_id, "group_id": group_id, "name": name,
                "price_from": price_from, "price_to": price_to,
                "only_in_stock": 1 if only_in_stock else None,
                "page": page, "per-page": per_page})
            items = _as_list(data.get("items") if isinstance(data, dict) else data)
            out += items
            meta = data.get("_meta") if isinstance(data, dict) else None
            pages = int((meta or {}).get("pageCount") or 1)
            if page >= pages or not items:
                break
        return out

    def product(self, product_id) -> dict:
        return self.call("product/view", {"id": product_id}) or {}

    def balance(self) -> tuple[float, str]:
        d = self.call("user/balance") or {}
        return float(d.get("balance") or 0), str(d.get("currency") or "")

    # ── покупка ─────────────────────────────────────────────────────────────
    def create_order(self, product_id, quantity: int = 1, idempotence_id: str | None = None):
        """
        idempotence_id защищает от двойного списания при повторе запроса:
        тот же id вернёт тот же заказ. По умолчанию генерируется новый.
        """
        return self.call("order/create", {
            "product": product_id, "quantity": quantity,
            "idempotence_id": idempotence_id or uuid.uuid4().hex}, post=True) or {}

    def order_status(self, order_id) -> str:
        d = self.call("order/status", {"id": order_id}) or {}
        return str(d.get("status") or "")

    def order_link(self, order_id) -> str | None:
        d = self.call("order/download", {"id": order_id}) or {}
        return d.get("link")

    def wait_completed(self, order_id, timeout_s: int = 180, poll_s: float = 3.0) -> str:
        """Ждёт, пока заказ выполнится. Возвращает финальный статус."""
        waited = 0.0
        while True:
            st = self.order_status(order_id)
            if st in ("completed", "canceled", "error", "refund"):
                return st
            if waited >= timeout_s:
                return st or "unknown"
            self._sleep(poll_s)
            waited += poll_s

    def download(self, link: str, order_id) -> str:
        """Скачивает выданный файл в data/purchases/darkstore/. Возвращает путь."""
        r = None
        for params in (None, {"key": self.key}):          # ссылка может быть публичной или требовать ключ
            self._throttle()
            r = self._http.get(link, params=params, timeout=90)
            if r.status_code == 200:
                break
        if r is None or r.status_code != 200:
            raise DarkstoreError(f"не удалось скачать файл заказа (HTTP {getattr(r, 'status_code', '?')})")
        os.makedirs(PURCHASES_DIR, exist_ok=True)
        cd = r.headers.get("Content-Disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
        ext = os.path.splitext(m.group(1))[1] if m else ""
        if not ext:
            head = r.content[:4]
            ext = ".zip" if head[:2] == b"PK" else ".txt"
        path = os.path.join(PURCHASES_DIR, f"order_{order_id}{ext}")
        with open(path, "wb") as f:
            f.write(r.content)
        return path


def _as_list(data) -> list[dict]:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", "data", "list"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


# ── что показывать и что скрывать ───────────────────────────────────────────

# По умолчанию скрываем «ретрив» (возвращённые/восстановленные — часто угнанные) и спамблок
RISKY_RE = re.compile(r"ретрив|retriv|спам[\s-]?блок|spam[\s-]?block|\bбан\b|banned|заблокирован|угнан|взлом", re.I)
# «БЕЗ СПАМБЛОКА» — достоинство товара, а не риск: такие фразы вырезаем до проверки
NEGATED_RE = re.compile(
    r"\b(без|нет|no|not)\s+(спа[с]?м[\s-]?блок\w*|spam[\s-]?block\w*|бана?|ban(ned)?|заблокирован\w*)", re.I)


def is_risky(product: dict) -> bool:
    # Только название и ЗНАЧЕНИЯ атрибутов (например, «Происхождение: Ретрив»). Описание не берём:
    # в нём продавцы пишут оговорки про спамблок/баны, и почти всё хорошее оказалось бы «рискованным».
    text = str(product.get("name") or "")
    for a in product.get("attributes") or []:
        text += " " + str(a.get("value") if isinstance(a, dict) else a)
    return bool(RISKY_RE.search(NEGATED_RE.sub(" ", text)))


def pick_by_name(items: list[dict], hints: tuple[str, ...]) -> list[dict]:
    """Категории/группы, в названии которых есть одна из подсказок (без учёта регистра)."""
    return [i for i in items if any(h in str(i.get("name", "")).lower() for h in hints)]


# ── разбор выданного товара ─────────────────────────────────────────────────

def _read_text(path: str) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            with open(path, encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, OSError):
            continue
    return ""


def parse_credentials(text: str) -> list[dict]:
    """Строки login:password[:id[:...]] -> [{login, password, extra}]. Пустые/мусорные пропускаются."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        out.append({"login": parts[0].strip(), "password": parts[1].strip(),
                    "extra": ":".join(parts[2:]).strip() or None})
    return out


def sniff(path: str) -> str:
    """Тип файла по первым байтам: zip / rar / 7z / gzip / html / text / unknown."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return "unknown"
    if head[:2] == b"PK":
        return "zip"
    if head[:4] == b"Rar!":
        return "rar"
    if head[:4] == bytes([0x37, 0x7A, 0xBC, 0xAF]):
        return "7z"
    if head[:2] == bytes([0x1F, 0x8B]):
        return "gzip"
    low = head.lower()
    if low.startswith(b"<!doc") or low.startswith(b"<html"):
        return "html"
    if head and all(32 <= b < 127 or b in (9, 10, 13) for b in head):
        return "text"
    return "unknown"


def _find_bsdtar() -> str | None:
    """bsdtar (libarchive) умеет RAR/RAR5/7z и бесплатный: Linux — пакет libarchive-tools, Windows — tar.exe."""
    import subprocess
    cands = [shutil.which("bsdtar"),
             os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "System32", "tar.exe"),
             shutil.which("tar")]
    for exe in cands:
        if exe and os.path.exists(exe):
            try:
                out = subprocess.run([exe, "--version"], capture_output=True, timeout=10, text=True).stdout.lower()
            except (subprocess.SubprocessError, OSError):
                continue
            if "bsdtar" in out or "libarchive" in out:
                return exe
    return None


def _extract_external(path: str, dest: str) -> bool:
    """
    RAR/7z распаковываем внешней утилитой: сначала bsdtar, затем 7z/7za/7zz.
    ВАЖНО: 7zip из Ubuntu собран без RAR — он создаёт битые файлы; поэтому результат проверяем
    (код возврата и непустые файлы), а при неудаче чистим папку, чтобы битое не приняли за сессию.
    """
    import subprocess

    def wipe() -> None:
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)

    cmds = []
    bsd = _find_bsdtar()
    if bsd:
        cmds.append([bsd, "-xf", path, "-C", dest])
    sevenz = next((shutil.which(x) for x in ("7z", "7za", "7zz") if shutil.which(x)), None)
    if sevenz:
        cmds.append([sevenz, "x", "-y", f"-o{dest}", path])
    for cmd in cmds:
        wipe()
        try:
            subprocess.run(cmd, capture_output=True, timeout=90, check=True)
        except (subprocess.SubprocessError, OSError):
            continue
        sizes = [os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(dest) for f in fs]
        if sizes and all(s > 0 for s in sizes):
            if sum(sizes) > MAX_UNPACK_MB * 1024 * 1024:
                wipe()
                raise DarkstoreError("архив слишком большой после распаковки — пропущен")
            return True
    wipe()
    return False


def unpack(path: str) -> str:
    """Распаковывает ZIP (а RAR/7z — через утилиту 7z, если есть) во временную папку; иначе кладёт туда копию файла."""
    tmp = tempfile.mkdtemp(prefix="darkstore_")
    kind = sniff(path)
    if kind == "zip" and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_UNPACK_MB * 1024 * 1024:
                raise DarkstoreError('архив слишком большой после распаковки — пропущен')
            z.extractall(tmp)           # zipfile сам не выпускает пути за пределы папки
    elif kind in ("rar", "7z", "gzip") and _extract_external(path, tmp):
        pass
    else:
        shutil.copy(path, tmp)
    return tmp


def describe_tree(root: str, limit: int = 14) -> str:
    """Короткое описание распакованного: сколько файлов и первые имена (только имена, без содержимого)."""
    names = []
    for d, _, fs in os.walk(root):
        for f in fs:
            names.append(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/"))
    names.sort()
    shown = ", ".join(names[:limit]) + (" …" if len(names) > limit else "")
    return f"файлов: {len(names)}" + (f" — {shown}" if names else "")


def collect_files(root: str) -> dict[str, list[str]]:
    """Сканирует распакованное: {'session': [...], 'tdata': [...], 'txt': [...]}."""
    found = {"session": [], "tdata": [], "txt": []}
    for dirpath, dirnames, filenames in os.walk(root):
        # папка tdata: либо называется tdata, либо в ней лежит key_datas (бывает, что имя другое)
        if os.path.basename(dirpath).lower() == "tdata" or "key_datas" in filenames:
            found["tdata"].append(dirpath)
            dirnames[:] = []
            continue
        for f in filenames:
            low = f.lower()
            if low.endswith(".session"):
                found["session"].append(os.path.join(dirpath, f))
            elif low.endswith((".txt", ".csv")):
                found["txt"].append(os.path.join(dirpath, f))
    return found


# ── атрибуты товара (API отдаёт их списком attributes) ──────────────────────

ATTR_ORIGIN, ATTR_2FA, ATTR_COUNTRY, ATTR_FORMAT, ATTR_DAYS = 4, 7, 12, 15, 16
_FORMAT_SHORT = {"tdata": "TD", "session": "S", "session+json": "SJ", ".od": "OD", "вход по коду": "код",
                 "zpprofile": "ZP"}


def attr_values(product: dict, attr_id: int) -> list[str]:
    return [str(a.get("value")) for a in (product.get("attributes") or [])
            if isinstance(a, dict) and a.get("attribute_id") == attr_id and a.get("value") is not None]


def product_country(product: dict) -> str:
    return ", ".join(attr_values(product, ATTR_COUNTRY))


def matches_country(product: dict, iso: str, normalize) -> bool:
    """
    Совпадает ли страна товара с iso (ISO-2). Берём атрибут «Страна»; если его нет —
    ищем название страны в тексте названия товара. normalize(текст) -> iso | None.
    """
    vals = attr_values(product, ATTR_COUNTRY)
    if vals:
        return any(normalize(v) == iso for v in vals)
    name = str(product.get("name") or "")
    for part in re.split(r"[|,/()\[\]:;]+", name):
        part = part.strip()
        if part and normalize(part) == iso:
            return True
        for word in part.split():
            if len(word) > 2 and normalize(word) == iso:
                return True
    return False


def product_days(product: dict) -> int | None:
    """Отлёжка в днях (атрибут «Отлежка»)."""
    for v in attr_values(product, ATTR_DAYS):
        digits = "".join(ch for ch in v if ch.isdigit())
        if digits:
            return int(digits)
    return None


def product_formats(product: dict) -> str:
    out = []
    for v in attr_values(product, ATTR_FORMAT):
        short = _FORMAT_SHORT.get(v.strip().lower(), v.strip()[:4])
        if short not in out:
            out.append(short)
    return "+".join(out)


def has_2fa(product: dict) -> bool:
    return any(v.strip().lower() in ("да", "yes", "1") for v in attr_values(product, ATTR_2FA))


# ── выдача ссылкой (продавцы кладут архив на Яндекс.Диск и пишут ссылку в txt) ─

MAX_DOWNLOAD_MB = 100          # архивы с tdata/session — это килобайты; больше — подозрительно
MAX_UNPACK_MB = 300            # защита от «бомбы» в zip
_LINK_RE = re.compile(r"(?:(?P<name>[^\s:]+)\s*:\s*)?(?P<url>https?://[^\s\"'<>]+)", re.I)


def parse_links(text: str) -> list[tuple[str | None, str]]:
    """Строки вида «18325147520.zip: https://disk.yandex.ru/d/xxxx» -> [(имя или None, ссылка)]."""
    out, seen = [], set()
    for line in text.splitlines():
        m = _LINK_RE.search(line)
        if m and m.group("url") not in seen:
            seen.add(m.group("url"))
            name = m.group("name")
            out.append((name if name and not name.lower().startswith("http") else None, m.group("url")))
    return out


def hint_from_name(name: str | None) -> str | None:
    """Номер телефона из имени файла «18325147520.zip» -> «18325147520» (или None)."""
    digits = "".join(ch for ch in (name or "").split(".")[0] if ch.isdigit())
    return digits if len(digits) >= 7 else None


def gdrive_id(url: str) -> str | None:
    """id файла из ссылки Google Drive: /file/d/<id>/..., open?id=<id>, uc?id=<id>."""
    if not re.search(r"drive\.google\.com|docs\.google\.com", url, re.I):
        return None
    m = re.search(r"/d/([A-Za-z0-9_-]{10,})", url) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", url)
    return m.group(1) if m else None


def fetch_link(url: str, dest_dir: str, *, http=None) -> str:
    """
    Скачивает файл по ссылке. Поддержаны публичные ссылки Яндекс.Диска (disk.yandex.*, yadi.sk)
    и прямые ссылки. Размер ограничен MAX_DOWNLOAD_MB. Возвращает путь к файлу.
    """
    http = http or requests
    os.makedirs(dest_dir, exist_ok=True)
    direct = url
    if re.search(r"(disk\.yandex\.[a-z]+|yadi\.sk)/", url, re.I):
        r = http.get("https://cloud-api.yandex.net/v1/disk/public/resources/download",
                     params={"public_key": url}, timeout=40)
        if r.status_code != 200:
            raise DarkstoreError(f"Яндекс.Диск не отдал файл (HTTP {r.status_code}) — ссылка недоступна или удалена")
        direct = (r.json() or {}).get("href")
        if not direct:
            raise DarkstoreError("Яндекс.Диск не вернул ссылку на скачивание")
    elif gdrive_id(url):
        # Публичный файл Google Drive. confirm=t обходит страницу «файл не проверен антивирусом»
        direct = f"https://drive.usercontent.google.com/download?id={gdrive_id(url)}&export=download&confirm=t"
    elif not re.search(r"\.(zip|rar|7z|tgz|gz|json|session)(\?|$)", url, re.I):
        raise DarkstoreError("ссылка не на Яндекс.Диск/Google Drive и не на архив — скачай вручную")
    r = http.get(direct, timeout=120, stream=True)
    if r.status_code != 200:
        raise DarkstoreError(f"скачивание не удалось (HTTP {r.status_code})"
                             + (" — Google Drive закрыл доступ или файл удалён" if gdrive_id(url) else ""))
    if "text/html" in str(r.headers.get("Content-Type", "")).lower():
        raise DarkstoreError("вместо файла пришла веб-страница — доступ к ссылке закрыт или нужен вход; скачай вручную")
    size = int(r.headers.get("Content-Length") or 0)
    if size > MAX_DOWNLOAD_MB * 1024 * 1024:
        raise DarkstoreError(f"файл слишком большой ({size // 1024 // 1024} МБ) — пропущен")
    path = os.path.join(dest_dir, f"dl_{uuid.uuid4().hex[:8]}.bin")
    total = 0
    with open(path, "wb") as f:
        for chunk in r.iter_content(65536):
            total += len(chunk)
            if total > MAX_DOWNLOAD_MB * 1024 * 1024:
                f.close()
                os.remove(path)
                raise DarkstoreError("файл слишком большой — скачивание прервано")
            f.write(chunk)
    return path
