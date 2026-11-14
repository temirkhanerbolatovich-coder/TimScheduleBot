import datetime as dt
import pathlib
import tempfile
import unittest
from bot import Bot, Store, TZ, digest, parse_note

TODAY = dt.date(2026, 10, 8)


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send(self, chat, text, keyboard=None, **kwargs):
        self.messages.append((chat, text, keyboard))

    def call(self, method, **kwargs):
        return True


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.api = FakeTelegram()
        self.bot = Bot(self.store, self.api, 123)

    def tearDown(self):
        self.store.db.close()

    def save(self, text):
        events, errors = parse_note(text, TODAY)
        self.assertFalse(errors)
        nonce = self.store.stage(123, {'type': 'save', 'events': events})
        self.assertTrue(self.store.commit(123, nonce))
        return self.store.events(123)

    def test_full_user_note(self):
        events, errors = parse_note(pathlib.Path(__file__).with_name('example_note.txt').read_text(encoding='utf-8'), TODAY)
        self.assertEqual(errors, [])
        self.assertEqual(len(events), 15)
        self.assertEqual(sum(e['weekday'] is not None for e in events), 7)
        self.assertEqual(events[0]['start'], 1200)
        self.assertEqual(events[1]['duration'], 1080)
        self.assertIn('коллегу', events[3]['details'])
        self.assertEqual(events[-1]['title'], 'Мастер класс по After Effects')

    def test_overnight_shift_is_visible_next_morning(self):
        self.save('08.10.2026 - Ночь')
        report = digest(self.store, 123, dt.date(2026, 10, 9))
        self.assertIn('Продолжение: ночная смена', report)
        self.assertIn('До 08:00 · началось 08.10 в 20:00', report)
        self.assertEqual(self.store.occurrences(123, dt.date(2026, 10, 10), 1), [])

    def test_custom_shift_and_day_durations(self):
        events, errors = parse_note('13.10.2026 - Ночь с 21:00 до 09:00\n14.10.2026 - 6 день\n21.10.2026 - 24', TODAY)
        self.assertFalse(errors)
        self.assertEqual([(e['start'], e['duration']) for e in events], [(1260, 720), (480, 360), (480, 1440)])

    def test_weekly_overlap(self):
        self.save('Среда:\nАнглийский с 14:00 до 17:00\nСУБД с 15:00 до 18:00')
        self.assertIn('Пересечение', digest(self.store, 123, dt.date(2026, 10, 14)))

    def test_reimport_updates_instead_of_duplicate(self):
        self.save('09.10.2026 10:00 Встреча')
        events = self.save('09.10.2026 11:00 Встреча')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['start'], 660)

    def test_bad_lines_are_reported_and_not_silently_saved(self):
        events, errors = parse_note('32.10.2026 10:00 Встреча\n09.10.2026 25:00 Встреча\nчто-нибудь\n09.10.2026 10:00 Встреча', TODAY)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(errors), 3)

    def test_draft_is_single_use_and_superseded(self):
        events, _ = parse_note('09.10.2026 10:00 Встреча', TODAY)
        first = self.store.stage(123, {'type': 'save', 'events': events})
        second = self.store.stage(123, {'type': 'save', 'events': events})
        self.assertFalse(self.store.commit(123, first))
        self.assertTrue(self.store.commit(123, second))
        self.assertFalse(self.store.commit(123, second))

    def test_single_override_preserves_other_weeks(self):
        e = self.save('Среда:\nАнглийский с 14:00 до 17:00')[0]
        replacement = dict(e, date='2026-10-15', weekday=None, start=1080)
        nonce = self.store.stage(123, {'type': 'override', 'id': e['id'], 'day': '2026-10-14', 'event': replacement})
        self.store.commit(123, nonce)
        self.assertFalse(self.store.occurrences(123, dt.date(2026, 10, 14), 1))
        self.assertEqual(self.store.occurrences(123, dt.date(2026, 10, 15), 1)[0]['start'], 1080)
        self.assertEqual(self.store.occurrences(123, dt.date(2026, 10, 21), 1)[0]['start'], 840)

    def test_scheduled_delivery_once_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(pathlib.Path(folder) / 'plans.db')
            store = Store(path)
            store.register(123)
            bot = Bot(store, self.api, 123)
            now = dt.datetime(2026, 10, 11, 20, 0, tzinfo=TZ)
            bot.scheduled(now)
            self.assertEqual(len(self.api.messages), 2)
            self.assertIn('12.10.2026', self.api.messages[1][1])
            store.db.close()
            store = Store(path)
            Bot(store, self.api, 123).scheduled(now)
            self.assertEqual(len(self.api.messages), 2)
            store.db.close()

    def test_failed_send_is_not_marked_delivered(self):
        self.store.register(123)
        def fail(*_, **kwargs):
            raise RuntimeError('network failure')
        self.api.send = fail
        with self.assertRaises(RuntimeError):
            self.bot.scheduled(dt.datetime(2026, 10, 8, 8, tzinfo=TZ))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 0)

    def test_foreign_user_is_ignored(self):
        self.bot.update({'message': {'chat': {'id': 456, 'type': 'private'}, 'from': {'id': 456}, 'text': '/start'}})
        self.assertEqual(self.api.messages, [])
        self.assertEqual(self.store.events(456), [])

    def test_move_requires_confirmation(self):
        e = self.save('09.10.2026 10:00 Встреча')[0]
        self.bot.update({'message': {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123}, 'text': f"Перенеси #{e['id']} на 15.10.2026 в 21:00"}})
        self.assertEqual(self.store.get(123, e['id'])['date'], '2026-10-09')
        callback = self.api.messages[-1][2][0][0]['callback_data']
        self.bot.update({'callback_query': {'id': '1', 'from': {'id': 123}, 'data': callback, 'message': {'chat': {'id': 123, 'type': 'private'}}}})
        self.assertEqual(self.store.get(123, e['id'])['date'], '2026-10-15')
        self.assertEqual(self.store.get(123, e['id'])['start'], 1260)


if __name__ == '__main__':
    unittest.main()
