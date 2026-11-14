import datetime as dt
import unittest

from bot import Bot, Store, TZ, parse_note
from monthview import day_marks, month_grid, week_summary
from test_interface import RecordingTelegram

NOTE = chr(10).join([
    '14.11.2026г (Суббота) - День (Админ) Замена',
    '15.11.2026г. (Воскресенье) - 18ч. (Админ)',
    '16.11.2026г. (Понедельник) - Ночь (Админ)',
    '18.11.2026г. (Среда) - День (Админ)',
    '19.11.2026г. (Четверг) - День (Админ)',
    '20.11.2026г. (Пятница) - Ночь (Пом)',
    '21.11.2026 - Выезд в Астану в 14:50',
    '22.11.2026 - Прибытие в Астану в 08:05',
    '23.11.2026 - Университет в 13:00',
    '24.11.2026 - Вылет в Грузию в 07:20',
])


class Stage4Tests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.api = RecordingTelegram()
        self.bot = Bot(self.store, self.api, 123)
        self.store.register(123)
        events, errors = parse_note(NOTE, dt.date(2026, 11, 14))
        self.assertFalse(errors)
        with self.store.db:
            for e in events:
                self.store.save(123, e)

    def tearDown(self):
        self.store.db.close()

    def callback(self, data):
        self.bot.update({'callback_query': {'id': 'q', 'from': {'id': 123}, 'data': data,
                                            'message': {'chat': {'id': 123, 'type': 'private'}}}})

    def message(self, text):
        self.bot.update({'message': {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123}, 'text': text}})

    def test_day_marks(self):
        marks = day_marks(self.store, 123, 2026, 11)
        self.assertEqual((marks[14], marks[15], marks[16], marks[20]), ('☀️', '🕑', '🌙', '🌙'))
        self.assertEqual((marks[21], marks[23], marks[24]), ('•', '•', '•'))
        self.assertNotIn(17, marks)  # продолжение ночной смены не отмечается на следующем дне

    def test_grid_layout_and_navigation(self):
        text, rows = month_grid(self.store, 123, 2026, 11, dt.date(2026, 11, 14))
        self.assertIn('Ноябрь 2026', text)
        self.assertTrue(all(len(r) == 7 for r in rows[1:-1]))
        self.assertEqual(rows[0][0]['callback_data'], 'cal:m:2026-10')
        self.assertEqual(rows[0][2]['callback_data'], 'cal:m:2026-12')
        flat = {b['text']: b['callback_data'] for r in rows[2:-1] for b in r}
        self.assertEqual(flat['[14☀️]'], 'cal:d:2026-11-14')
        self.assertEqual(flat['16🌙'], 'cal:d:2026-11-16')
        self.assertTrue(all(len(b['callback_data'].encode()) <= 64 for r in rows for b in r))

    def test_calendar_command_buttons_and_day_view(self):
        self.message('📆 Месяц')
        self.assertIn('Ноябрь 2026', self.api.messages[-1]['text'])
        self.callback('cal:m:2026-12')
        self.assertIn('Декабрь 2026', self.api.messages[-1]['text'])
        before = len(self.api.messages)
        self.callback('cal:x')
        self.assertEqual(len(self.api.messages), before)
        self.callback('cal:d:2026-11-16')
        self.assertIn('Ночная смена · Админ', self.api.messages[-1]['text'])
        self.assertIn('cal:m:2026-11', str(self.api.messages[-1]['keyboard']))
        self.message('/calendar 03.2027')
        self.assertIn('Март 2027', self.api.messages[-1]['text'])

    def test_week_summary_totals_and_free_days(self):
        text, keyboard = week_summary(self.store, 123, dt.date(2026, 11, 16), dt.datetime(2026, 11, 14, tzinfo=TZ))
        self.assertIn('Смен: 4 · 48 ч · ночных: 2', text)
        self.assertIn('46 000 тг', text)
        self.assertIn('Свободные дни: нет', text)
        self.assertEqual(keyboard[0][1]['callback_data'], 'wk:2026-11-23')
        next_text, _ = week_summary(self.store, 123, dt.date(2026, 11, 23), dt.datetime(2026, 11, 14, tzinfo=TZ))
        self.assertIn('Смен на этой неделе нет', next_text)
        self.assertIn('ср 25.11', next_text)
        self.assertNotIn('пн 23.11,', next_text)

    def test_week_summary_command_and_navigation(self):
        self.message('📊 Итог недели')
        self.assertIn('Итог недели', self.api.messages[-1]['text'])
        self.callback('wk:2026-11-18')  # среда приводится к понедельнику
        self.assertIn('16.11 — 22.11.2026', self.api.messages[-1]['text'])

    def test_weekly_digest_includes_summary(self):
        now = dt.datetime(2026, 11, 15, 19, 30, tzinfo=TZ)  # воскресенье
        self.assertEqual(now.weekday(), 6)
        self.bot.scheduled(now)
        weekly = [m for m in self.api.messages if 'Следующая неделя' in m['text']]
        self.assertTrue(weekly)
        self.assertIn('Итог недели', weekly[-1]['text'])


if __name__ == '__main__':
    unittest.main()
