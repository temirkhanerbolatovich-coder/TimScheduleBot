from payroll import shift_end, pay_state
"""Personal Telegram planner. Python 3.11+, standard library only."""
import datetime as dt
from html import escape
import json
import logging
import os
import re
import signal
import time

from formatting import digest, event_title, label
from parsing import TZ, WEEKDAYS, clocks, hm, parse_date, parse_note
from monthview import month_grid, week_summary
from payroll import (MONTH_NAMES, STATUS_NAMES, is_shift, money, month_keyboard, month_list,
                     month_report, parse_month)
from queries import free_text, next_text
from shifts import ROLES, SHIFT_TYPES, build_event, date_keyboard, role_keyboard, type_keyboard
from status import status_text
from store import Store
from telegram_api import Telegram, TelegramError

MENU_COMMANDS = {
    '📅 Сегодня': '/today', '🌅 Завтра': '/tomorrow',
    '🗓 Неделя': '/week', '📋 Мои записи': '/list',
    '➕ Добавить планы': '/add', '⚙️ Сводки': '/settings',
    '❓ Помощь': '/help', '↩️ Отмена': '/cancel',
    '🧩 Смена': '/shift', '⏭ Ближайшее': '/next', '💰 Итоги': '/month',
    '📆 Месяц': '/calendar', '📊 Итог недели': '/summary',
}
MAIN_KEYBOARD = {'keyboard': [[{'text': text} for text in row] for row in (
    ('📅 Сегодня', '🌅 Завтра'), ('🗓 Неделя', '📆 Месяц', '📋 Мои записи'),
    ('🧩 Смена', '⏭ Ближайшее'), ('💰 Итоги', '📊 Итог недели'), ('➕ Добавить планы', '⚙️ Сводки'), ('❓ Помощь', '↩️ Отмена'))],
    'resize_keyboard': True, 'is_persistent': True,
    'input_field_placeholder': 'Выбери действие или отправь планы'}


