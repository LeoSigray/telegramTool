#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ -z "$API_TOKEN" ] && [ -f .env ]; then
  set -a; source .env; set +a
fi

if [ -z "$API_TOKEN" ]; then
  echo "ERROR: API_TOKEN не задан. Создай .env или экспорти переменную."
  exit 1
fi

if [ -d .venv ]; then
  source .venv/bin/activate
fi

exec uvicorn api.server:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-9000}" --reload
