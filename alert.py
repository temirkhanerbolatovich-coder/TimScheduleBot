"""Отправляет владельцу сообщение о сбое службы. Запускается systemd (OnFailure)."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import urllib.request


def main():
    unit = sys.argv[1] if len(sys.argv) > 1 else 'неизвестная служба'
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    state = Path(os.environ.get('BOT_STATE_DIR', ''))
    if not token or not state.is_absolute():
        raise SystemExit('Нужны TELEGRAM_BOT_TOKEN и абсолютный BOT_STATE_DIR')
    db = sqlite3.connect((state / 'plans.sqlite3').as_uri() + '?mode=ro', uri=True)
    row = db.execute("SELECT value FROM meta WHERE key='owner'").fetchone()
    db.close()
    if not row:
        raise SystemExit('Владелец не привязан')
    text = f'⚠️ Сбой службы {unit}. Посмотри журнал: sudo journalctl -u {unit} -n 30'
    req = urllib.request.Request('https://api.telegram.org/bot' + token + '/sendMessage',
                                 json.dumps({'chat_id': int(row[0]), 'text': text}).encode(),
                                 {'Content-Type': 'application/json'})
    urllib.request.urlopen(req, timeout=20).close()


if __name__ == '__main__':
    main()
