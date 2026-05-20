import os
import time
import json
import sqlite3
import requests

from config import CONFIG, ACCOUNTS_DIR, SESSIONS_DIR

# DC IP-адреса Telegram
DC_IPS = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}


def telegram_json_to_session(item_id: str, telegram_json_raw: str):
    """
    Конвертирует telegram_json (из LZT) в Telethon .session файл.
    Возвращает путь к .session файлу или None при ошибке.
    """
    try:
        data = json.loads(telegram_json_raw)
    except (json.JSONDecodeError, TypeError):
        print(f"  [session] Невалидный telegram_json для {item_id}")
        return None

    dc_id = data.get("dc_id") or data.get("dcId") or 2
    auth_key_hex = data.get("auth_key") or data.get("authKey") or ""

    if not auth_key_hex:
        print(f"  [session] Нет auth_key в telegram_json для {item_id}")
        return None

    # Декодируем auth_key (hex или base64)
    try:
        auth_key = bytes.fromhex(auth_key_hex)
    except (ValueError, TypeError):
        try:
            import base64
            auth_key = base64.b64decode(auth_key_hex)
        except Exception:
            print(f"  [session] Не удалось декодировать auth_key для {item_id}")
            return None

    if len(auth_key) != 256:
        print(f"  [session] Неверная длина auth_key: {len(auth_key)} байт (нужно 256)")
        return None

    os.makedirs(SESSIONS_DIR, exist_ok=True)
    session_path = os.path.join(SESSIONS_DIR, f"{item_id}.session")

    if os.path.exists(session_path):
        os.remove(session_path)

    # Создаём Telethon SQLite сессию version=7
    db = sqlite3.connect(session_path)
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
            md5_digest blob, file_size integer, type integer,
            id integer, hash integer,
            primary key(md5_digest, file_size, type)
        );
        CREATE TABLE update_state (
            id integer primary key, pts integer, qts integer,
            date integer, seq integer
        );
        INSERT INTO version VALUES (7);
    """)
    ip = DC_IPS.get(int(dc_id), DC_IPS[2])
    db.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        (int(dc_id), ip, 443, auth_key, None),
    )
    db.commit()
    db.close()

    print(f"  [session] Создана: sessions/{item_id}.session (dc={dc_id})")
    return session_path


class LZTMarketAPI:
    def __init__(self):
        token = CONFIG["LZT_TOKEN"]
        if not token:
            raise ValueError("LZT_TOKEN не установлен в config.py")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        self.base_url = CONFIG["LZT_API_BASE_URL"].rstrip("/")
        self.category_id = CONFIG["LZT_MARKET_ID"]
        self._balance_id = None

    def _request(self, method, endpoint, data=None):
        url = f"{self.base_url}{endpoint}"
        try:
            if method == "GET":
                response = requests.get(url, headers=self.headers, params=data, timeout=300)
            elif method == "POST":
                response = requests.post(url, headers=self.headers, data=data, timeout=300)
            else:
                raise ValueError(f"Неизвестный метод {method}")

            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                print(f"Невалидный JSON от {url}: {response.text[:500]}")
                return None

        except requests.exceptions.HTTPError as e:
            print(f"HTTP ошибка {url}: {e}")
            print(f"Ответ сервера: {response.text[:500]}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"Ошибка запроса к LZT.market: {e}")
            return None

    def get_balance_id(self):
        """Получает balance_id для 'Баланса для покупки аккаунтов'."""
        if self._balance_id:
            return self._balance_id

        result = self._request("GET", "/me")
        if not result:
            return None

        user = result.get("user", result)
        main_balance = float(user.get("balance", 0) or 0)
        if main_balance > 0:
            return None

        for b in user.get("balances", []):
            if b.get("type") == "account" and float(b.get("balance", 0) or 0) > 0:
                self._balance_id = b["balance_id"]
                print(f"  Кошелёк: {b.get('title')} (id={self._balance_id}, баланс={b['balance']} ₽)")
                return self._balance_id

        print("  Предупреждение: не найден кошелёк с балансом.")
        return None

    def search_accounts(self, min_price=0, max_price=25, page=1):
        """Ищет Telegram аккаунты на LZT.market."""
        print(f"Ищу аккаунты Telegram (цена {min_price}-{max_price} руб.)...")
        params = {
            "pmin": min_price,
            "pmax": max_price,
            "page": page,
            "order_by": "price_to_up",
            "spam": "no",
        }
        result = self._request("GET", "/telegram", params)
        if result and result.get("items"):
            print(f"Найдено {len(result['items'])} аккаунтов.")
            return result["items"]
        print("Аккаунты не найдены по заданным параметрам.")
        return []

    def fast_buy(self, item_id, price):
        """Покупает аккаунт через fast-buy."""
        print(f"Покупаю аккаунт item_id={item_id} за {price} руб...")
        params = {"price": int(price)}
        balance_id = self.get_balance_id()
        if balance_id:
            params["balance_id"] = balance_id

        result = self._request("POST", f"/{item_id}/fast-buy", params)
        if not result:
            print("Fast-buy: пустой ответ")
            return None

        if result.get("status") == "ok":
            item = result["item"]
            actual_price = item.get("rub_price", price)
            print(f"Куплен аккаунт item_id={item['item_id']} за {actual_price} руб.")
            return item

        print(f"Ошибка fast-buy: {result}")
        return None

    def save_account(self, item):
        """
        Сохраняет данные аккаунта в accounts/ и конвертирует
        telegram_json в sessions/ для Telethon.
        """
        item_id = str(item.get("item_id", "unknown"))
        login_data = item.get("loginData") or {}
        login = item.get("login")
        password = login_data.get("password")
        telegram_json_raw = item.get("telegram_json")

        if not login:
            print("В item нет login, нечего сохранять.")
            return None

        # Сохраняем сырые данные в accounts/
        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        file_path = os.path.join(ACCOUNTS_DIR, f"{item_id}.txt")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"login={login}\n")
            if password is not None:
                f.write(f"password={password}\n")
            if telegram_json_raw:
                f.write(f"telegram_json={telegram_json_raw}\n")
        print(f"  Данные сохранены: accounts/{item_id}.txt")

        # Конвертируем telegram_json → .session для Telethon
        if telegram_json_raw:
            session_path = telegram_json_to_session(item_id, telegram_json_raw)
            if session_path:
                return session_path
            else:
                print(f"  [!] Не удалось создать .session — проверь accounts/{item_id}.txt вручную")
        else:
            print(f"  [!] telegram_json отсутствует — аккаунт не будет доступен для рассылки")

        return file_path


def buy_accounts_interactive():
    """Интерактивная скупка аккаунтов через консоль."""
    api = LZTMarketAPI()

    try:
        min_price = int(input("Минимальная цена (0): ") or "0")
        max_price = int(input("Максимальная цена (25): ") or "25")
        count = int(input("Сколько купить (3): ") or "3")
    except ValueError:
        print("Некорректный ввод.")
        return

    accounts = api.search_accounts(min_price=min_price, max_price=max_price)
    if not accounts:
        return

    bought = 0
    for acc in accounts:
        if bought >= count:
            break

        item_id = acc["item_id"]
        price = acc["price"]
        spam = acc.get("telegram_spam_block", "unknown")
        print(f"\n  ID: {item_id} | Цена: {price} руб. | Спам-блок: {spam}")

        item = api.fast_buy(item_id, price)
        if not item:
            continue

        api.save_account(item)
        bought += 1
        if bought < count:
            time.sleep(2)

    print(f"\nКуплено аккаунтов: {bought}/{count}")
    print("Проверь аккаунты: меню → 1 → 4")