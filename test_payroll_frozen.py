import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from bot import Bot
from store import Store
from parsing import parse_note, TZ
from payroll import shift_pay, pay_state, month_report
from test_interface import RecordingTelegram


class FrozenPayrollTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.store.register(123)
        self.event = parse_note('14.11.2026 - День (Админ)')[0][0]
        with self.store.db:
            self.store.save(123, self.event)
        self.ident = self.store.events(123)[0]['id']

    def tearDown(self):
        self.store.db.close()

    def rate(self, value):
        nonce = self.store.stage(123, dict(type='rate', role='Админ', per12=value))
        self.store.commit(123, nonce)

    def test_reimport_and_edit_preserve_rate_and_status_new_shift_uses_new_rate(self):
        self.store.update_event(123, self.ident, 'work', status='worked')
        self.rate(24000)
        with self.store.db:
            self.store.save(123, self.event)
            self.store.save(123, dict(self.event, date='2026-11-15'))
        old, new = self.store.events(123)
        self.assertEqual((old['rate_per12'], old['status'], new['rate_per12']), (12000, 'worked', 24000))
        self.assertEqual(shift_pay(old, self.store.rates(123)), 12000)
        with self.store.db:
            self.store.save(123, dict(old, duration=480), old['id'])
        self.assertEqual(shift_pay(self.store.get(123, old['id']), self.store.rates(123)), 8000)

    def test_explicit_reprice_requires_confirmation_and_undo_restores_rate(self):
        self.rate(24000)
        api = RecordingTelegram()
        bot = Bot(self.store, api, 123)
        bot.callback(123, f'reprice:{self.ident}')
        self.assertEqual(self.store.get(123, self.ident)['rate_per12'], 12000)
        bot.callback(123, api.messages[-1]['keyboard'][0][0]['callback_data'])
        self.assertEqual(self.store.get(123, self.ident)['rate_per12'], 24000)
        with self.store.db:
            self.store.restore_last(123)
        self.assertEqual(self.store.get(123, self.ident)['rate_per12'], 12000)

    def test_states_and_covered_pay(self):
        now = dt.datetime(2026, 11, 15, tzinfo=TZ)
        event = self.store.get(123, self.ident)
        self.assertEqual(pay_state(event, now), 'pending')
        self.assertEqual(pay_state(event, now - dt.timedelta(days=2)), 'planned')
        for status in ('worked', 'extra'):
            self.assertEqual(pay_state(dict(event, status=status), now), 'confirmed')
        self.assertEqual(shift_pay(dict(event, status='covered'), self.store.rates(123)), 0)
        text = month_report(self.store, 123, 2026, 11, now)
        self.assertIn('Подтверждено: 0', text)
        self.assertIn('Ожидает отметки: 12', text)

    def test_legacy_migration_and_snapshot_are_stable_after_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sqlite3'
            store = Store(path)
            with store.db:
                store.save(123, self.event)
                row = store.events(123)[0]
                row.pop('rate_per12')
                store.db.execute('UPDATE events SET data=?', (json.dumps(row),))
                store.snapshot(123, 'legacy')
                store.db.execute("DELETE FROM meta WHERE key='frozen_rates_v1'")
            store.db.close()
            store = Store(path)
            self.assertEqual(store.events(123)[0]['rate_per12'], 12000)
            with store.db:
                store.db.execute('INSERT INTO rates VALUES(123, ?, 24000)', ('Админ',))
                store.restore_last(123)
            store.db.close()
            store = Store(path)
            self.assertEqual(store.events(123)[0]['rate_per12'], 12000)
            store.db.close()

    def test_end_prompt_deduplicated_and_failed_send_retries(self):
        api = RecordingTelegram()
        bot = Bot(self.store, api, 123)
        now = dt.datetime(2026, 11, 14, 20, 1, tzinfo=TZ)
        with self.store.db:
            self.store.db.execute("UPDATE settings SET daily='23:59'")
        original = api.send
        def fail(*args, **kwargs):
            raise RuntimeError('offline')
        api.send = fail
        with self.assertRaises(RuntimeError):
            bot.scheduled(now)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM deliveries').fetchone()[0], 0)
        api.send = original
        bot.scheduled(now)
        bot.scheduled(now)
        self.assertEqual(len(api.messages), 1)
        self.assertIn('Смена закончилась', api.messages[0]['text'])


if __name__ == '__main__':
    unittest.main()
