"""Load the local token, securely pair the owner once, and run the planner."""
import contextlib
import argparse
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
PAIRING_SECONDS = 600


class SetupError(Exception):
    """A startup failure whose message is safe to show without the bot token."""


def load_config(path):
    """Read a small UTF-8 KEY=VALUE file; never execute or interpolate its values."""
    if not path.is_file():
        raise SetupError('Не найден .env. Скопируй .env.example в .env и вставь токен.')
    if path.stat().st_size > 16384:
        raise SetupError('Файл .env слишком большой. Используй формат из .env.example.')
    config = {}
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not separator or key not in ('TELEGRAM_BOT_TOKEN', 'BOT_TIMEZONE') or key in config:
            raise SetupError(f'Проверь формат строки {number} в .env: нужен KEY=VALUE.')
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        config[key] = value
    return validate_config(config)


def validate_config(config):
    """Validate settings without echoing secrets from a file or the environment."""
    token = config.get('TELEGRAM_BOT_TOKEN', '')
    if len(token) > 256 or not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise SetupError('Вставь полный токен от BotFather вместо PASTE_TOKEN_HERE в .env и сохрани файл.')
    timezone = config.get('BOT_TIMEZONE', 'Asia/Qyzylorda')
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise SetupError('Проверь BOT_TIMEZONE в .env и установку tzdata из requirements.txt.') from None
    return dict(config, BOT_TIMEZONE=timezone)


def load_server_config():
    """Server secrets are supplied by systemd, separately from the source code."""
    config = validate_config({key: os.environ[key] for key in ('TELEGRAM_BOT_TOKEN', 'BOT_TIMEZONE')
                              if key in os.environ})
    state = os.environ.get('BOT_STATE_DIR', '')
    if not state or not Path(state).is_absolute():
        raise SetupError('Для сервера нужен абсолютный BOT_STATE_DIR с постоянным хранилищем.')
    return config, Path(state)


@contextlib.contextmanager
def single_instance(path):
    """Hold an OS file lock; a crashed process releases it automatically."""
    handle = path.open('a+b')
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SetupError('Бот уже запущен из этой папки. Используй открытое окно.') from None
        yield
    finally:
        handle.close()


def matching_owner(update, code, started):
    """Accept a fresh direct private message carrying the local pairing secret."""
    message = update.get('message', {})
    actor = message.get('from', {})
    chat = message.get('chat', {})
    owner = actor.get('id')
    if (chat.get('type') != 'private' or type(owner) is not int or owner <= 0
            or chat.get('id') != owner or actor.get('is_bot')
            or message.get('date', 0) < started
            or message.get('forward_origin') or message.get('via_bot')
            or message.get('business_connection_id')):
        return None
    if not secrets.compare_digest(message.get('text', '').strip().encode('utf-8'),
                                  ('/start ' + code).encode('utf-8')):
        return None
    return owner


def ensure_owner(store, api, identity):
    """Reuse a persisted owner or pair with a random code valid for ten minutes."""
    meta = dict(store.db.execute('SELECT key,value FROM meta'))
    bot_id = str(identity['id'])
    if meta.get('bot_id') not in (None, bot_id):
        raise SetupError('Токен принадлежит другому боту. Для него используй отдельную папку и базу.')
    if meta.get('owner'):
        owner = int(meta['owner'])
        if owner <= 0 or meta.get('bot_id') != bot_id:
            raise SetupError('Повреждена привязка владельца в базе. Сохрани базу и проверь настройку.')
        return owner

    code = 'setup_' + secrets.token_hex(16)
    started = int(time.time())
    deadline = time.monotonic() + PAIRING_SECONDS
    offset = int(meta.get('offset', '0'))
    print('\nПервый запуск: привяжи свой Telegram-аккаунт (10 минут).')
    print('Открой ссылку и нажми «Запустить» / Start:')
    print(f"https://t.me/{identity['username']}?start={code}")
    print('Или отправь своему боту эту команду:')
    print('/start ' + code)
    print('Обычный /start без кода владельца не назначает.\n', flush=True)
    while time.monotonic() < deadline:
        updates = api.call('getUpdates', offset=offset, timeout=20,
                           allowed_updates=['message', 'callback_query'])
        for update in updates:
            if time.monotonic() >= deadline:
                break
            next_offset = update['update_id'] + 1
            owner = matching_owner(update, code, started)
            if owner is not None:
                # Bind identity and the consumed update atomically before normal polling.
                with store.db:
                    store.db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',
                                         [('bot_id', bot_id), ('owner', str(owner)),
                                          ('offset', str(next_offset))])
                    store.db.execute('INSERT OR IGNORE INTO settings(chat) VALUES(?)', (owner,))
                print('Владелец привязан. При следующих запусках код не нужен.', flush=True)
                return owner
            offset = next_offset
            with store.db:
                store.db.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)", (str(offset),))
    raise SetupError('Время привязки истекло. Запусти START_BOT.cmd ещё раз и используй новую ссылку.')


def main(server=False):
    config, state_dir = load_server_config() if server else (load_config(ROOT / '.env'), ROOT)
    # The timezone must be set before importing bot, which initializes ZoneInfo.
    os.environ.update(config)
    from bot import Bot, Store, Telegram, run

    state_dir.mkdir(parents=True, exist_ok=True)
    with single_instance(state_dir / 'bot.lock'):
        api = Telegram(config['TELEGRAM_BOT_TOKEN'])
        identity = api.call('getMe')
        if not identity.get('is_bot') or not identity.get('username'):
            raise SetupError('Telegram не вернул сведения о боте. Проверь токен и повтори запуск.')
        print(f"Подключён бот: {identity['first_name']} (@{identity['username']}).", flush=True)
        if api.call('getWebhookInfo').get('url'):
            raise SetupError('У бота настроен webhook. Отключи его перед запуском через polling.')
        store = Store(str(state_dir / 'plans.sqlite3'))
        try:
            owner = ensure_owner(store, api, identity)
            bot = Bot(store, api, owner)
            print('TimScheduleBot работает. Отправь /start, /today или заметку с планами.')
            if server:
                print('Бот работает на сервере независимо от твоего компьютера.\n', flush=True)
            else:
                print('Оставь окно открытым. Для остановки нажми Ctrl+C.\n', flush=True)
            run(bot)
        finally:
            store.db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='TimScheduleBot')
    parser.add_argument('--server', action='store_true', help='Read secrets from the environment and use BOT_STATE_DIR')
    arguments = parser.parse_args()
    try:
        main(server=arguments.server)
    except KeyboardInterrupt:
        print('\nБот остановлен.')
    except SetupError as exc:
        print('\n' + str(exc))
        raise SystemExit(1)
    except RuntimeError as exc:
        # Telegram.call sanitizes its errors; never show arbitrary exception text or URLs.
        message = str(exc)
        if message in ('Telegram HTTP 401', 'Telegram HTTP 404'):
            print('\nTelegram отклонил токен. Проверь токен в .env.')
        elif message == 'Telegram HTTP 409':
            print('\nЭтот токен уже используется другим процессом. Останови второй экземпляр бота.')
        else:
            print('\nНе удалось подключиться к Telegram. Проверь интернет и повтори запуск.')
        raise SystemExit(1)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f'\nОшибка локальных настроек или базы ({type(exc).__name__}). Проверь папку и .env.')
        raise SystemExit(1)
