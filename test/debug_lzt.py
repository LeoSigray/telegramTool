"""
Отладочный скрипт для LZT.market API.
Проверяет баланс и пробует разные варианты fast-buy.
"""
import requests
import json
import sys

TOKEN = input("Вставь свой LZT_TOKEN: ").strip()

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
}

BASE = "https://prod-api.lzt.market"

# 1. Проверяем баланс через API
print("\n=== Проверка баланса через API ===")
r = requests.get(f"{BASE}/me", headers=headers, timeout=10)
print(f"Status: {r.status_code}")
try:
    data = r.json()
    user = data.get("user", data)
    print(f"Полный ответ:\n{json.dumps(user, ensure_ascii=False, indent=2)[:1200]}")
except Exception as e:
    print(f"Ошибка: {e}, тело: {r.text[:300]}")

# 2. Ищем один дешёвый аккаунт для теста
print("\n=== Поиск тестового аккаунта ===")
r2 = requests.get(
    f"{BASE}/telegram",
    headers=headers,
    params={"pmin": 0, "pmax": 30, "order_by": "price_to_up", "spam": "no"},
    timeout=10,
)
items = r2.json().get("items", []) if r2.status_code == 200 else []
if not items:
    print("Аккаунты не найдены")
    sys.exit()

acc = items[0]
item_id = acc["item_id"]
price = acc["price"]
print(f"  item_id={item_id}, price={price}, type={type(price)}")

# 3. Пробуем разные варианты запроса
print("\n=== Тест fast-buy (только запрос, без реальной покупки) ===")
variants = [
    {"price": price},
    {"price": int(price)},
    {"price": str(int(price))},
    {"price": price, "currency": "rub"},
    {"price": int(price), "currency": "rub"},
]

for i, params in enumerate(variants, 1):
    print(f"\nВариант {i}: {params}")
    r3 = requests.post(
        f"{BASE}/{item_id}/fast-buy",
        headers=headers,
        data=params,
        timeout=10,
    )
    print(f"  Status: {r3.status_code}")
    try:
        resp = r3.json()
        if resp.get("status") == "ok":
            print(f"  ✓ УСПЕХ! Куплен аккаунт!")
            break
        else:
            errors = resp.get("errors", resp)
            print(f"  ✗ Ошибка: {errors}")
    except Exception:
        print(f"  Ответ: {r3.text[:200]}")