import datetime as dt
import unittest

from bot import Bot, Store, TZ, parse_note
from formatting import label
from queries import free_text, next_text
from shifts import build_event
from test_interface import RecordingTelegram


class Stage2Tests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.api = RecordingTelegram()
        self.bot = Bot(self.store, self.api, 123)
        self.store.register(123)

    def tearDown(self):
        self.store.db.close()

    def message(self, text, **extra):
        self.bot.update({'message': dict({'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123}, 'text': text}, **extra)})

    def callback(self, data):
        self.bot.update({'callback_query': {'id': 'q', 'from': {'id': 123}, 'data': data,
                                            'message': {'chat': {'id': 123, 'type': 'private'}}}})

    def confirm(self):
        self.callback(self.api.messages[-1]['keyboard'][0][0]['callback_data'])

    def test_template_flow_saves_shift_with_role(self):
        self.message('🧩 Смена')
        self.callback('tpl:t:night')
        self.callback('tpl:r:night:a')
        day = dt.date(2026, 11, 20)
        self.callback(f'tpl:d:night:a:{day.isoformat()}')
        self.assertIn('Админ', self.api.messages[-1]['text'])
        self.confirm()
        (event,) = self.store.events(123)
        self.assertEqual((event['date'], event['title'], event['role'], event['start'], event['duration']),
                         ('2026-11-20', 'Работа — ночь', 'Админ', 1200, 720))

    def test_template_custom_date(self):
        self.callback('tpl:o:h18:h')
        self.message('25.11.2026')
        self.assertIn('Помощник', self.api.messages[-1]['text'])
        self.confirm()
        (event,) = self.store.events(123)
        self.assertEqual((event['date'], event['duration']), ('2026-11-25', 1080))

    def test_shift_type_defaults_apply_to_new_notes_and_templates(self):
        self.callback('settings:shifts')
        self.callback('shiftcfg:night')
        self.message('21:00-09:00')
        self.confirm()
        events, _ = parse_note('13.10.2026 - Ночь', dt.date(2026, 10, 1), self.store.shift_defaults(123))
        self.assertEqual((events[0]['start'], events[0]['duration']), (1260, 720))
        event = build_event('night', 'a', dt.date(2026, 11, 2), self.store.shift_defaults(123))
        self.assertEqual(event['start'], 1260)

    def test_undo_restores_previous_state_and_journal(self):
        self.message('20.11.2026 - Ночь')
        self.confirm()
        self.message('21.11.2026 - День')
        self.confirm()
        self.assertEqual(len(self.store.events(123)), 2)
        self.message('/journal')
        self.assertIn('Добавление записей', self.api.messages[-1]['text'])
        self.message('/undo')
        self.confirm()
        self.assertEqual([e['date'] for e in self.store.events(123)], ['2026-11-20'])
        self.message('/undo')
        self.confirm()
        self.assertEqual(self.store.events(123), [])
        self.message('/undo')
        self.assertIn('нечего', self.api.messages[-1]['text'])

    def test_undo_brings_back_deleted_series_with_overrides(self):
        self.message('Среда:\nАнглийский с 14:00 до 17:00, 402А')
        self.confirm()
        ident = self.store.events(123)[0]['id']
        self.message(f'/delete {ident}')
        self.confirm()
        self.assertEqual(self.store.events(123), [])
        self.message('/undo')
        self.confirm()
        self.assertEqual(self.store.events(123)[0]['id'], ident)

    def test_forwarded_caption_is_parsed(self):
        self.message('', caption='20.11.2026 - Ночь', forward_date=1)
        self.assertIn('Сохранить', self.api.messages[-1]['text'])

    def test_next_and_free(self):
        now = dt.datetime(2026, 11, 20, 10, 0, tzinfo=TZ)
        events, _ = parse_note('20.11.2026 - Ночь\n21.11.2026 - Английский с 09:00 до 10:30, 402А', now.date())
        with self.store.db:
            for e in events:
                self.store.save(123, e)
        self.assertIn('через 10 ч', next_text(self.store, 123, now))
        night = dt.datetime(2026, 11, 20, 23, 0, tzinfo=TZ)
        self.assertIn('Идёт сейчас', next_text(self.store, 123, night))
        free = free_text(self.store, 123, dt.date(2026, 11, 20))
        self.assertIn('00:00–20:00 · 20 ч', free)
        free_next = free_text(self.store, 123, dt.date(2026, 11, 21))
        self.assertIn('10:30–', free_next)
        self.assertNotIn('00:00–', free_next)

    def test_role_and_short_18h_in_note(self):
        note = chr(10).join(['15.11.2026г. (Воскресенье) - 18ч. (Админ)', '20.11.2026г. (Пятница) - Ночь (Пом)',
                          '14.11.2026г (Суббота) - День (Админ) Замена', '21.11.2026 - Выезд в Астану в 14:50'])
        events, errors = parse_note(note, dt.date(2026, 11, 14))
        self.assertFalse(errors)
        self.assertEqual([(e['title'], e.get('role'), e['duration']) for e in events],
                         [('Работа — 18 часов', 'Админ', 1080), ('Работа — ночь', 'Помощник', 720),
                          ('Работа — день', 'Админ', 720), ('Выезд в Астану', None, 0)])
        self.assertIn('Замена', label(events[2]))
        self.assertNotIn('Админ)', label(events[2]))

    def test_new_commands_in_menu(self):
        self.message('⏭ Ближайшее')
        self.assertIn('Ближайших записей', self.api.messages[-1]['text'])
        self.message('/free завтра')
        self.assertIn('Свободное время', self.api.messages[-1]['text'])


if __name__ == '__main__':
    unittest.main()
