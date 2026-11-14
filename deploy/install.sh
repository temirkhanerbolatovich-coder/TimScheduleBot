#!/usr/bin/env bash
# Run as root from the staging directory after transferring code and .env.
set -euo pipefail

python3 - <<'PY'
import sqlite3
import sys
from zoneinfo import ZoneInfo
assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'
print('Python, SQLite and timezone:', sys.version.split()[0], sqlite3.sqlite_version, ZoneInfo('Asia/Qyzylorda'))
PY

if ! id -u timschedulebot >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/timschedulebot --shell /usr/sbin/nologin timschedulebot
fi

install -d -m 755 -o root -g root /opt/timschedulebot
install -d -m 700 -o timschedulebot -g timschedulebot /var/lib/timschedulebot
install -m 644 -o root -g root $(ls *.py | grep -v -e '^test_' -e '^get_id\.py$') /opt/timschedulebot/
install -m 600 -o root -g root .env /etc/timschedulebot.env
install -m 644 -o root -g root deploy/timschedulebot.service deploy/timschedulebot-alert@.service deploy/timschedulebot-backup.service deploy/timschedulebot-backup.timer /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/timschedulebot.service /etc/systemd/system/timschedulebot-alert@.service /etc/systemd/system/timschedulebot-backup.service /etc/systemd/system/timschedulebot-backup.timer
systemctl daemon-reload
systemctl enable timschedulebot.service
systemctl restart timschedulebot.service
# Enable the backup timer after owner pairing and the first validated snapshot.
systemctl is-active timschedulebot.service
