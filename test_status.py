import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import Store
from status import duration, status_text


class StatusTests(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(duration(59), '0 мин')
        self.assertEqual(duration(3 * 3600 + 120), '3 ч 2 мин')
        self.assertEqual(duration(86400 + 60), '1 д 0 ч 1 мин')

    def test_status_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / 'plans.sqlite3'))
            try:
                text = status_text(store, 1, tmp)
                self.assertIn('Версия:', text)
                self.assertIn('Записей: 0', text)
                self.assertIn('Последняя сводка: ещё не было', text)
                self.assertIn('Последняя копия базы: нет', text)
                (Path(tmp) / 'backups').mkdir()
                (Path(tmp) / 'backups' / 'plans-20261114-000000.sqlite3').write_text('x')
                self.assertRegex(status_text(store, 1, tmp), r'Последняя копия базы: \d\d\.\d\d\.\d{4}')
            finally:
                store.db.close()


class AlertTests(unittest.TestCase):
    def test_alert_sends_to_owner(self):
        import alert
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / 'plans.sqlite3'))
            with store.db:
                store.db.execute("INSERT INTO meta VALUES('owner','42')")
            store.db.close()
            env = {'TELEGRAM_BOT_TOKEN': 'T', 'BOT_STATE_DIR': tmp}
            with patch.dict('os.environ', env), patch('sys.argv', ['alert.py', 'x.service']), \
                    patch('alert.urllib.request.urlopen') as opened:
                alert.main()
            request = opened.call_args[0][0]
            self.assertIn(b'"chat_id": 42', request.data)
            self.assertIn('x.service'.encode(), request.data)


if __name__ == '__main__':
    unittest.main()
