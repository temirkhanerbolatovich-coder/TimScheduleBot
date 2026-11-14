import contextlib
import io
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import time
import urllib.error
import unittest
from unittest.mock import Mock, patch

from bot import Bot, Store, Telegram, TelegramError, run
from run_bot import SetupError, ensure_owner, load_config, main, matching_owner, single_instance

IDENTITY = {'id': 987, 'is_bot': True, 'username': 'TestPlannerBot', 'first_name': 'Test planner'}
CODE = 'setup_' + 'a' * 32
FAKE_TOKEN = '123456:' + 'test_value_' * 4


def message(ident=123, text='/start ' + CODE, date=100, chat_type='private'):
    return {'update_id': 5, 'message': {'date': date, 'text': text,
            'chat': {'id': ident, 'type': chat_type}, 'from': {'id': ident, 'is_bot': False}}}


class FakeTelegram:
    def __init__(self, updates=None):
        self.updates = updates or []
        self.calls = []

    def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == 'getMe':
            return IDENTITY
        if method == 'getWebhookInfo':
            return {'url': ''}
        if method == 'getUpdates':
            return self.updates
        raise AssertionError('Unexpected API call: ' + method)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.config = self.root / '.env'
        self.store = Store(':memory:')

    def tearDown(self):
        self.store.db.close()
        self.folder.cleanup()

    def test_utf8_bom_quotes_and_comments(self):
        self.config.write_text('# настройки\nTELEGRAM_BOT_TOKEN="' + FAKE_TOKEN +
                               '"\nBOT_TIMEZONE=Asia/Qyzylorda\n', encoding='utf-8-sig')
        config = load_config(self.config)
        self.assertEqual(config['TELEGRAM_BOT_TOKEN'], FAKE_TOKEN)
        self.assertEqual(config['BOT_TIMEZONE'], 'Asia/Qyzylorda')

    def test_bad_config_is_rejected_without_exposing_values(self):
        for content in ('TELEGRAM_BOT_TOKEN=PASTE_TOKEN_HERE',
                        'TELEGRAM_BOT_TOKEN=' + FAKE_TOKEN + '\nUNKNOWN=private_value',
                        'TELEGRAM_BOT_TOKEN=' + FAKE_TOKEN + '\nTELEGRAM_BOT_TOKEN=other',
                        'TELEGRAM_BOT_TOKEN=' + FAKE_TOKEN + '\nBOT_TIMEZONE=../bad',
                        'TELEGRAM_BOT_TOKEN=' + FAKE_TOKEN + '\nBOT_TIMEZONE=Unknown/Zone'):
            with self.subTest(content_type=content.splitlines()[-1].partition('=')[0]):
                self.config.write_text(content, encoding='utf-8')
                with self.assertRaises(SetupError) as caught:
                    load_config(self.config)
                self.assertNotIn(FAKE_TOKEN, str(caught.exception))
                self.assertNotIn('private_value', str(caught.exception))

    def test_missing_config_has_a_useful_error(self):
        with self.assertRaisesRegex(SetupError, '.env.example'):
            load_config(self.config)

    def test_only_fresh_direct_private_pairing_is_accepted(self):
        self.assertEqual(matching_owner(message(), CODE, 100), 123)
        rejected = [message(text='/start'), message(text='/start wrong'), message(text='Привет, бот!'),
                    message(date=99), message(chat_type='group'), message(ident=-123),
                    {'callback_query': {'from': {'id': 123}, 'data': CODE}}]
        for field, value in [('forward_origin', {'type': 'user'}),
                             ('via_bot', {'id': 999}), ('business_connection_id', 'business')]:
            update = message()
            update['message'][field] = value
            rejected.append(update)
        spoofed = message()
        spoofed['message']['from']['id'] = 456
        rejected.append(spoofed)
        bot_actor = message()
        bot_actor['message']['from']['is_bot'] = True
        rejected.append(bot_actor)
        for update in rejected:
            with self.subTest(update_kind=tuple(update)):
                self.assertIsNone(matching_owner(update, CODE, 100))

    def test_pairing_persists_identity_owner_and_position(self):
        api = FakeTelegram([message(text='/start'), message()])
        api.updates[0]['update_id'] = 4
        with patch('run_bot.secrets.token_hex', return_value='a' * 32), \
                patch('run_bot.time.time', return_value=100), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ensure_owner(self.store, api, IDENTITY), 123)
        meta = dict(self.store.db.execute('SELECT key,value FROM meta'))
        self.assertEqual(meta, {'bot_id': '987', 'owner': '123', 'offset': '6'})
        self.assertEqual(self.store.db.execute('SELECT chat FROM settings').fetchone()[0], 123)

    def test_owner_survives_reopening_database_without_pairing_again(self):
        path = self.root / 'plans.sqlite3'
        store = Store(str(path))
        with store.db:
            store.db.executemany('INSERT INTO meta VALUES(?,?)', [('bot_id', '987'), ('owner', '123')])
        store.db.close()
        reopened = Store(str(path))
        try:
            api = FakeTelegram()
            self.assertEqual(ensure_owner(reopened, api, IDENTITY), 123)
            self.assertEqual(api.calls, [])
        finally:
            reopened.db.close()

    def test_another_bot_cannot_reuse_personal_database(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO meta VALUES('bot_id','111')")
        with self.assertRaisesRegex(SetupError, 'другому боту'):
            ensure_owner(self.store, FakeTelegram(), IDENTITY)

    def test_expired_pairing_does_not_bind_or_accept_a_late_update(self):
        for moments in ([0, 601], [0, 1, 601, 601]):
            with self.subTest(moments=moments), patch('run_bot.time.monotonic', side_effect=moments), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(SetupError, 'истекло'):
                    ensure_owner(self.store, FakeTelegram([message(date=int(time.time()))]), IDENTITY)
            self.assertFalse(self.store.db.execute("SELECT 1 FROM meta WHERE key='owner'").fetchone())

    def test_local_lock_blocks_other_process_and_is_released(self):
        lock = self.root / 'bot.lock'
        code = ('import sys\nfrom pathlib import Path\nfrom run_bot import single_instance\n'
                'with single_instance(Path(sys.argv[1])):\n    pass\n')
        with single_instance(lock):
            result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, str(lock)],
                                    cwd=Path(__file__).parent, capture_output=True, text=True, encoding='utf-8')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Бот уже запущен', result.stderr)
        with single_instance(lock):
            pass

    def test_launcher_starts_with_the_saved_owner_and_closes_database(self):
        self.config.write_text('TELEGRAM_BOT_TOKEN=' + FAKE_TOKEN, encoding='utf-8')
        store = Store(str(self.root / 'plans.sqlite3'))
        with store.db:
            store.db.executemany('INSERT INTO meta VALUES(?,?)', [('bot_id', '987'), ('owner', '123')])
        store.db.close()
        api = FakeTelegram()
        with patch('run_bot.ROOT', self.root), patch('bot.Telegram', return_value=api), \
                patch('bot.run') as run, patch.dict(os.environ), contextlib.redirect_stdout(io.StringIO()):
            main()
        planner = run.call_args.args[0]
        self.assertIsInstance(planner, Bot)
        self.assertEqual(planner.owner, 123)
        self.assertEqual([method for method, _ in api.calls], ['getMe', 'getWebhookInfo'])
        with self.assertRaises(sqlite3.ProgrammingError):
            planner.store.db.execute('SELECT 1')

    def test_webhook_is_preserved_and_prevents_polling(self):
        self.config.write_text('TELEGRAM_BOT_TOKEN=' + FAKE_TOKEN, encoding='utf-8')
        api = FakeTelegram()
        original_call = api.call
        api.call = lambda method, **kwargs: ({'url': 'https://example.test/hook'}
                                              if method == 'getWebhookInfo' else original_call(method, **kwargs))
        with patch('run_bot.ROOT', self.root), patch('bot.Telegram', return_value=api), \
                patch.dict(os.environ), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SetupError, 'webhook'):
                main()
        self.assertFalse((self.root / 'plans.sqlite3').exists())

    def test_telegram_error_never_exposes_token_url(self):
        url = 'https://api.telegram.org/bot' + FAKE_TOKEN + '/getMe'
        failure = urllib.error.HTTPError(url, 401, 'Unauthorized', {}, None)
        with patch('telegram_api.urllib.request.urlopen', side_effect=failure):
            with self.assertRaises(TelegramError) as caught:
                Telegram(FAKE_TOKEN).call('getMe')
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))
        self.assertNotIn(url, str(caught.exception))

    def test_polling_processes_update_and_keeps_position(self):
        api = FakeTelegram()
        api.send = lambda *args, **kwargs: None
        handlers = {}

        def get_updates(method, **kwargs):
            self.assertEqual(method, 'getUpdates')
            self.assertEqual(kwargs['offset'], 0)
            handlers['stop']()
            return [message(text='/start')]

        def install_handler(signum, handler):
            handlers['stop'] = handler

        api.call = get_updates
        with patch('bot.signal.signal', side_effect=install_handler):
            run(Bot(self.store, api, 123))
        self.assertEqual(self.store.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()[0], '6')

    def test_polling_stops_on_revoked_token_or_other_instance(self):
        for status in (401, 404, 409):
            with self.subTest(status=status):
                api = FakeTelegram()
                api.call = Mock(side_effect=TelegramError('Telegram HTTP', status))
                with patch('bot.signal.signal'), patch('bot.time.sleep') as sleep:
                    with self.assertRaises(TelegramError):
                        run(Bot(self.store, api, 123))
                sleep.assert_not_called()

    def test_polling_retries_temporary_network_failure(self):
        api = FakeTelegram()
        handlers = {}

        def fail(*args, **kwargs):
            raise TelegramError('Telegram connection failed')

        def install_handler(signum, handler):
            handlers['stop'] = handler

        api.call = fail
        with patch('bot.signal.signal', side_effect=install_handler), \
                patch('bot.time.sleep', side_effect=lambda seconds: handlers['stop']()) as sleep, \
                self.assertLogs(level='WARNING'):
            run(Bot(self.store, api, 123))
        sleep.assert_called_once_with(5)


if __name__ == '__main__':
    unittest.main()
