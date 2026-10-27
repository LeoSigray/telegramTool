#!/bin/sh
# Записывает секрет в .env, не показывая его на экране и не оставляя в истории команд.
# Использование (на сервере, от пользователя tg):  sh vkpanel/set_secret.sh VKPANEL_BOT_TOKEN
set -e
key="$1"
[ -n "$key" ] || { echo "Использование: sh vkpanel/set_secret.sh ИМЯ_ПЕРЕМЕННОЙ"; exit 1; }
env_file="$(dirname "$0")/../.env"
printf "Значение для %s (ввод скрыт): " "$key"
stty -echo; read -r value; stty echo; echo
[ -n "$value" ] || { echo "Пусто, не меняю."; exit 1; }
if grep -q "^$key=" "$env_file"; then
  tmp="$(mktemp)"; grep -v "^$key=" "$env_file" > "$tmp"; cat "$tmp" > "$env_file"; rm -f "$tmp"
fi
printf '%s=%s\n' "$key" "$value" >> "$env_file"
chmod 600 "$env_file"
echo "Готово: $key записан в .env. Служба подхватит его сама в течение 30 секунд."
