import os
import time
import json
import asyncio
import sqlite3
import requests

from config import CONFIG, ACCOUNTS_DIR, SESSIONS_DIR

DC_IPS = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}


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
        self._balance_id = None

    def _request(self, method, endpoint, data=None):
        url = f"{self.base_url}{endpoint}"
        try:
            if method == "GET":
                r = requests.get(url, headers=self.headers, params=data, timeout=30)
            elif method == "POST":
                r = requests.post(url, headers=self.headers, data=data, timeout=30)
            else:
                raise ValueError(f"Неизвестный метод: {method}")
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            print(f"  HTTP ошибка {url}: {e}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"  Ошибка запроса {url}: {e}")
            return None

    def get_item(self, item_id: int) -> dict:
        """Полные данные аккаунта. Содержит telegram_dc_id, telegram_phone и др."""
        result = self._request("GET", f"/{item_id}")
        if result:
            return result.get("item") or result
        return {}

    def get_telegram_login_code(self, item_id: int) -> str | None:
        """Запрашивает код входа в Telegram у LZT (GET /{item_id}/telegram-login-code)."""
        result = self._request("GET", f"/{item_id}/telegram-login-code")
        if not result:
            return None
        codes = result.get("codes") or {}
        return codes.get("code") or None

    def get_balance_id(self):
        if self._balance_id:
            return self._balance_id
        result = self._request("GET", "/me")
        if not result:
            return None
        user = result.get("user", result)
        if float(user.get("balance", 0) or 0) > 0:
            return None
        for b in user.get("balances", []):
            if b.get("type") == "account" and float(b.get("balance", 0) or 0) > 0:
                self._balance_id = b["balance_id"]
                print(f"  Кошелёк: {b.get('title')} (баланс={b['balance']} ₽)")
                return self._balance_id
        return None

    def search_accounts(self, min_price=0, max_price=25, page=1):
        print(f"Ищу аккаунты Telegram (цена {min_price}–{max_price} руб.)...")
        params = {"pmin": min_price, "pmax": max_price, "page": page,
                  "order_by": "price_to_up", "spam": "no"}
        result = self._request("GET", "/telegram", params)
        if result and result.get("items"):
            print(f"Найдено {len(result['items'])} аккаунтов.")
            return result["items"]
        print("Аккаунты не найдены.")
        return []

    def fast_buy(self, item_id, price) -> dict:
        print(f"Покупаю item_id={item_id} за {price} руб...")
        params = {"price": int(price)}
        bid = self.get_balance_id()
        if bid:
            params["balance_id"] = bid
        result = self._request("POST", f"/{item_id}/fast-buy", params)
        if not result:
            return {}
        if result.get("status") == "ok":
            item = result["item"]
            print(f"Куплен item_id={item['item_id']} за {item.get('rub_price', price)} руб.")
            # Дозапрашиваем полные данные (там есть telegram_dc_id, telegram_phone)
            time.sleep(2)
            full = self.get_item(item["item_id"])
            if full:
                for k, v in item.items():
                    if k not in full or not full[k]:
                        full[k] = v
                return full
            return item
        print(f"Ошибка fast-buy: {result}")
        return {}

    def save_account_txt(self, item: dict):
        """Сохраняет сырые данные аккаунта в accounts/ITEM_ID.txt."""
        item_id = str(item.get("item_id", "unknown"))
        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        path = os.path.join(ACCOUNTS_DIR, f"{item_id}.txt")
        with open(path, "w", encoding="utf-8") as f:
            login = item.get("login") or ""
            f.write(f"login={login}\n")
            ld = item.get("loginData") or {}
            pwd = ld.get("password") or item.get("password") or ""
            if pwd:
                f.write(f"password={pwd}\n")
            tj = item.get("telegram_json") or ""
            if tj:
                f.write(f"telegram_json={tj}\n")
            # Сохраняем ключевые Telegram-поля явно
            for key in ("telegram_dc_id", "telegram_phone"):
                val = item.get(key)
                if val:
                    f.write(f"{key}={val}\n")
        print(f"  Данные сохранены: accounts/{item_id}.txt")


async def login_via_code(item: dict, api: "LZTMarketAPI") -> str | None:
    """
    Автоматический вход через Telethon + код с LZT API.
    Возвращает путь к .session или None.
    """
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from proxy_manager import get_telethon_proxy

    item_id  = str(item.get("item_id", "unknown"))
    phone    = str(item.get("telegram_phone", "")).strip()
    dc_id    = int(item.get("telegram_dc_id") or 2)
    password = (item.get("loginData") or {}).get("password") or \
               item.get("telegram_password_value") or ""

    if not phone:
        print(f"  [!] Нет номера телефона для {item_id}")
        return None

    # Нормализуем номер
    if not phone.startswith("+"):
        phone = f"+{phone}"

    session_path = os.path.join(SESSIONS_DIR, item_id)
    os.makedirs(SESSIONS_DIR, exist_ok=True)

    proxy = get_telethon_proxy()
    client = TelegramClient(
        session_path,
        CONFIG["TELEGRAM_API_ID"],
        CONFIG["TELEGRAM_API_HASH"],
        proxy=proxy,
    )

    try:
        await client.connect()

        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"  [session] Уже авторизован: {me.first_name} (@{me.username})")
            await client.disconnect()
            return f"{session_path}.session"

        print(f"  Отправляю код на {phone}...")
        await client.send_code_request(phone)

        # Даём LZT время получить код (обычно 5–15 сек)
        print(f"  Жду код от LZT API...")
        code = None
        for attempt in range(6):  # до 30 секунд
            await asyncio.sleep(5)
            code = api.get_telegram_login_code(int(item_id))
            if code:
                print(f"  Получен код: {code}")
                break
            print(f"  Код ещё не пришёл, попытка {attempt+1}/6...")

        if not code:
            print(f"  [!] Не удалось получить код от LZT для {item_id}")
            await client.disconnect()
            return None

        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            if password:
                print(f"  2FA пароль найден, ввожу...")
                await client.sign_in(password=password)
            else:
                print(f"  [!] Требуется 2FA пароль, но он не найден в данных аккаунта")
                await client.disconnect()
                return None

        me = await client.get_me()
        print(f"  [session] Вошёл: {me.first_name} (@{me.username}, id={me.id})")
        await client.disconnect()
        return f"{session_path}.session"

    except Exception as e:
        print(f"  [!] Ошибка входа для {item_id}: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        return None


def create_session_for_item(item: dict, api: "LZTMarketAPI") -> str | None:
    """
    Создаёт рабочую .session для аккаунта.
    Использует вход через код от LZT — самый надёжный способ.
    """
    item_id = str(item.get("item_id", "unknown"))
    print(f"  Создаю сессию через вход с кодом LZT...")
    session_path = asyncio.run(login_via_code(item, api))
    if session_path:
        return session_path

    print(f"  [!] Автовход не удался для {item_id}")
    print(f"  [!] Скачай .session вручную с LZT: lzt.market/{item_id}")
    return None


def buy_accounts_interactive():
    api = LZTMarketAPI()

    try:
        min_price = int(input("Минимальная цена (0): ") or "0")
        max_price = int(input("Максимальная цена (25): ") or "25")
        count     = int(input("Сколько купить (3): ")    or "3")
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
        price   = acc["price"]
        spam    = acc.get("telegram_spam_block", "unknown")
        print(f"\n  ID: {item_id} | Цена: {price} руб. | Спам-блок: {spam}")

        item = api.fast_buy(item_id, price)
        if not item:
            continue

        api.save_account_txt(item)
        session = create_session_for_item(item, api)
        bought += 1

        if bought < count:
            time.sleep(3)

    print(f"\nКуплено аккаунтов: {bought}/{count}")
    print("Проверь аккаунты: меню → 1 → 4")