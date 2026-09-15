#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ -z "$API_TOKEN" ] && [ -f .env ]; then
  set -a; source .env; set +a
fi

# API_TOKEN больше не обязателен: если не задан, api/auth.py сам сгенерирует
# токен при старте и сохранит в ~/.telegramtool/token (десктоп-приложение
# читает его оттуда автоматически).
if [ -z "$API_TOKEN" ]; then
  echo "API_TOKEN не задан — будет автосгенерирован в ~/.telegramtool/token"
fi

if [ -d .venv ]; then
  source .venv/bin/activate
fi

exec uvicorn api.server:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-9000}" --reload
