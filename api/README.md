# telegramTool HTTP API

Опциональный HTTP-слой поверх существующего CLI. Позволяет запускать рассылки/получать
статус из внешнего приложения (например — мобильной CRM).

## Запуск

```bash
pip install -r requirements.txt

# Сгенери токен и положи в .env
echo "API_TOKEN=$(openssl rand -hex 32)" > .env

./run_api.sh
# слушает 0.0.0.0:9000
```

Swagger UI: http://127.0.0.1:9000/docs

## Аутентификация

Каждый запрос (кроме `/health`) требует заголовок:

```
Authorization: Bearer <API_TOKEN>
```

## Endpoints

| Метод | Путь                              | Назначение                                      |
|-------|-----------------------------------|-------------------------------------------------|
| GET   | `/health`                         | Без авторизации; статус сервера                 |
| GET   | `/accounts`                       | Список аккаунтов из `sessions/`                 |
| GET   | `/accounts/check`                 | Проверить авторизацию (медленно)                |
| POST  | `/broadcast/chat/start`           | Запустить рассылку по чатам                     |
| GET   | `/broadcast/jobs`                 | Список запущенных и завершённых задач           |
| GET   | `/broadcast/jobs/{id}`            | Детали задачи (цели, лог)                       |
| POST  | `/broadcast/jobs/{id}/stop`       | Мягко остановить                                |
| DELETE| `/broadcast/jobs/{id}`            | Удалить запись о завершённой задаче             |

### POST /broadcast/chat/start

```json
{
  "message": "Привет! ...",
  "targets": ["@chat1", "https://t.me/chat2", "+abcdef..."],
  "parallel": 2
}
```

Возвращает `Job` с `id`. Прогресс смотрится через `GET /broadcast/jobs/{id}?include_log=true`.

## Webhook входящих сообщений (опционально)

Если задать `CRM_WEBHOOK_URL` и `CRM_WEBHOOK_TOKEN` — при старте сервера
поднимется listener: подключит все аккаунты из `sessions/` и при каждом
**входящем личном сообщении** будет POST'ить в этот URL:

```json
{
  "account_name": "+380981257112",
  "platform_user_id": "12345678",
  "username": "ivan",
  "display_name": "Ivan",
  "phone": null,
  "chat_id": "12345678",
  "message_id": "9876",
  "text": "Привет",
  "from_me": false,
  "sent_at": "2025-..."
}
```

Заголовок: `Authorization: Bearer $CRM_WEBHOOK_TOKEN`.

CRM-бэк в этом проекте принимает webhook на `/external/incoming` — у него
переменная `EXTERNAL_API_TOKEN` должна совпадать с `CRM_WEBHOOK_TOKEN` здесь.

## Заметки

- Хранение задач — **в памяти процесса**. Перезапустишь сервер — список задач пуст
  (но дневные счётчики и состояние сессий не теряются).
- Все клиенты живут в общем пуле. broadcast-задачи **не создают новые
  подключения** — пользуются пулом (поэтому listener продолжает ловить ответы
  во время и после рассылки).
- Лимиты (`CHAT_DELAY_*`, `JOIN_DELAY_*`, дневные, `DM_LIMIT_PER_ACCOUNT`)
  берутся из `config.py` / `data/daily_limits.py` — те же что и у CLI-версии.
