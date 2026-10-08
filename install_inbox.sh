#!/bin/bash
# Ставит бэкенд «Диалоги» (мессенджер) на сервере как ПОСТОЯННУЮ службу systemd и запускает её.
# Служба стартует вместе с сервером, сама перезапускается после любого падения и работает независимо от окон tmux,
# вашего ноутбука и консольных программ (прогрев/рассылка). Запускать от root. Можно запускать сколько угодно раз.
# Последней строкой печатает токен для входа. Вызывается из messenger.ps1.
APP=/home/tg/telegramTool
UNIT=/etc/systemd/system/telegramtool-inbox.service

if [ ! -x "$APP/.venv/bin/uvicorn" ]; then
  echo "ОШИБКА: на сервере нет $APP/.venv/bin/uvicorn - залей код (deploy.bat) и поставь зависимости" >&2
  exit 1
fi

cat > /tmp/telegramtool-inbox.service.new <<'EOF'
[Unit]
Description=telegramTool: мессенджер Диалоги (ловит ответы на рассылку 24/7)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=tg
Group=tg
WorkingDirectory=/home/tg/telegramTool
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/tg/telegramTool/.venv/bin/uvicorn api.server:app --host 127.0.0.1 --port 9000
Restart=always
RestartSec=20
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
EOF

CHANGED=0
if ! cmp -s /tmp/telegramtool-inbox.service.new "$UNIT"; then
  cp /tmp/telegramtool-inbox.service.new "$UNIT"
  systemctl daemon-reload
  CHANGED=1
  echo "Служба установлена." >&2
fi
rm -f /tmp/telegramtool-inbox.service.new
systemctl enable telegramtool-inbox >/dev/null 2>&1

# Старый способ запуска (окно tmux «inbox») занимает порт и аккаунты - убираем, дальше всё делает служба.
if su - tg -c 'tmux has-session -t inbox' 2>/dev/null; then
  su - tg -c 'tmux kill-session -t inbox' 2>/dev/null
  CHANGED=1
  echo "Старое окно tmux 'inbox' остановлено - теперь «Диалоги» работают как служба." >&2
  sleep 2
fi

if [ "$CHANGED" = "1" ]; then
  systemctl restart telegramtool-inbox
else
  systemctl start telegramtool-inbox
fi

# ждём, пока бэкенд начнёт слушать порт (аккаунты подключаются до старта приёма запросов)
echo "Жду запуск (подключение аккаунтов занимает до минуты)..." >&2
for _ in $(seq 1 150); do
  (echo > /dev/tcp/127.0.0.1/9000) 2>/dev/null && break
  sleep 1
done
if ! (echo > /dev/tcp/127.0.0.1/9000) 2>/dev/null; then
  echo "ВНИМАНИЕ: порт 9000 пока не слушается. Что пишет служба:" >&2
  journalctl -u telegramtool-inbox -n 15 --no-pager >&2
fi

# токен: API_TOKEN из .env важнее файла (так же выбирает api/auth.py)
TOKEN=$(grep -m1 '^API_TOKEN=' "$APP/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r')
if [ -z "$TOKEN" ] && [ -f /home/tg/.telegramtool/token ]; then
  TOKEN=$(cat /home/tg/.telegramtool/token)
fi
echo "$TOKEN"
