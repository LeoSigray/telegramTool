"""
Диагностика: смотрим что отдаёт LZT API для купленного аккаунта.
Запуск: python check_lzt_response.py
"""
import json
import requests
from config import CONFIG

TOKEN = CONFIG["LZT_TOKEN"]
BASE  = CONFIG["LZT_API_BASE_URL"].rstrip("/")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

# Вставь ID одного из своих аккаунтов
ITEM_ID = input("Введи item_id аккаунта (например 231772481): ").strip()

print(f"\nЗапрашиваю GET /{ITEM_ID} ...")
r = requests.get(f"{BASE}/{ITEM_ID}", headers=HEADERS, timeout=15)
print(f"Status: {r.status_code}")

data = r.json()

# Сохраняем полный ответ чтобы посмотреть все поля
with open("lzt_response_debug.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print("Полный ответ сохранён в lzt_response_debug.json")

# Показываем ключевые поля
item = data.get("item", data)
print("\n--- Ключевые поля ---")
for key in ["item_id", "login", "dc_id", "telegram_dc", "telegram_json",
            "loginData", "copyFormatData", "accountLinks", "auth_key"]:
    val = item.get(key)
    if val is not None:
        val_str = str(val)
        print(f"  {key}: {val_str[:120]}{'...' if len(val_str)>120 else ''}")

# Telegram json разбор
tj_raw = item.get("telegram_json", "")
if tj_raw:
    try:
        tj = json.loads(tj_raw)
        print("\n--- telegram_json поля ---")
        for k, v in tj.items():
            print(f"  {k}: {str(v)[:100]}")
    except Exception as e:
        print(f"telegram_json parse error: {e}")
