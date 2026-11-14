"""Сводка о состоянии бота для команды /status."""
import datetime as dt
import os
from pathlib import Path
import time

from parsing import TZ
from version import __version__

STARTED = time.time()


def duration(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    parts = ([f'{days} д'] if days else []) + ([f'{hours} ч'] if hours or days else []) + [f'{rest // 60} мин']
    return ' '.join(parts)


def status_text(store, chat, state_dir=None):
    db = store.db
    size = db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
    events = db.execute('SELECT COUNT(*) FROM events WHERE chat=?', (chat,)).fetchone()[0]
    last = db.execute('SELECT kind, day FROM deliveries WHERE chat=? ORDER BY day DESC, kind LIMIT 1', (chat,)).fetchone()
    lines = ['🤖 <b>Состояние бота</b>',
             f'Версия: {__version__}',
             f'Работает: {duration(time.time() - STARTED)}',
             f'Записей: {events}',
             f'База: {size / 1024:.0f} КБ',
             'Последняя сводка: ' + (f"{last['day']} ({'ежедневная' if last['kind'] == 'daily' else 'недельная'})" if last else 'ещё не было')]
    state = state_dir or os.environ.get('BOT_STATE_DIR')
    if state:
        snaps = sorted((Path(state) / 'backups').glob('plans-*.sqlite3'))
        if snaps:
            when = dt.datetime.fromtimestamp(snaps[-1].stat().st_mtime, TZ)
            lines.append(f'Последняя копия базы: {when:%d.%m.%Y %H:%M}')
        else:
            lines.append('Последняя копия базы: нет')
    return '\n'.join(lines)
