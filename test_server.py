import contextlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from backup_database import backup_database
from bot import Store, parse_note
from run_bot import SetupError, load_server_config, main
from test_setup import FAKE_TOKEN, FakeTelegram


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.env = {'TELEGRAM_BOT_TOKEN': FAKE_TOKEN, 'BOT_STATE_DIR': str(self.state)}

    def tearDown(self):
        self.folder.cleanup()

    def test_server_requires_token_and_absolute_persistent_directory(self):
        invalid = [{}, {'TELEGRAM_BOT_TOKEN': FAKE_TOKEN},
                   {'TELEGRAM_BOT_TOKEN': FAKE_TOKEN, 'BOT_STATE_DIR': 'relative'}]
        for config in invalid:
            with self.subTest(keys=tuple(config)), patch.dict(os.environ, config, clear=True):
                with self.assertRaises(SetupError):
                    load_server_config()
        with patch.dict(os.environ, self.env, clear=True):
            config, state = load_server_config()
        self.assertEqual(state, self.state)
        self.assertEqual(config['BOT_TIMEZONE'], 'Asia/Qyzylorda')

    def test_server_uses_persistent_state_without_local_secret_file(self):
        store = Store(str(self.state / 'plans.sqlite3'))
        with store.db:
            store.db.executemany('INSERT INTO meta VALUES(?,?)', [('bot_id', '987'), ('owner', '123')])
        store.db.close()
        with patch('run_bot.ROOT', self.root), patch.dict(os.environ, self.env, clear=True), \
                patch('bot.Telegram', return_value=FakeTelegram()), patch('bot.run') as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            main(server=True)
        self.assertEqual(run.call_args.args[0].owner, 123)
        self.assertTrue((self.state / 'bot.lock').is_file())
        self.assertFalse((self.root / '.env').exists())
        self.assertFalse((self.root / 'plans.sqlite3').exists())
        self.assertIn('независимо от твоего компьютера', output.getvalue())
        self.assertNotIn(FAKE_TOKEN, output.getvalue())

    def test_backup_restores_plans_owner_and_pending_draft(self):
        store = Store(str(self.state / 'plans.sqlite3'))
        events, errors = parse_note('09.10.2026 10:00 Встреча')
        self.assertFalse(errors)
        with store.db:
            store.save(123, events[0])
            store.db.execute("INSERT INTO meta VALUES('owner','123')")
        nonce = store.stage(123, {'type': 'delete', 'id': 1})
        try:
            destination = backup_database(self.state)
            restored = Store(str(destination))
            try:
                self.assertEqual(restored.events(123)[0]['title'], 'Встреча')
                self.assertEqual(restored.db.execute("SELECT value FROM meta WHERE key='owner'").fetchone()[0], '123')
                self.assertEqual(restored.db.execute('SELECT nonce FROM drafts').fetchone()[0], nonce)
                self.assertEqual(restored.db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            finally:
                restored.db.close()
        finally:
            store.db.close()

    def test_retention_keeps_unrelated_files_and_recent_backups(self):
        store = Store(str(self.state / 'plans.sqlite3'))
        store.db.close()
        folder = self.state / 'backups'
        folder.mkdir()
        old = folder / 'plans-20200101-000000.sqlite3'
        unrelated = folder / 'my-plans.sqlite3'
        recent = folder / 'plans-20261008-000000.sqlite3'
        for path in (old, unrelated, recent):
            path.write_text('test', encoding='utf-8')
        expired = time.time() - 15 * 86400
        os.utime(old, (expired, expired))
        os.utime(unrelated, (expired, expired))
        backup_database(self.state)
        self.assertFalse(old.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(recent.exists())

    def test_missing_database_is_not_created_by_backup(self):
        with self.assertRaises(FileNotFoundError):
            backup_database(self.state)
        self.assertFalse((self.state / 'plans.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