class Bot:
    def __init__(self, store, api, owner):
        self.store, self.api, self.owner = store, api, owner

    def send(self, chat, text, keyboard=None, formatted=False):
        return self.api.send(chat, text, MAIN_KEYBOARD if keyboard is None else keyboard, formatted=formatted)

    def preview(self, chat, text, action):
        self.store.set_ui_state(chat)
        nonce = self.store.stage(chat, action)
        self.send(chat, text + '\n\n<b>Сохранить изменение?</b>', [[
            {'text': '✅ Сохранить', 'callback_data': 'yes:' + nonce},
            {'text': '↩️ Отмена', 'callback_data': 'no:' + nonce}]], formatted=True)

    def update(self, update):
        cb = update.get('callback_query')
        msg = cb.get('message', {}) if cb else update.get('message', {})
        chat = msg.get('chat', {})
        actor = (cb or msg).get('from', {}).get('id')
        if chat.get('type') != 'private' or actor != self.owner:
            if cb and cb.get('id'):
                self.api.call('answerCallbackQuery', callback_query_id=cb['id'], text='Это личное меню владельца бота.')
            return
        cid = chat['id']
        self.store.register(cid)
        if cb:
            self.api.call('answerCallbackQuery', callback_query_id=cb['id'])
            try:
                self.callback(cid, cb.get('data', ''))
            except (ValueError, IndexError) as exc:
                self.send(cid, f'Не удалось выполнить действие: {exc}. Выбери запись или действие заново.')
            return
        text = (msg.get('text') or msg.get('caption') or '').strip()
        if not text:
            self.send(cid, 'Пока принимаю только текст. Скопируй текст из заметок.')
            return
        text = MENU_COMMANDS.get(text, text)
        # Explicit record IDs make natural edits unambiguous.
        m = re.fullmatch(r'перенеси\s+#?(\d+)\s+на\s+(\d{1,2}\.\d{1,2}\.\d{4})\s+(?:в\s+)?(\d{1,2}:\d{2})[.!]?', text, re.I)
        if m:
            text = '/move ' + ' '.join(m.groups())
        m = re.fullmatch(r'(?:отмени|удали)\s+#?(\d+)[.!]?', text, re.I)
        if m:
            text = '/delete ' + m[1]
        if text.startswith('/'):
            self.store.set_ui_state(cid)
            try:
                self.command(cid, text)
            except (ValueError, IndexError) as exc:
                self.send(cid, f'Не удалось применить команду: {exc}. Формат — /help.')
            return
        if self.store.ui_state(cid):
            try:
                if self.interface_input(cid, text):
                    return
            except (ValueError, IndexError) as exc:
                self.send(cid, f'Проверь введённые данные: {exc}. Попробуй ещё раз или нажми «↩️ Отмена».')
                return
        events, errors = parse_note(text, defaults=self.store.shift_defaults(cid))
        if not events:
            self.send(cid, 'Не нашёл событий.\n' + '\n'.join(errors) + '\nНажми «❓ Помощь», чтобы увидеть пример.')
            return
        report = '\n\n'.join(label(e) for e in events)
        if errors:
            report += '\n\n⚠️ НЕ будут сохранены следующие строки:\n' + escape('\n'.join(errors))
        self.preview(cid, report, {'type': 'save', 'events': events})

    def callback(self, chat, data):
        if not isinstance(data, str) or not 1 <= len(data.encode('utf-8')) <= 64:
            raise ValueError('Некорректная кнопка')
        confirmation = re.fullmatch(r'(yes|no):([a-f0-9]{12})', data)
        if confirmation:
            op, nonce = confirmation.groups()
            if op == 'yes':
                try:
                    ok = self.store.commit(chat, nonce)
                    self.send(chat, '✅ Сохранено.' if ok else 'Этот запрос уже обработан или заменён новой заметкой.')
                except sqlite3.IntegrityError:
                    self.send(chat, 'На новой дате уже есть одноимённая запись. Удали дубликат или измени название.')
            else:
                with self.store.db:
                    self.store.db.execute('DELETE FROM drafts WHERE chat=? AND nonce=?', (chat, nonce))
                self.send(chat, 'Изменение отменено.')
            return
        navigation = re.fullmatch(r'nav:(today|tomorrow|week|list|settings|menu)', data)
        if navigation:
            self.store.set_ui_state(chat)
            return self.command(chat, '/' + navigation[1])
        template = re.fullmatch(r'tpl:(back|t|r|d|o)(?::([a-z0-9]{1,5}))?(?::([ah]))?(?::(\d{4}-\d{2}-\d{2}))?', data)
        if template:
            return self.template_step(chat, *template.groups())
        reprice = re.fullmatch(r'reprice:(\d{1,12})', data)
        if reprice:
            event = self.store.get(chat, int(reprice[1]))
            if not event or not is_shift(event) or event.get('role') not in self.store.rates(chat):
                raise ValueError('Выбери смену с ролью')
            rate = self.store.rates(chat)[event['role']]
            return self.preview(chat, f'Ставка этой смены: {money(event.get("rate_per12", rate))} → {money(rate)} за 12 ч',
                                {'type': 'reprice', 'id': event['id'], 'role': event['role'], 'per12': rate})
        mark = re.fullmatch(r'shift:(w|c|x|a|h):(\d{1,12})', data)
        if mark:
            ident = int(mark[2])
            event = self.store.get(chat, ident)
            if not event or not is_shift(event):
                raise ValueError('Это не смена')
            if mark[1] in ('a', 'h'):
                self.store.update_event(chat, ident, 'Роль смены', role=ROLES[mark[1]])
            else:
                status = {'w': 'worked', 'c': 'covered', 'x': 'extra'}[mark[1]]
                self.store.update_event(chat, ident, 'Отметка смены', status=status)
            return self.show_record(chat, ident)
        if data == 'cal:x':
            return None
        calendar_month = re.fullmatch(r'cal:m:(\d{4})-(\d{2})', data)
        if calendar_month:
            return self.show_calendar(chat, int(calendar_month[1]), int(calendar_month[2]))
        calendar_day = re.fullmatch(r'cal:d:(\d{4}-\d{2}-\d{2})', data)
        if calendar_day:
            day = dt.date.fromisoformat(calendar_day[1])
            self.store.set_ui_state(chat)
            return self.send(chat, digest(self.store, chat, day, 1, WEEKDAYS[day.weekday()].capitalize()),
                             [[{'text': '◀️ К календарю', 'callback_data': f'cal:m:{day:%Y-%m}'}]], formatted=True)
        week = re.fullmatch(r'wk:(\d{4}-\d{2}-\d{2})', data)
        if week:
            return self.show_week_summary(chat, dt.date.fromisoformat(week[1]))
        month = re.fullmatch(r'month:(sum|list):(\d{4})-(\d{2})', data)
        if month:
            return self.show_month(chat, int(month[2]), int(month[3]), month[1] == 'list')
        if data == 'settings:rates':
            return self.show_rates(chat)
        ratecfg = re.fullmatch(r'ratecfg:([ah])', data)
        if ratecfg:
            self.store.set_ui_state(chat, {'command': 'ratecfg', 'role': ROLES[ratecfg[1]]})
            return self.send(chat, f'Отправь ставку роли «{ROLES[ratecfg[1]]}» за 12 часов в тенге, например 12000. '
                             'Оплата считается пропорционально часам смены.')
        if data == 'settings:shifts':
            return self.show_shift_settings(chat)
        shiftcfg = re.fullmatch(r'shiftcfg:(night|day|h18|d6|h24)', data)
        if shiftcfg:
            self.store.set_ui_state(chat, {'command': 'shiftcfg', 'type': shiftcfg[1]})
            return self.send(chat, f'Отправь новое время для смены «{SHIFT_TYPES[shiftcfg[1]][0]}» в формате ЧЧ:ММ-ЧЧ:ММ, например 20:00-08:00. '
                             'Изменение коснётся только новых записей.')
        page = re.fullmatch(r'records:(\d{1,6})', data)
        if page:
            return self.show_records(chat, int(page[1]))
        record = re.fullmatch(r'record:(\d{1,12})', data)
        if record:
            return self.show_record(chat, int(record[1]))
        action = re.fullmatch(r'act:(move|edit|delete|done|skip|override):(\d{1,12})', data)
        if action:
            operation, ident = action[1], int(action[2])
            event = self.store.get(chat, ident)
            if not event:
                raise ValueError('Запись уже удалена')
            if operation in ('delete', 'done'):
                return self.command(chat, f'/{operation} {ident}')
            if operation in ('skip', 'override'):
                if event['date']:
                    raise ValueError('Нужно регулярное занятие')
                return self.choose_occurrence(chat, event, operation)
            if operation == 'move' and not event['date']:
                raise ValueError('Для серии выбери перенос одного занятия')
            self.store.set_ui_state(chat, {'command': '/' + operation, 'id': ident})
            prompt = ('Отправь новую дату и время, например: 15.10.2026 18:00.' if operation == 'move'
                      else 'Отправь новое название, время и описание записи одной заметкой. '
                           'Для регулярного занятия текущий день недели сохранится, если не указать новый.')
            return self.send(chat, prompt)
        occurrence = re.fullmatch(r'occ:(skip|override):(\d{1,12}):(\d{4}-\d{2}-\d{2})', data)
        if occurrence:
            operation, ident = occurrence[1], int(occurrence[2])
            source = dt.date.fromisoformat(occurrence[3])
            event = self.store.get(chat, ident)
            if not event or event['date'] or source.weekday() != event['weekday'] or source.isoformat() < event['since']:
                raise ValueError('На эту дату нет повторения')
            if operation == 'skip':
                return self.command(chat, f'/skip {ident} {source:%d.%m.%Y}')
            self.store.set_ui_state(chat, {'command': '/override', 'id': ident, 'source': source.strftime('%d.%m.%Y')})
            return self.send(chat, f'Переносим занятие за {source:%d.%m}. Отправь новую дату и время: 15.10.2026 18:00.')
        if data in ('settings:pause', 'settings:resume'):
            self.command(chat, '/' + data.split(':')[1])
            return self.show_settings(chat)
        if data == 'settings:daily':
            self.store.set_ui_state(chat, {'command': '/daily'})
            return self.send(chat, 'Выбери время ежедневной сводки или отправь своё в формате ЧЧ:ММ.', [[
                {'text': value, 'callback_data': 'daily:' + value} for value in ('07:00', '07:30', '08:00')]])
        if data == 'settings:weekly':
            self.store.set_ui_state(chat)
            return self.send(chat, 'Выбери день недельной сводки.', [[
                {'text': day.capitalize(), 'callback_data': f'weeklyday:{i}'}] for i, day in enumerate(WEEKDAYS)])
        weekday = re.fullmatch(r'weeklyday:([0-6])', data)
        if weekday:
            day = int(weekday[1])
            self.store.set_ui_state(chat, {'command': '/weekly', 'day': day})
            return self.send(chat, 'Выбери время недельной сводки или отправь своё в формате ЧЧ:ММ.', [[
                {'text': value, 'callback_data': f'weekly:{day}:' + value} for value in ('18:00', '19:00', '20:00')]])
        daily = re.fullmatch(r'daily:((?:[01]\d|2[0-3]):[0-5]\d)', data)
        weekly = re.fullmatch(r'weekly:([0-6]):((?:[01]\d|2[0-3]):[0-5]\d)', data)
        if daily or weekly:
            command = '/daily ' + daily[1] if daily else '/weekly ' + ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'][int(weekly[1])] + ' ' + weekly[2]
            self.command(chat, command)
            self.store.set_ui_state(chat)
            return self.show_settings(chat)
        raise ValueError('Кнопка устарела или не поддерживается')

    def interface_input(self, chat, text):
        state = self.store.ui_state(chat)
        command = state['command']
        if command == 'note':
            return False
        if command == 'tpl':
            day, _ = parse_date(text, dt.datetime.now(TZ).date())
            if day is None:
                raise ValueError('Нужна дата ДД.ММ.ГГГГ')
            self.store.set_ui_state(chat)
            self.preview_template(chat, state['type'], state['role'], day)
            return True
        if command == 'ratecfg':
            digits = re.sub(r'[\s .,]|тг|тенге', '', text, flags=re.I)
            if not digits.isdigit() or not 0 < int(digits) <= 10_000_000:
                raise ValueError('Нужна сумма в тенге числом, например 12000')
            self.preview(chat, f'Ставка «{escape(state["role"])}»: <b>{money(int(digits))}</b> за 12 часов.\n'
                         'Суммы в итогах пересчитаются для всех смен.', {'type': 'rate', 'role': state['role'], 'per12': int(digits)})
            return True
        if command == 'shiftcfg':
            m = re.fullmatch(r'(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})', text)
            if not m:
                raise ValueError('Нужен формат ЧЧ:ММ-ЧЧ:ММ')
            start, end = clocks(m[1])[0][0], clocks(m[2])[0][0]
            duration = (end - start) % 1440 or 1440
            title = SHIFT_TYPES[state['type']][2]
            self.preview(chat, f'Стандартное время «{SHIFT_TYPES[state["type"]][0]}»: <b>{hm(start)}–{hm(end)}</b> ({duration / 60:g} ч).\n'
                         'Старые записи не изменятся.', {'type': 'shiftcfg', 'title': title, 'start': start, 'duration': duration})
            return True
        if command in ('/move', '/override') and not re.fullmatch(r'\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2}', text):
            raise ValueError('Нужны дата и время: ДД.ММ.ГГГГ ЧЧ:ММ')
        if command in ('/daily', '/weekly') and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', text):
            raise ValueError('Нужно время ЧЧ:ММ')
        if 'id' in state and not self.store.get(chat, state['id']):
            self.store.set_ui_state(chat)
            raise ValueError('Запись уже удалена')
        if command == '/edit':
            event = self.store.get(chat, state['id'])
            if event['weekday'] is not None:
                text = WEEKDAYS[event['weekday']] + ':\n' + text
            elif parse_date(text, dt.datetime.now(TZ).date())[0] is None:
                text = dt.date.fromisoformat(event['date']).strftime('%d.%m.%Y') + ' ' + text
        prefix = command
        if 'id' in state:
            prefix += ' ' + str(state['id'])
        if command == '/override':
            prefix += ' ' + state['source']
        if command == '/weekly':
            prefix += ' ' + ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'][state['day']]
        self.command(chat, prefix + ' ' + text)
        self.store.set_ui_state(chat)
        return True

    def template_step(self, chat, step, type_key, role_key, day):
        self.store.set_ui_state(chat)
        if step == 'back' or (step == 't' and type_key not in SHIFT_TYPES):
            return self.send(chat, '🧩 <b>Новая смена</b>\n\nВыбери тип смены.', type_keyboard(), formatted=True)
        if type_key not in SHIFT_TYPES:
            raise ValueError('Неизвестный тип смены')
        if step == 't':
            return self.send(chat, f'🧩 <b>{SHIFT_TYPES[type_key][0]}</b>\n\nВыбери роль.', role_keyboard(type_key), formatted=True)
        if role_key not in ROLES:
            raise ValueError('Выбери роль заново')
        today = dt.datetime.now(TZ).date()
        if step == 'r':
            return self.send(chat, f'🧩 <b>{SHIFT_TYPES[type_key][0]} · {ROLES[role_key]}</b>\n\nВыбери дату.',
                             date_keyboard(type_key, role_key, today), formatted=True)
        if step == 'o':
            self.store.set_ui_state(chat, {'command': 'tpl', 'type': type_key, 'role': role_key})
            return self.send(chat, 'Отправь дату смены, например 25.11.2026.')
        return self.preview_template(chat, type_key, role_key, dt.date.fromisoformat(day))

    def preview_template(self, chat, type_key, role_key, day):
        event = build_event(type_key, role_key, day, self.store.shift_defaults(chat))
        self.preview(chat, label(event), {'type': 'save', 'events': [event]})

    def show_calendar(self, chat, year, month):
        if not 2000 <= year <= 2100 or not 1 <= month <= 12:
            raise ValueError('Месяц вне диапазона')
        self.store.set_ui_state(chat)
        text, keyboard = month_grid(self.store, chat, year, month, dt.datetime.now(TZ).date())
        return self.send(chat, text, keyboard, formatted=True)

    def show_week_summary(self, chat, monday):
        self.store.set_ui_state(chat)
        monday -= dt.timedelta(days=monday.weekday())
        text, keyboard = week_summary(self.store, chat, monday, dt.datetime.now(TZ))
        return self.send(chat, text, keyboard, formatted=True)

    def show_month(self, chat, year, month, as_list=False):
        self.store.set_ui_state(chat)
        now = dt.datetime.now(TZ)
        build = month_list if as_list else month_report
        return self.send(chat, build(self.store, chat, year, month, now),
                         month_keyboard(year, month, 'list' if as_list else 'sum'), formatted=True)

    def show_rates(self, chat):
        self.store.set_ui_state(chat)
        rates = self.store.rates(chat)
        lines = ['💰 <b>Ставки за 12 часов</b>\n'] + [f'{escape(role)}: {money(value)} ({money(value / 12)} в час)' for role, value in rates.items()]
        rows = [[{'text': role, 'callback_data': 'ratecfg:' + key}] for key, role in ROLES.items()]
        return self.send(chat, '\n'.join(lines) + '\n\nВыбери роль, чтобы изменить ставку.', rows, formatted=True)

    def show_shift_settings(self, chat):
        self.store.set_ui_state(chat)
        defaults = self.store.shift_defaults(chat)
        rows, lines = [], ['⏱ <b>Стандартное время смен</b>\n']
        for key, (title, _, name) in SHIFT_TYPES.items():
            start, duration = defaults.get(name, (None, None))
            if start is None:
                lines.append(f'{title}: по умолчанию')
            else:
                lines.append(f'{title}: {hm(start)}–{hm((start + duration) % 1440)}')
            rows.append([{'text': title, 'callback_data': 'shiftcfg:' + key}])
        return self.send(chat, '\n'.join(lines) + '\n\nВыбери смену, чтобы изменить время.', rows, formatted=True)

    def show_menu(self, chat):
        self.store.set_ui_state(chat)
        return self.send(chat, '👋 <b>Твоё расписание</b>\n\nВыбери действие на кнопках внизу. '
                         'В «Моих записях» можно переносить, менять и удалять планы. '
                         'Чтобы добавить расписание, отправь заметку — я покажу её перед сохранением.', formatted=True)

    def show_records(self, chat, page=0):
        self.store.set_ui_state(chat)
        events = self.store.events(chat)
        if not events:
            return self.send(chat, '📋 Записей пока нет. Нажми «➕ Добавить планы» или отправь заметку.')
        size = 8
        page = min(page, (len(events) - 1) // size)
        rows = []
        for event in events[page * size:(page + 1) * size]:
            icon, title = event_title(event)
            when = dt.date.fromisoformat(event['date']).strftime('%d.%m') if event['date'] else WEEKDAYS[event['weekday']][:3]
            rows.append([{'text': f'{icon} {when} · {hm(event["start"])} · {title[:34]}' + (' ✅' if event['done'] else ''),
                          'callback_data': f'record:{event["id"]}'}])
        navigation = []
        if page:
            navigation.append({'text': '◀️ Назад', 'callback_data': f'records:{page - 1}'})
        if (page + 1) * size < len(events):
            navigation.append({'text': 'Дальше ▶️', 'callback_data': f'records:{page + 1}'})
        if navigation:
            rows.append(navigation)
        rows.append([{'text': '🏠 Главное меню', 'callback_data': 'nav:menu'}])
        return self.send(chat, f'📋 <b>Мои записи</b> · страница {page + 1}\n\nВыбери запись кнопкой ниже.', rows, formatted=True)

    def show_record(self, chat, ident):
        self.store.set_ui_state(chat)
        event = self.store.get(chat, ident)
        if not event:
            raise ValueError('Запись уже удалена')
        actions = [('✏️ Изменить', 'edit'), ('🗑 Удалить', 'delete')]
        if event['date']:
            actions.insert(0, ('🕒 Перенести', 'move'))
            if not event['done']:
                actions.append(('✅ Завершить', 'done'))
        else:
            actions = [('🕒 Перенести одно занятие', 'override'), ('🚫 Отменить одно занятие', 'skip'),
                       ('✏️ Изменить всю серию', 'edit'), ('🗑 Удалить всю серию', 'delete')]
        keyboard = [[{'text': title, 'callback_data': f'act:{operation}:{ident}'}] for title, operation in actions]
        if is_shift(event):
            marks = [[{'text': text, 'callback_data': f'shift:{code}:{ident}'}]
                     for code, text in (('w', '✅ Отработал'), ('c', '🔁 Меня заменили'), ('x', '➕ Я заменил'))]
            if not event.get('role'):
                marks.insert(0, [{'text': '🛡 ' + name, 'callback_data': f'shift:{code}:{ident}'} for code, name in (('a', 'Админ'), ('h', 'Помощник'))])
            marks.append([{'text': '💰 Применить текущую ставку', 'callback_data': f'reprice:{ident}'}])
            keyboard[0:0] = marks
        keyboard.append([{'text': '📋 К списку', 'callback_data': 'nav:list'}])
        rate_info = ('\n💰 Ставка: ' + money(event['rate_per12']) + ' за 12 ч') if 'rate_per12' in event else ''
        return self.send(chat, label(event) + rate_info + f'\n\n<i>Запись #{ident}</i>', keyboard, formatted=True)

    def choose_occurrence(self, chat, event, operation):
        self.store.set_ui_state(chat)
        today = dt.datetime.now(TZ).date()
        overrides = {row['day']: json.loads(row['data']) for row in self.store.db.execute(
            'SELECT day,data FROM overrides WHERE chat=? AND event=?', (chat, event['id']))}
        keyboard = []
        for i in range(28):
            day = today + dt.timedelta(days=i)
            if day.weekday() == event['weekday'] and day.isoformat() >= event['since']:
                if day.isoformat() in overrides and overrides[day.isoformat()] is None:
                    continue
                moved = overrides.get(day.isoformat())
                suffix = ' · перенесено на ' + dt.date.fromisoformat(moved['date']).strftime('%d.%m') if moved else ''
                keyboard.append([{'text': f'{WEEKDAYS[day.weekday()].capitalize()}, {day:%d.%m.%Y}',
                                  'callback_data': f'occ:{operation}:{event["id"]}:{day.isoformat()}'}])
                keyboard[-1][0]['text'] += suffix
        keyboard.append([{'text': '📋 К записи', 'callback_data': f'record:{event["id"]}'}])
        return self.send(chat, 'Выбери дату одного занятия. Остальные недели сохранятся.', keyboard)

    def show_settings(self, chat):
        self.store.set_ui_state(chat)
        settings = self.store.db.execute('SELECT * FROM settings WHERE chat=?', (chat,)).fetchone()
        day, time_of_week = settings['weekly'].split()
        status = '⏸ Отключены' if settings['paused'] else '🔔 Включены'
        text = f'⚙️ <b>Сводки</b>\n\n{status}\nЕжедневно: <b>{settings["daily"]}</b>\n'
        text += f'На следующую неделю: <b>{WEEKDAYS[int(day)]}, {time_of_week}</b>\nЧасовой пояс: {escape(TZ.key)}'
        pause = ('🔔 Включить', 'resume') if settings['paused'] else ('⏸ Отключить', 'pause')
        return self.send(chat, text, [[{'text': '🕒 Ежедневная', 'callback_data': 'settings:daily'},
                                      {'text': '🗓 Недельная', 'callback_data': 'settings:weekly'}],
                                     [{'text': '⏱ Время смен', 'callback_data': 'settings:shifts'},
                                      {'text': '💰 Ставки', 'callback_data': 'settings:rates'}],
                                     [{'text': pause[0], 'callback_data': 'settings:' + pause[1]}]], formatted=True)

    def command(self, chat, text):
        parts = text.split()
        cmd = parts[0].split('@')[0].lower()
        today = dt.datetime.now(TZ).date()
        if cmd in ('/start', '/menu'):
            return self.show_menu(chat)
        if cmd == '/shift':
            return self.send(chat, '🧩 <b>Новая смена</b>\n\nВыбери тип смены.', type_keyboard(), formatted=True)
        if cmd in ('/month', '/history'):
            year, month = parse_month(parts[1] if len(parts) > 1 else '', today)
            return self.show_month(chat, year, month, cmd == '/history')
        if cmd == '/calendar':
            year, month = parse_month(parts[1] if len(parts) > 1 else '', today)
            return self.show_calendar(chat, year, month)
        if cmd == '/summary':
            return self.show_week_summary(chat, today)
        if cmd == '/rates':
            return self.show_rates(chat)
        if cmd == '/next':
            return self.send(chat, next_text(self.store, chat, dt.datetime.now(TZ)), formatted=True)
        if cmd == '/free':
            day = today
            if len(parts) > 1:
                day = {'завтра': today + dt.timedelta(days=1), 'сегодня': today}.get(parts[1].lower()) or parse_date(parts[1], today)[0]
                if day is None:
                    raise ValueError('Нужна дата ДД.ММ.ГГГГ, «сегодня» или «завтра»')
            return self.send(chat, free_text(self.store, chat, day), formatted=True)
        if cmd == '/undo':
            last = self.store.last_snapshot(chat)
            if not last:
                return self.send(chat, 'Отменять пока нечего.')
            return self.preview(chat, f'Отменить последнее изменение: <b>{escape(last["label"])}</b> от {last["ts"]}?\nЗаписи вернутся к состоянию до него.', {'type': 'undo'})
        if cmd == '/journal':
            rows = self.store.journal(chat)
            if not rows:
                return self.send(chat, 'Журнал изменений пуст.')
            return self.send(chat, '📜 <b>Последние изменения</b>\n\n' + '\n'.join(f'{r["ts"]} · {escape(r["label"])}' for r in rows) + '\n\n/undo отменяет самое последнее.', formatted=True)
        if cmd == '/status':
            return self.send(chat, status_text(self.store, chat), formatted=True)
        if cmd == '/help':
            return self.send(chat, 'Кнопки «Сегодня», «Завтра» и «Неделя» показывают планы. '
                             '«Мои записи» открывают управление каждой записью, «Сводки» — расписание уведомлений.\n\n'
                             'Добавляй планы обычным текстом, например:\n'
                             '09.10.2026 - Ночь\n\nСреда:\nАнглийский с 14:00 до 17:00, 402А, Преподаватель Г\n\n'
                             'Изменения записей сохраняются только после кнопки «✅ Сохранить». '
                             '«↩️ Отмена» отменяет ввод или черновик. «🧩 Смена» добавляет смену кнопками, «⏭ Ближайшее» показывает следующее событие. «📆 Месяц» (/calendar) — календарь, «📊 Итог недели» (/summary), «💰 Итоги» (/month, /history, /rates) — оплата и смены за месяц, /free — свободное время, /undo — отмена последнего изменения, /journal — журнал, /status — состояние бота. Текстовые команды из README тоже работают.')
        if cmd == '/add':
            self.store.set_ui_state(chat, {'command': 'note'})
            tomorrow = today + dt.timedelta(days=1)
            return self.send(chat, '➕ <b>Добавить планы</b>\n\nОтправь один план или всю заметку. Пример:\n\n'
                             f'<code>{tomorrow:%d.%m.%Y} - Ночь\n\nСреда:\nАнглийский с 14:00 до 17:00, 402А</code>\n\n'
                             'Сначала покажу предпросмотр, затем появится кнопка сохранения.', formatted=True)
        if cmd == '/settings':
            self.store.register(chat)
            return self.show_settings(chat)
        if cmd in ('/today', '/tomorrow', '/week'):
            heading = {'/today': 'Сегодня', '/tomorrow': 'Завтра', '/week': 'Ближайшие 7 дней'}[cmd]
            return self.send(chat, digest(self.store, chat, today + dt.timedelta(days=cmd == '/tomorrow'),
                                         7 if cmd == '/week' else 1, heading), formatted=True)
        if cmd == '/list':
            return self.show_records(chat)
        if cmd == '/cancel':
            with self.store.db:
                self.store.db.execute('DELETE FROM drafts WHERE chat=?', (chat,))
            self.store.set_ui_state(chat)
            return self.send(chat, 'Ввод и ожидающее изменение отменены.')
        if cmd in ('/pause', '/resume'):
            with self.store.db:
                self.store.db.execute('UPDATE settings SET paused=? WHERE chat=?', (int(cmd == '/pause'), chat))
            return self.send(chat, 'Сводки отключены.' if cmd == '/pause' else 'Сводки включены.')
        if cmd in ('/daily', '/weekly'):
            if len(parts) != (2 if cmd == '/daily' else 3):
                raise ValueError('Нужно время, а для недельной сводки также день недели')
            val = parts[-1]
            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', val):
                raise ValueError('Нужно время ЧЧ:ММ')
            if cmd == '/weekly':
                wd = {'пн': 0, 'вт': 1, 'ср': 2, 'чт': 3, 'пт': 4, 'сб': 5, 'вс': 6}.get(parts[1].lower())
                if wd is None:
                    raise ValueError('Нужен день: пн, вт, ср, чт, пт, сб, вс')
                val = f'{wd} {val}'
            column = 'daily' if cmd == '/daily' else 'weekly'
            with self.store.db:
                self.store.db.execute(f'UPDATE settings SET {column}=? WHERE chat=?', (val, chat))
            return self.send(chat, 'Время сводки обновлено: ' + val)
        if cmd not in ('/move', '/edit', '/delete', '/skip', '/override', '/done'):
            raise ValueError('Неизвестная команда')
        ident = int(parts[1].lstrip('#'))
        e = self.store.get(chat, ident)
        if not e:
            raise ValueError('Запись не найдена')
        if cmd == '/delete':
            return self.preview(chat, 'Удалить ' + label(e) + ('\nБудут удалены все повторения и исключения.' if not e['date'] else ''), {'type': 'delete', 'id': ident})
        if cmd == '/done':
            if not e['date']:
                raise ValueError('Для регулярного занятия используй /skip ID ДАТА')
            e['done'] = True
        elif cmd == '/edit':
            parsed, errors = parse_note(text.split(maxsplit=2)[2], today, self.store.shift_defaults(chat))
            if errors or len(parsed) != 1:
                raise ValueError('Нужна ровно одна распознанная запись')
            new = parsed[0]
            if e['weekday'] is not None or new['weekday'] is not None:
                if e['weekday'] is None or new['weekday'] is None:
                    raise ValueError('Нельзя менять регулярную запись на разовую; создай новую')
                new['since'] = e['since']
                count = self.store.db.execute('SELECT COUNT(*) FROM overrides WHERE chat=? AND event=?', (chat, ident)).fetchone()[0]
                if count:
                    raise ValueError('У записи есть исключения. Сначала создай новую серию или удали старую')
            e = new
        elif cmd == '/move':
            if not e['date']:
                raise ValueError('Для одного регулярного занятия используй /override; для всей серии /edit')
            target, _ = parse_date(parts[2], today)
            if target is None:
                raise ValueError('Нужна дата ДД.ММ.ГГГГ')
            e['date'] = target.isoformat()
            ts = clocks(parts[3])
            if len(ts) != 1:
                raise ValueError('Нужно время ЧЧ:ММ')
            e['start'] = ts[0][0]
        elif cmd in ('/skip', '/override'):
            if e['date']:
                raise ValueError('Для разового события используй /delete или /move')
            source, _ = parse_date(parts[2], today)
            if source is None or source.weekday() != e['weekday'] or source.isoformat() < e['since']:
                raise ValueError('На эту дату нет повторения')
            replacement = None
            if cmd == '/override':
                target, _ = parse_date(parts[3], today)
                if target is None:
                    raise ValueError('Нужна новая дата')
                ts = clocks(parts[4])
                if len(ts) != 1:
                    raise ValueError('Нужно время ЧЧ:ММ')
                replacement = dict(e, date=target.isoformat(), weekday=None, start=ts[0][0])
            return self.preview(chat, f"Изменить только занятие «{escape(event_title(e)[1])}» за {source:%d.%m.%Y}:\n" + (label(replacement) if replacement else 'Отменить это занятие'), {'type': 'override', 'id': ident, 'day': source.isoformat(), 'event': replacement})
        self.preview(chat, label(e), {'type': 'edit', 'id': ident, 'event': e})

    def scheduled(self, now):
        # Ask only about recently ended shifts; avoid flooding on migration/restart.
        for settings in self.store.db.execute('SELECT * FROM settings WHERE paused=0').fetchall():
            chat = settings['chat']
            for event in self.store.events(chat):
                if not is_shift(event) or pay_state(event, now) != 'pending':
                    continue
                end = shift_end(event)
                if not dt.timedelta(0) <= now - end <= dt.timedelta(hours=24):
                    continue
                key = (chat, 'shift_end:' + str(event['id']), end.isoformat())
                if self.store.db.execute('SELECT 1 FROM deliveries WHERE chat=? AND kind=? AND day=?', key).fetchone():
                    continue
                self.send(chat, 'Смена закончилась. Подтверди результат или измени часы.\n' + label(event),
                          [[{'text': '✅ Отработал', 'callback_data': f'shift:w:{event["id"]}'},
                            {'text': '🔁 Меня заменили', 'callback_data': f'shift:c:{event["id"]}'}],
                           [{'text': '✏️ Изменить часы', 'callback_data': f'act:edit:{event["id"]}'}]], formatted=True)
                with self.store.db:
                    self.store.db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)', key)
        # Catch up today's summary after downtime, not a flood of historical messages.
        for s in self.store.db.execute('SELECT * FROM settings WHERE paused=0').fetchall():
            weekday, weekly_time = s['weekly'].split()
            jobs = [('daily', s['daily'], now.date(), 1)]
            if now.weekday() == int(weekday):
                next_monday = now.date() + dt.timedelta(days=7 - now.weekday())
                jobs.append(('weekly', weekly_time, next_monday, 7))
            for kind, due, first, days in jobs:
                if now.strftime('%H:%M') < due:
                    continue
                key = (s['chat'], kind, now.date().isoformat())
                if self.store.db.execute('SELECT 1 FROM deliveries WHERE chat=? AND kind=? AND day=?', key).fetchone():
                    continue
                text = digest(self.store, s['chat'], first, days, 'Сегодня' if kind == 'daily' else 'Следующая неделя')
                if kind == 'weekly':
                    text += '\n\n━━━━━━━━━━━━\n\n' + week_summary(self.store, s['chat'], first, now)[0]
                self.send(s['chat'], text, formatted=True)
                with self.store.db:
                    self.store.db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)', key)


