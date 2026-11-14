"""Create a consistent SQLite snapshot without stopping the planner."""
import datetime as dt
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import time


def backup_database(state_dir, keep_days=14):
    """Back up the existing database and expire only this script's old snapshots."""
    database = state_dir / 'plans.sqlite3'
    if not database.is_file():
        raise FileNotFoundError('Planner database does not exist yet')
    backups = state_dir / 'backups'
    backups.mkdir(mode=0o700, exist_ok=True)
    name = 'plans-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S') + '.sqlite3'
    destination = backups / name
    temporary = destination.with_suffix('.tmp')
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source, \
                closing(sqlite3.connect(temporary)) as target:
            source.backup(target)
            if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    threshold = time.time() - keep_days * 86400
    for snapshot in backups.iterdir():
        if (re.fullmatch(r'plans-\d{8}-\d{6}\.sqlite3', snapshot.name)
                and snapshot.is_file() and not snapshot.is_symlink()
                and snapshot.stat().st_mtime < threshold):
            snapshot.unlink()
    return destination


if __name__ == '__main__':
    os.umask(0o077)
    state = Path(os.environ.get('BOT_STATE_DIR', ''))
    if not state.is_absolute():
        raise SystemExit('Set an absolute BOT_STATE_DIR')
    result = backup_database(state)
    print('Database backup completed: ' + result.name)
