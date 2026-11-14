"""Хранилище SQLite: события, настройки, черновики, исключения."""
import datetime as dt
import json
import os
import sqlite3

from parsing import TZ


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, chat INTEGER NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(chat,key));
        CREATE TABLE IF NOT EXISTS settings(chat INTEGER PRIMARY KEY, daily TEXT DEFAULT '07:30', weekly TEXT DEFAULT '6 19:00', paused INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS drafts(chat INTEGER PRIMARY KEY, nonce TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS overrides(chat INTEGER, event INTEGER, day TEXT, data TEXT, PRIMARY KEY(chat,event,day));
        CREATE TABLE IF NOT EXISTS deliveries(chat INTEGER, kind TEXT, day TEXT, PRIMARY KEY(chat,kind,day));
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS ui_states(chat INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, chat INTEGER NOT NULL, ts TEXT NOT NULL, label TEXT NOT NULL, events TEXT NOT NULL, overrides TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rates(chat INTEGER, role TEXT, per12 INTEGER, PRIMARY KEY(chat,role));
        CREATE TABLE IF NOT EXISTS shift_defaults(chat INTEGER, title TEXT, start INTEGER, duration INTEGER, PRIMARY KEY(chat,title));
        ''')
        self.freeze_rates()

    def freeze_rates(self):
        """Backfill legacy records and undo snapshots once, using current rates."""
        if self.db.execute("SELECT 1 FROM meta WHERE key='frozen_rates_v1'").fetchone():
            return
        with self.db:
            for row in self.db.execute('SELECT id,chat,data FROM events').fetchall():
                event = json.loads(row['data'])
                if self.freeze_event_rate(row['chat'], event):
                    self.db.execute('UPDATE events SET data=? WHERE id=?', (json.dumps(event), row['id']))
            for row in self.db.execute('SELECT id,chat,events FROM snapshots').fetchall():
                entries = json.loads(row['events'])
                for entry in entries:
                    event = json.loads(entry[2])
                    self.freeze_event_rate(row['chat'], event)
                    entry[2] = json.dumps(event)
                self.db.execute('UPDATE snapshots SET events=? WHERE id=?', (json.dumps(entries), row['id']))

            self.db.execute("INSERT INTO meta VALUES('frozen_rates_v1', '1')")

    def freeze_event_rate(self, chat, event):
        from payroll import is_shift
        rates = self.rates(chat)
        if is_shift(event) and event.get('role') in rates and 'rate_per12' not in event:
            event['rate_per12'] = rates[event['role']]
            return True
        return False

    def register(self, chat):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO settings(chat) VALUES(?)', (chat,))

    def ui_state(self, chat):
        row = self.db.execute('SELECT data FROM ui_states WHERE chat=?', (chat,)).fetchone()
        return json.loads(row['data']) if row else None

    def set_ui_state(self, chat, state=None):
        with self.db:
            if state is None:
                self.db.execute('DELETE FROM ui_states WHERE chat=?', (chat,))
            else:
                self.db.execute('DELETE FROM drafts WHERE chat=?', (chat,))
                self.db.execute('INSERT OR REPLACE INTO ui_states VALUES(?,?)', (chat, json.dumps(state)))

    def events(self, chat):
        return [dict(json.loads(r['data']), id=r['id']) for r in self.db.execute('SELECT * FROM events WHERE chat=? ORDER BY id', (chat,))]

    def get(self, chat, ident):
        return next((e for e in self.events(chat) if e['id'] == ident), None)

    def save(self, chat, e, ident=None):
        e = {k: v for k, v in e.items() if k != 'id'}
        # Slot identity excludes details/time, so a corrected note updates its existing event.
        key = json.dumps([e['date'], e['weekday'], e['title'].casefold()], ensure_ascii=False)
        previous = self.get(chat, ident) if ident else None
        if previous is None:
            row = self.db.execute('SELECT data FROM events WHERE chat=? AND key=?', (chat, key)).fetchone()
            previous = json.loads(row['data']) if row else None
        if previous:
            if 'role' not in e and previous.get('role'):
                e['role'] = previous['role']
            if previous.get('role') == e.get('role') and 'rate_per12' in previous:
                e['rate_per12'] = previous['rate_per12']
            else:
                e.pop('rate_per12', None)
            if 'status' not in e and previous.get('status'):
                e['status'] = previous['status']
        self.freeze_event_rate(chat, e)
        data = json.dumps(e, ensure_ascii=False)
        if ident:
            self.db.execute('UPDATE events SET key=?, data=? WHERE id=? AND chat=?', (key, data, ident, chat))
        else:
            self.db.execute('INSERT INTO events(chat,key,data) VALUES(?,?,?) ON CONFLICT(chat,key) DO UPDATE SET data=excluded.data', (chat, key, data))

    def stage(self, chat, action):
        nonce = os.urandom(6).hex()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?)', (chat, nonce, json.dumps(action, ensure_ascii=False)))
        return nonce

    ACTION_LABELS = {'save': 'Добавление записей', 'edit': 'Изменение записи', 'delete': 'Удаление записи',
                     'override': 'Изменение одного занятия', 'shiftcfg': 'Время стандартной смены', 'rate': 'Ставка', 'undo': 'Отмена изменения'}

    def snapshot(self, chat, label):
        """Сохраняет состояние записей перед изменением, чтобы его можно было отменить."""
        events = [[r['id'], r['key'], r['data']] for r in self.db.execute('SELECT id,key,data FROM events WHERE chat=? ORDER BY id', (chat,))]
        overrides = [[r['event'], r['day'], r['data']] for r in self.db.execute('SELECT event,day,data FROM overrides WHERE chat=?', (chat,))]
        ts = dt.datetime.now(TZ).strftime('%d.%m.%Y %H:%M')
        self.db.execute('INSERT INTO snapshots(chat,ts,label,events,overrides) VALUES(?,?,?,?,?)',
                        (chat, ts, label, json.dumps(events), json.dumps(overrides)))
        self.db.execute('DELETE FROM snapshots WHERE chat=? AND id NOT IN (SELECT id FROM snapshots WHERE chat=? ORDER BY id DESC LIMIT 30)', (chat, chat))

    def last_snapshot(self, chat):
        return self.db.execute('SELECT * FROM snapshots WHERE chat=? ORDER BY id DESC LIMIT 1', (chat,)).fetchone()

    def journal(self, chat, limit=10):
        return self.db.execute('SELECT ts,label FROM snapshots WHERE chat=? ORDER BY id DESC LIMIT ?', (chat, limit)).fetchall()

    def restore_last(self, chat):
        row = self.last_snapshot(chat)
        if not row:
            return False
        self.db.execute('DELETE FROM events WHERE chat=?', (chat,))
        self.db.execute('DELETE FROM overrides WHERE chat=?', (chat,))
        self.db.executemany('INSERT INTO events(id,chat,key,data) VALUES(?,?,?,?)', [(i, chat, k, d) for i, k, d in json.loads(row['events'])])
        self.db.executemany('INSERT INTO overrides(chat,event,day,data) VALUES(?,?,?,?)', [(chat, e, d, x) for e, d, x in json.loads(row['overrides'])])
        self.db.execute('DELETE FROM snapshots WHERE id=?', (row['id'],))
        return True

    def rates(self, chat):
        from payroll import DEFAULT_RATES
        found = dict(DEFAULT_RATES)
        found.update({r['role']: r['per12'] for r in self.db.execute('SELECT role,per12 FROM rates WHERE chat=?', (chat,))})
        return found

    def update_event(self, chat, ident, label, **changes):
        """Меняет поля записи с сохранением снимка для /undo."""
        event = self.get(chat, ident)
        if not event:
            raise ValueError('Запись уже удалена')
        with self.db:
            self.snapshot(chat, label)
            self.save(chat, dict(event, **changes), ident)

    def shift_defaults(self, chat):
        return {r['title']: (r['start'], r['duration']) for r in self.db.execute('SELECT * FROM shift_defaults WHERE chat=?', (chat,))}

    def commit(self, chat, nonce):
        row = self.db.execute('SELECT * FROM drafts WHERE chat=? AND nonce=?', (chat, nonce)).fetchone()
        if not row:
            return False
        a = json.loads(row['data'])
        with self.db:
            if a['type'] == 'undo':
                if not self.restore_last(chat):
                    self.db.execute('DELETE FROM drafts WHERE chat=?', (chat,))
                    return False
                self.db.execute('DELETE FROM drafts WHERE chat=?', (chat,))
                return True
            if a['type'] not in ('shiftcfg', 'rate'):
                self.snapshot(chat, self.ACTION_LABELS.get(a['type'], a['type']))
            if a['type'] == 'shiftcfg':
                self.db.execute('INSERT OR REPLACE INTO shift_defaults VALUES(?,?,?,?)', (chat, a['title'], a['start'], a['duration']))
            elif a['type'] == 'reprice':
                event = self.get(chat, a['id'])
                if not event or event.get('role') != a['role']:
                    raise ValueError('Роль смены изменилась; открой карточку заново')
                event['rate_per12'] = a['per12']
                self.db.execute('UPDATE events SET data=? WHERE chat=? AND id=?',
                                (json.dumps(event), chat, a['id']))
            elif a['type'] == 'rate':
                self.db.execute('INSERT OR REPLACE INTO rates VALUES(?,?,?)', (chat, a['role'], a['per12']))
            elif a['type'] == 'save':
                for e in a['events']:
                    self.save(chat, e)
            elif a['type'] == 'edit':
                self.save(chat, a['event'], a['id'])
            elif a['type'] == 'delete':
                self.db.execute('DELETE FROM events WHERE chat=? AND id=?', (chat, a['id']))
                self.db.execute('DELETE FROM overrides WHERE chat=? AND event=?', (chat, a['id']))
            elif a['type'] == 'override':
                self.db.execute('INSERT OR REPLACE INTO overrides VALUES(?,?,?,?)', (chat, a['id'], a['day'], json.dumps(a['event'])))
            self.db.execute('DELETE FROM drafts WHERE chat=?', (chat,))
        return True

    def occurrences(self, chat, first, days):
        begin = dt.datetime.combine(first, dt.time(), TZ)
        finish = begin + dt.timedelta(days=days)
        found = []
        for e in self.events(chat):
            if e['done']:
                continue
            overrides = {r['day']: json.loads(r['data']) for r in self.db.execute('SELECT day,data FROM overrides WHERE chat=? AND event=?', (chat, e['id']))}
            if e['date']:
                dates = [dt.date.fromisoformat(e['date'])]
            else:
                dates = [first + dt.timedelta(days=i) for i in range(-1, days) if (first + dt.timedelta(days=i)).weekday() == e['weekday'] and (first + dt.timedelta(days=i)).isoformat() >= e['since']]
            instances = [(d, e) for d in dates if d.isoformat() not in overrides]
            instances += [(dt.date.fromisoformat(v['date']), dict(v, id=e['id'])) for v in overrides.values() if v is not None]
            for d, item in instances:
                start = dt.datetime.combine(d, dt.time(), TZ) + dt.timedelta(minutes=item['start'])
                end = start + dt.timedelta(minutes=item['duration'])
                if start < finish and (end > begin or (item['duration'] == 0 and start >= begin)):
                    found.append(dict(item, at=start, end=end))
        return sorted(found, key=lambda x: (x['at'], x['id']))