def run(bot):
    """Poll Telegram and deliver summaries using an already initialized planner."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    store, api = bot.store, bot.api
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    row = store.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
    offset = int(row['value']) if row else 0
    while running:
        try:
            bot.scheduled(dt.datetime.now(TZ))
            for update in api.call('getUpdates', offset=offset, timeout=20, allowed_updates=['message', 'callback_query']):
                bot.update(update)
                offset = update['update_id'] + 1
                with store.db:
                    store.db.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)", (str(offset),))
        except Exception as exc:
            if isinstance(exc, TelegramError) and exc.status in (401, 404, 409):
                raise
            logging.warning('Operation failed (%s); retry in 5s', type(exc).__name__)
            time.sleep(5)


def main():
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    owner = os.getenv('OWNER_TELEGRAM_ID')
    if not token or not owner:
        raise SystemExit('Set TELEGRAM_BOT_TOKEN and OWNER_TELEGRAM_ID. See README.md.')
    api = Telegram(token)
    # Do not silently remove a pre-existing webhook or erase pending messages.
    if api.call('getWebhookInfo').get('url'):
        raise SystemExit('An active webhook exists. Disable it before using polling.')
    store = Store(os.getenv('BOT_DATABASE', 'plans.sqlite3'))
    try:
        run(Bot(store, api, int(owner)))
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
