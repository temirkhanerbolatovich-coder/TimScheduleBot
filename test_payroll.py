import datetime as dt
import unittest

from bot import Bot, Store, TZ, parse_note
from formatting import label
from payroll import month_list, month_report, money, parse_month, shift_month, shift_pay
from test_interface import RecordingTelegram

NOTE = chr(10).join([
    '14.11.2026г (Суббота) - День (Админ) Замена',
    '15.11.2026г. (Воскресенье) - 18ч. (Админ)',
    '16.11.2026г. (Понедельник) - Ночь (Админ)',
    '18.11.2026г. (Среда) - День (Админ)',
    '19.11.2026г. (Четверг) - День (Админ)',
    '20.11.2026г. (Пятница) - Ночь (Пом)',
])
NOW = dt.datetime(2026, 11, 17, 12, 0, tzinfo=TZ)


class PayrollTests(unittest.TestCase):
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

    def test_money_and_month_helpers(self):
        self.assertEqual(money(66000), '66 000 тг')
        self.assertEqual(parse_month('11.2026', dt.date(2026, 1, 1)), (2026, 11))
        self.assertEqual(parse_month('2026-03', dt.date(2026, 1, 1)), (2026, 3))
        self.assertEqual(shift_month(2026, 1, -1), (2025, 12))
        with self.assertRaises(ValueError):
            parse_month('13.2026', dt.date(2026, 1, 1))

    def test_pay_is_proportional_to_hours(self):
        rates = self.store.rates(123)
        by_date = {e['date']: e for e in self.store.events(123)}
        self.assertEqual(shift_pay(by_date['2026-11-14'], rates), 12000)
        self.assertEqual(shift_pay(by_date['2026-11-15'], rates), 18000)
        self.assertAlmostEqual(shift_pay(by_date['2026-11-20'], rates), 10000)

    def test_month_report_totals_and_split_by_time(self):
        text = month_report(self.store, 123, 2026, 11, NOW)
        self.assertIn('Админ</b>: 5 · 66 ч · 66 000 тг', text)
        self.assertIn('Помощник</b>: 1 · 12 ч · 10 000 тг', text)
        self.assertIn('Итого: 76 000 тг', text)
        self.assertIn('Ожидает отметки: 42 000 тг', text)  # 14.11 + 15.11 + 16.11 завершились к 17.11 12:00
        self.assertIn('Запланировано: 34 000 тг', text)
        self.assertIn('Ночь — 2', text)

    def test_marks_change_pay_and_support_undo(self):
        ident = next(e['id'] for e in self.store.events(123) if e['date'] == '2026-11-16')
        self.callback(f'shift:c:{ident}')
        self.assertIn('Меня заменили', self.api.messages[-1]['text'])
        text = month_report(self.store, 123, 2026, 11, NOW)
        self.assertIn('Итого: 64 000 тг', text)
        self.assertIn('Меня заменили: 1', text)
        self.message('/undo')
        self.callback(self.api.messages[-1]['keyboard'][0][0]['callback_data'])
        self.assertIn('Итого: 76 000 тг', month_report(self.store, 123, 2026, 11, NOW))

    def test_shift_without_role_is_reported_and_role_can_be_set(self):
        events, _ = parse_note('21.11.2026 - Ночь', dt.date(2026, 11, 14))
        with self.store.db:
            self.store.save(123, events[0])
        self.assertIn('Без роли: 1', month_report(self.store, 123, 2026, 11, NOW))
        ident = next(e['id'] for e in self.store.events(123) if e['date'] == '2026-11-21')
        self.callback(f'record:{ident}')
        self.assertIn('shift:a:', str(self.api.messages[-1]['keyboard']))
        self.callback(f'shift:h:{ident}')
        self.assertNotIn('Без роли', month_report(self.store, 123, 2026, 11, NOW))
        self.assertIn('Помощник</b>: 2', month_report(self.store, 123, 2026, 11, NOW))

    def test_rate_change_with_confirmation(self):
        self.message('/rates')
        self.callback('ratecfg:a')
        self.message('13 000 тг')
        self.callback(self.api.messages[-1]['keyboard'][0][0]['callback_data'])
        self.assertEqual(self.store.rates(123)['Админ'], 13000)
        self.assertIn('Итого: 76 000 тг', month_report(self.store, 123, 2026, 11, NOW))
        self.message('/undo')
        self.assertIn('нечего', self.api.messages[-1]['text'])  # изменение ставки не входит в журнал записей

    def test_month_screen_navigation_and_list(self):
        self.message('/month 11.2026')
        self.assertIn('Итоги · ноябрь 2026', self.api.messages[-1]['text'])
        keyboard = str(self.api.messages[-1]['keyboard'])
        self.assertIn('month:sum:2026-10', keyboard)
        self.assertIn('month:list:2026-11', keyboard)
        self.callback('month:list:2026-11')
        text = self.api.messages[-1]['text']
        self.assertIn('14.11 сб · День · Админ · 12 ч', text)
        self.assertIn('15.11 вс · 18 часов · Админ · 18 ч', text)
        self.callback('month:sum:2026-12')
        self.assertIn('Смен в этом месяце нет', self.api.messages[-1]['text'])
        self.message('/history')
        self.assertIn('Смены ·', self.api.messages[-1]['text'])

    def test_shortened_shift_hours(self):
        events, errors = parse_note('08.11.2026г. (Воскресенье) - День 8ч. (Админ)', dt.date(2026, 11, 1))
        self.assertFalse(errors)
        (e,) = events
        self.assertEqual((e['start'], e['duration'], e['role']), (480, 480, 'Админ'))
        self.assertAlmostEqual(shift_pay(e, self.store.rates(123)), 8000)
        self.assertNotIn('8ч', label(e))
        self.assertIn('08:00–16:00', label(e))
        events, _ = parse_note('09.11.2026 - Ночь с 21:00 6ч', dt.date(2026, 11, 1))
        self.assertEqual((events[0]['start'], events[0]['duration']), (1260, 360))

    def test_month_list_marks(self):
        text = month_list(self.store, 123, 2026, 11, NOW)
        self.assertIn('❔ 14.11', text)
        self.assertIn('⏳ 19.11', text)


if __name__ == '__main__':
    unittest.main()
