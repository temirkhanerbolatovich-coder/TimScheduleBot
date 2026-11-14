#!/usr/bin/env bash
# Выполняется на сервере deploy.ps1. Аргумент: ИД выпуска (каталог в staging).
# Ставит новую версию кода поверх работающей службы; базу и токен не трогает.
set -euo pipefail
REL="$1"
STAGE="/home/ubuntu/timschedulebot-stage/$REL"
APP=/opt/timschedulebot

cd "$STAGE"
# Все модули бота, кроме тестов и вспомогательного get_id.py: новые файлы подхватываются сами.
FILES="$(ls *.py | grep -v -e '^test_' -e '^get_id[.]py$' | paste -sd' ')"
echo "Файлы: $FILES"
echo "[1/6] Тесты на сервере"
python3 -X utf8 -m unittest -q 2>&1 | tail -3

echo "[2/6] Снимок базы"
sudo -n systemctl start timschedulebot-backup.service
sudo -n bash -c 'ls -1t /var/lib/timschedulebot/backups/plans-*.sqlite3 | head -1'

echo "[3/6] Сохраняю прежний код"
sudo -n install -d -m 755 "$APP/releases/$REL"
for f in $FILES; do [ -f "$APP/$f" ] && sudo -n cp -p "$APP/$f" "$APP/releases/$REL/$f"; done

rollback() {
  echo "ОШИБКА: возвращаю прежний код" >&2
  sudo -n systemctl stop timschedulebot || true
  for f in $FILES; do [ -f "$APP/releases/$REL/$f" ] && sudo -n install -m 644 -o root -g root "$APP/releases/$REL/$f" "$APP/$f"; done
  sudo -n systemctl start timschedulebot
  echo "Прежняя версия возвращена, база не менялась." >&2
  exit 1
}

echo "[4/6] Останавливаю бота и ставлю файлы"
sudo -n systemctl stop timschedulebot
trap rollback ERR
for f in $FILES; do sudo -n install -m 644 -o root -g root "$STAGE/$f" "$APP/$f"; done
sudo -n install -m 644 -o root -g root deploy/*.service deploy/*.timer /etc/systemd/system/
sudo -n systemd-analyze verify /etc/systemd/system/timschedulebot.service /etc/systemd/system/timschedulebot-alert@.service /etc/systemd/system/timschedulebot-backup.service
sudo -n systemctl daemon-reload
SINCE="$(date '+%Y-%m-%d %H:%M:%S')"
sudo -n systemctl start timschedulebot

echo "[5/6] Жду подключения к Telegram"
ok=0
for i in $(seq 1 30); do
  if sudo -n journalctl -u timschedulebot --since "$SINCE" --no-pager | grep -q 'TimScheduleBot работает'; then ok=1; break; fi
  systemctl is-active --quiet timschedulebot || break
  sleep 2
done
[ "$ok" = 1 ]
trap - ERR

echo "[6/6] Итог"
systemctl is-active timschedulebot timschedulebot-backup.timer
echo "Выпуск $REL установлен. Откат: $APP/releases/$REL"
