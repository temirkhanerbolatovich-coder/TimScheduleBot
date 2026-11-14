"""Минимальный клиент Telegram Bot API."""
import json
import urllib.error
import urllib.request

from message_format import message_chunks


class TelegramError(RuntimeError):
    """A sanitized Telegram failure; status codes never contain the token URL."""
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Telegram:
    def __init__(self, token):
        self.url = 'https://api.telegram.org/bot' + token + '/'

    def call(self, method, **params):
        req = urllib.request.Request(self.url + method, json.dumps(params).encode(), {'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=40) as res:
                body = json.load(res)
        except urllib.error.HTTPError as exc:
            # Do not log exception URLs: they include the bot token.
            raise TelegramError(f'Telegram HTTP {exc.code}', exc.code) from None
        except (urllib.error.URLError, TimeoutError):
            raise TelegramError('Telegram connection failed') from None
        if not body.get('ok'):
            raise TelegramError('Telegram rejected request')
        return body['result']

    def send(self, chat, text, keyboard=None, formatted=False):
        chunks = message_chunks(text, formatted)
        messages = []
        for i, chunk in enumerate(chunks):
            params = {'chat_id': chat, 'text': chunk}
            if formatted:
                params['parse_mode'] = 'HTML'
            if keyboard and i == len(chunks) - 1:
                params['reply_markup'] = keyboard if isinstance(keyboard, dict) else {'inline_keyboard': keyboard}
            messages.append(self.call('sendMessage', **params))
        return messages
