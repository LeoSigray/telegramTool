# Telegram Automator

Инструмент для автоматизации Telegram: скупка аккаунтов, рассылка в ЛС/чаты, инвайтинг.

## Быстрый старт

### 1. Клонировать репозиторий
```bash
git clone https://github.com/Leonardo2282/telegramTool.git
cd telegramTool
```

### 2. Создать виртуальное окружение и установить зависимости
```bash
python3 -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate

pip install -r requirements.txt
```

> ⚠️ **Важно:** `requirements.txt` фиксирует `telethon<1.25` — это обязательно для совместимости с `opentele` (импорт tdata). Устанавливай зависимости строго через `pip install -r requirements.txt`, не обновляй telethon отдельно.

### 3. Настроить конфигурацию
Открой `config.py` и заполни свои данные:
```python
CONFIG = {
    "LZT_TOKEN": "...",          # Токен LZT.market
    "TELEGRAM_API_ID": 0,        # Твой API ID (my.telegram.org)
    "TELEGRAM_API_HASH": "...",  # Твой API Hash
    ...
}
```

### 4. Запустить
```bash
python main.py
```

---

## Подготовка данных

### Excel файл (data/targets.xlsx)
Создаётся автоматически при первом запуске.

- Лист **Users** — список юзеров для ЛС и инвайтинга:
  | user_id | username | comment |
  |---------|----------|---------|
  | 123456  |          | клиент  |
  |         | @username| лид     |

- Лист **Chats** — список чатов для рассылки:
  | chat_id     | username  | comment |
  |-------------|-----------|---------|
  | -1001234567 |           | чат 1   |
  |             | @mychat   | чат 2   |

### Аккаунты
Сессии хранятся в папке `sessions/` (создаётся автоматически).
Добавить аккаунты можно двумя способами через меню:
- **Купить на LZT.market** — автоматически
- **Импортировать tdata** — папка из Telegram Desktop

### Прокси
Через меню **"Настройки прокси"**, формат:
```
socks5://user:pass@host:port
```

---

## Структура проекта
```
telegramTool/
├── main.py                 # Точка входа (консольное меню)
├── config.py               # Конфигурация и лимиты
├── proxy_manager.py        # SOCKS5 прокси
├── requirements.txt
├── accounts/
│   ├── manager.py          # Менеджер сессий
│   ├── lzt_buyer.py        # Скупка аккаунтов LZT.market
│   └── tdata_importer.py   # Импорт tdata → Telethon session
├── messaging/
│   ├── dm_sender.py        # Рассылка в ЛС
│   ├── chat_sender.py      # Рассылка по чатам
│   └── inviter.py          # Инвайтинг в группу
└── data/
    └── excel_manager.py    # Чтение/запись Excel
```

## Лимиты Telegram (встроены)
| Действие     | Задержка        | Лимит/аккаунт/день |
|--------------|-----------------|---------------------|
| ЛС           | 30–60 сек       | 20 сообщений        |
| Чаты         | 60–120 сек      | 10 сообщений        |
| Инвайтинг    | 5–15 сек        | 40 инвайтов         |
