"""Run before the main bot; prints private chat user IDs, never the token."""
import os
from bot import Telegram

token = os.getenv('TELEGRAM_BOT_TOKEN')
if not token:
    raise SystemExit('Set TELEGRAM_BOT_TOKEN first.')
api = Telegram(token)
if api.call('getWebhookInfo').get('url'):
    raise SystemExit('An active webhook exists; use a new bot or disable the webhook.')
print('Send /start to your bot now. Press Ctrl+C after your own user ID appears.')
offset = 0
while True:
    for update in api.call('getUpdates', offset=offset, timeout=20, allowed_updates=['message']):
        offset = update['update_id'] + 1
        msg = update.get('message', {})
        if msg.get('chat', {}).get('type') == 'private':
            actor = msg.get('from', {})
            print('User ID:', actor.get('id'), 'username:', actor.get('username', '(none)'))
