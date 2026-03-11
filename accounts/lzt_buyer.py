import os
import time
import json
import requests

from config import CONFIG, ACCOUNTS_DIR, SESSIONS_DIR


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

    def _request(self, method, endpoint, data=None):
        url = f"{self.base_url}{endpoint}"
        try:
            if method == "GET":
                response = requests.get(url, headers=self.headers, params=data, timeout=300)
            elif method == "POST":
                response = requests.post(url, headers=self.headers, json=data, timeout=300)
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
        result = self._request("POST", f"/{item_id}/fast-buy", {"price": price})

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
        """Сохраняет данные купленного аккаунта в файл."""
        item_id = item.get("item_id", "unknown")
        login_data = item.get("loginData") or {}
        login = item.get("login")
        password = login_data.get("password")
        telegram_json_raw = item.get("telegram_json")

        if not login:
            print("В item нет login, нечего сохранять.")
            return None

        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        file_path = os.path.join(ACCOUNTS_DIR, f"{item_id}.txt")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"login={login}\n")
            if password is not None:
                f.write(f"password={password}\n")
            if telegram_json_raw:
                f.write(f"telegram_json={telegram_json_raw}\n")

        print(f"Данные аккаунта сохранены: {file_path}")
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
