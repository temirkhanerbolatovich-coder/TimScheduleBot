import datetime as dt
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from bot import Bot, Store, Telegram, TZ, MAIN_KEYBOARD, digest, parse_note
from message_format import message_chunks


class RecordingTelegram:
    def __init__(self):
        self.messages, self.calls = [], []

    def send(self, chat, text, keyboard=None, formatted=False):
        self.messages.append({'chat': chat, 'text': text, 'keyboard': keyboard, 'formatted': formatted})

    def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return True


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise AssertionError('Unbalanced message')

    def handle_data(self, data):
        self.text.append(data)


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.api = RecordingTelegram()
        self.bot = Bot(self.store, self.api, 123)
        self.store.register(123)

    def tearDown(self):
        self.store.db.close()

    def message(self, text, actor=123):
        self.bot.update({'message': {'chat': {'id': actor, 'type': 'private'},
                                    'from': {'id': actor}, 'text': text}})

    def callback(self, data, actor=123, include_message=True):
        callback = {'id': 'query', 'from': {'id': actor}, 'data': data}
        if include_message:
            callback['message'] = {'chat': {'id': actor, 'type': 'private'}}
        self.bot.update({'callback_query': callback})

    def save(self, text):
        events, errors = parse_note(text)
        self.assertFalse(errors)
        with self.store.db:
            for event in events:
                self.store.save(123, event)
        return self.store.events(123)

    def confirm(self):
        data = self.api.messages[-1]['keyboard'][0][0]['callback_data']
        self.callback(data)
        return data

    def test_main_menu_and_every_navigation_button_work_without_commands(self):
        self.message('/start')
        self.assertEqual(self.api.messages[-1]['keyboard'], MAIN_KEYBOARD)
        for text in ('📅 Сегодня', '🌅 Завтра', '🗓 Неделя', '📋 Мои записи',
                     '➕ Добавить планы', '⚙️ Сводки', '❓ Помощь', '↩️ Отмена'):
            before = len(self.api.messages)
            self.message(text)
            self.assertGreater(len(self.api.messages), before)
            self.assertFalse(self.store.events(123))

    def test_week_uses_names_rooms_and_conflict_times_instead_of_record_ids(self):
        self.save('Среда:\nАнглийский с 14:00 до 17:00, 402А, Преподаватель Г\nСУБД с 15:00 до 18:00, Казахстан')
        first = dt.datetime.now(TZ).date()
        first += dt.timedelta(days=(2 - first.weekday()) % 7)
        text = digest(self.store, 123, first, 7)
        self.assertIn('📍 Кабинет 402А', text)
        self.assertIn('👤 Преподаватель Г', text)
        self.assertIn('Пересечение 15:00–17:00', text)
        self.assertIn('Английский ↔ СУБД', text)
        self.assertNotRegex(text, r'#\d+')

    def test_shift_details_are_not_repeated_and_overnight_continuation_is_clear(self):
        self.save('08.10.2026 - Ночь')
        text = digest(self.store, 123, dt.date(2026, 10, 8), 2)
        self.assertIn('Ночная смена', text)
        self.assertIn('20:00–08:00 · до 09.10', text)
        self.assertIn('Продолжение: ночная смена', text)
        self.assertIn('До 08:00 · началось 08.10 в 20:00', text)
        self.assertNotIn('ℹ️ Ночь', text)

    def test_user_text_is_escaped_in_cards_previews_and_conflicts(self):
        self.message('09.10.2026 10:00 <script> & встреча')
        text = self.api.messages[-1]['text']
        self.assertIn('&lt;script&gt; &amp; встреча', text)
        self.assertNotIn('<script>', text)
        self.confirm()
        self.message('🗓 Неделя')
        self.assertNotIn('<script>', self.api.messages[-1]['text'])

    def test_record_list_has_pagination_and_selection_buttons(self):
        self.save('\n'.join(f'09.10.2026 10:00 Встреча {i}' for i in range(12)))
        self.message('📋 Мои записи')
        rows = self.api.messages[-1]['keyboard']
        self.assertEqual(sum(button['callback_data'].startswith('record:') for row in rows for button in row), 8)
        self.assertTrue(any(button['callback_data'] == 'records:1' for row in rows for button in row))
        self.callback('records:1')
        self.assertIn('страница 2', self.api.messages[-1]['text'])
        self.callback('record:1')
        self.assertIn('Встреча 0', self.api.messages[-1]['text'])

    def test_move_button_asks_only_for_date_time_and_requires_confirmation(self):
        event = self.save('09.10.2026 10:00 Встреча')[0]
        self.callback(f'act:move:{event["id"]}')
        self.message('15.10.2026 18:00')
        self.assertEqual(self.store.get(123, event['id'])['date'], '2026-10-09')
        self.confirm()
        updated = self.store.get(123, event['id'])
        self.assertEqual((updated['date'], updated['start']), ('2026-10-15', 1080))

    def test_bad_input_keeps_the_wizard_and_cancel_clears_it(self):
        event = self.save('09.10.2026 10:00 Встреча')[0]
        self.callback(f'act:move:{event["id"]}')
        self.message('завтра')
        self.assertIsNotNone(self.store.ui_state(123))
        self.assertEqual(self.store.get(123, event['id'])['date'], '2026-10-09')
        self.message('↩️ Отмена')
        self.assertIsNone(self.store.ui_state(123))

    def test_edit_regular_record_keeps_weekday_without_typing_a_command(self):
        event = self.save('Среда:\nАнглийский с 14:00 до 17:00')[0]
        self.callback(f'act:edit:{event["id"]}')
        self.message('Английский с 18:00 до 20:00, 402А')
        self.confirm()
        updated = self.store.get(123, event['id'])
        self.assertEqual((updated['weekday'], updated['start']), (2, 1080))

    def test_edit_oneoff_record_keeps_date_unless_a_new_date_is_entered(self):
        event = self.save('09.10.2026 10:00 Встреча')[0]
        for text, date in (('Новая встреча с 11:00 до 12:00', '2026-10-09'),
                           ('15.10.2026 Новая встреча с 11:00 до 12:00', '2026-10-15')):
            self.callback(f'act:edit:{event["id"]}')
            self.message(text)
            self.confirm()
            self.assertEqual(self.store.get(123, event['id'])['date'], date)

    def test_custom_shift_duration_is_not_labeled_with_standard_hours(self):
        self.message('09.10.2026 - 18 часов с 14:00 до 20:00')
        self.assertIn('Рабочая смена · 6 ч', self.api.messages[-1]['text'])
        self.assertNotIn('Смена на 18 часов', self.api.messages[-1]['text'])

    def test_delete_and_done_buttons_do_not_change_data_before_confirmation(self):
        event = self.save('09.10.2026 10:00 Встреча')[0]
        self.callback(f'act:done:{event["id"]}')
        self.assertFalse(self.store.get(123, event['id'])['done'])
        self.confirm()
        self.assertTrue(self.store.get(123, event['id'])['done'])
        self.callback(f'act:delete:{event["id"]}')
        self.assertIsNotNone(self.store.get(123, event['id']))
        self.confirm()
        self.assertIsNone(self.store.get(123, event['id']))

    def test_skip_occurrence_by_buttons_preserves_other_weeks(self):
        event = self.save('Среда:\nАнглийский с 14:00 до 17:00')[0]
        self.callback(f'act:skip:{event["id"]}')
        choice = self.api.messages[-1]['keyboard'][0][0]['callback_data']
        day = dt.date.fromisoformat(choice.rsplit(':', 1)[1])
        self.callback(choice)
        self.confirm()
        self.assertFalse(self.store.occurrences(123, day, 1))
        self.assertEqual(len(self.store.occurrences(123, day + dt.timedelta(days=7), 1)), 1)
        self.callback(f'act:skip:{event["id"]}')
        self.assertNotIn(choice, [button['callback_data'] for row in self.api.messages[-1]['keyboard'] for button in row])

    def test_override_occurrence_by_buttons_and_text_preserves_duration(self):
        event = self.save('Среда:\nАнглийский с 14:00 до 17:00')[0]
        self.callback(f'act:override:{event["id"]}')
        choice = self.api.messages[-1]['keyboard'][0][0]['callback_data']
        source = dt.date.fromisoformat(choice.rsplit(':', 1)[1])
        self.callback(choice)
        target = source + dt.timedelta(days=1)
        self.message(f'{target:%d.%m.%Y} 18:00')
        self.confirm()
        items = self.store.occurrences(123, target, 1)
        self.assertEqual((items[0]['start'], items[0]['duration']), (1080, 180))
        self.assertFalse(self.store.occurrences(123, source, 1))

    def test_summary_settings_can_be_changed_with_buttons(self):
        self.message('⚙️ Сводки')
        self.callback('settings:daily')
        self.callback('daily:08:00')
        self.callback('settings:weekly')
        self.callback('weeklyday:5')
        self.callback('weekly:5:20:00')
        self.callback('settings:pause')
        settings = self.store.db.execute('SELECT * FROM settings WHERE chat=123').fetchone()
        self.assertEqual((settings['daily'], settings['weekly'], settings['paused']), ('08:00', '5 20:00', 1))
        self.callback('settings:resume')
        self.assertEqual(self.store.db.execute('SELECT paused FROM settings WHERE chat=123').fetchone()[0], 0)

    def test_foreign_inline_and_malformed_callbacks_are_answered_without_side_effects(self):
        event = self.save('09.10.2026 10:00 Встреча')[0]
        self.callback(f'act:delete:{event["id"]}', actor=456)
        self.assertFalse(self.api.messages)
        self.callback('nav:list', include_message=False)
        self.assertFalse(self.api.messages)
        for data in ('act:delete:not-a-number', 'record:999', '', 'a' * 65):
            self.callback(data)
        self.assertEqual(len(self.api.calls), 6)
        self.assertIsNotNone(self.store.get(123, event['id']))
        self.assertFalse(self.store.db.execute('SELECT 1 FROM drafts').fetchone())

    def test_starting_new_wizard_invalidates_the_previous_confirmation(self):
        event = self.save('09.10.2026 10:00 Встреча')[0]
        self.callback(f'act:delete:{event["id"]}')
        old = self.api.messages[-1]['keyboard'][0][0]['callback_data']
        self.callback(f'act:move:{event["id"]}')
        self.callback(old)
        self.assertIsNotNone(self.store.get(123, event['id']))

    def test_wizard_survives_database_reopening(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'plans.sqlite3')
            store = Store(path)
            store.set_ui_state(123, {'command': '/daily'})
            store.db.close()
            reopened = Store(path)
            try:
                bot = Bot(reopened, self.api, 123)
                bot.update({'message': {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123}, 'text': '08:30'}})
                self.assertEqual(reopened.db.execute('SELECT daily FROM settings').fetchone()[0], '08:30')
                self.assertIsNone(reopened.ui_state(123))
            finally:
                reopened.db.close()

    def test_formatted_chunks_preserve_entities_emoji_and_balanced_tags(self):
        text = '<b>' + ('🌙 &amp; &lt;пример&gt; ' * 300) + '</b>\n<i>конец</i>'
        rendered = []
        chunks = message_chunks(text, formatted=True, limit=100)
        for chunk in chunks:
            parser = TextParser()
            parser.feed(chunk)
            self.assertFalse(parser.stack)
            plain = ''.join(parser.text)
            self.assertLessEqual(len(plain.encode('utf-16-le')) // 2, 100)
            rendered.append(plain)
        self.assertEqual(''.join(rendered), unescape(re.sub(r'</?(?:b|i)>', '', text)))

    def test_transport_sets_html_and_attaches_keyboard_only_to_last_chunk(self):
        api = Telegram('fake')
        with patch.object(api, 'call', return_value={'message_id': 1}) as call:
            api.send(123, '<b>' + '🌙' * 2500 + '</b>', MAIN_KEYBOARD, formatted=True)
        self.assertGreater(call.call_count, 1)
        self.assertEqual(call.call_args.kwargs['parse_mode'], 'HTML')
        self.assertEqual(call.call_args.kwargs['reply_markup'], MAIN_KEYBOARD)
        for earlier in call.call_args_list[:-1]:
            self.assertNotIn('reply_markup', earlier.kwargs)


if __name__ == '__main__':
    unittest.main()
