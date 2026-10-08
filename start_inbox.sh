#!/bin/bash
# Запускает бэкенд «Диалоги» на сервере (в tmux-окне inbox), если он ещё не запущен,
# и печатает токен последней строкой. Вызывается скриптом messenger.ps1 с ноутбука.
#
# По умолчанию подключает все аккаунты, КРОМЕ тех, что сейчас на прогреве (API_SKIP_WARMUP=1),
# чтобы не было конфликта сессий. Если прогрев закончен и нужны вообще все: ALL=1 ./start_inbox.sh
cd ~/telegramTool || exit 1

if ! tmux has-session -t inbox 2>/dev/null; then
  if [ "$ALL" = "1" ]; then SKIP=""; else SKIP="API_SKIP_WARMUP=1"; fi
  tmux new-session -d -s inbox "$SKIP .venv/bin/uvicorn api.server:app --host 127.0.0.1 --port 9000"
  echo "Запускаю бэкенд (подключение аккаунтов займёт до минуты)..." >&2
fi

# ждём, пока бэкенд начнёт слушать порт (аккаунты подключаются до старта приёма запросов)
for _ in $(seq 1 120); do
  (echo > /dev/tcp/127.0.0.1/9000) 2>/dev/null && break
  sleep 1
done

# токен: из .env (API_TOKEN) или автосгенерированный
if [ -f ~/.telegramtool/token ]; then
  cat ~/.telegramtool/token
elif [ -n "$API_TOKEN" ]; then
  echo "$API_TOKEN"
else
  grep -m1 '^API_TOKEN=' .env 2>/dev/null | cut -d= -f2-
fi
