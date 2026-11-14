"""Оформление карточек и сводок (HTML для Telegram)."""
import datetime as dt
from html import escape
import re

from parsing import TZ, WEEKDAYS, hm

SHIFT_LABELS = {
    'Работа — ночь': ('🌙', 'Ночная смена'),
    'Работа — день': ('☀️', 'Дневная смена'),
    'Работа — 18 часов': ('🕑', 'Смена на 18 часов'),
    'Работа — 6 день': ('☀️', 'Дневная смена · 6 часов'),
    'Работа — 24 часа': ('🕗', 'Суточная смена'),
}


def event_title(event):
    icon, title = SHIFT_LABELS.get(event['title'], ('📚' if event['weekday'] is not None else '📌', event['title']))
    expected = {'Работа — 18 часов': 1080, 'Работа — 6 день': 360, 'Работа — 24 часа': 1440}
    if event['title'] in expected and event['duration'] != expected[event['title']]:
        title = f"Рабочая смена · {event['duration'] / 60:g} ч"
    if event.get('role') and event['title'] in SHIFT_LABELS:
        title += ' · ' + event['role']
    return icon, title


def event_details(event):
    details = event['details'].strip()
    if event['title'] in SHIFT_LABELS:
        # The shift type and its hours are already shown prominently on the card.
        details = re.sub(r'^(?:6\s+день|18\s*ч(?:ас(?:ов|а)?)?\.?|24|ночь|день)(?![а-яё])', '', details, flags=re.I)
        details = re.sub(r'^\s*\d{1,2}\s*ч(?:ас(?:ов|а)?)?\.?(?![а-яё])', '', details, flags=re.I)
        details = re.sub(r'\b(?:с|в|до)\s+\d{1,2}:\d{2}', '', details, flags=re.I).strip(' ,.-–—()')
    room = re.match(r'^(?:каб(?:инет)?\.?\s*)?(\d{2,4}[А-Яа-яA-Za-z]?)(?:,\s*(.*))?$', details)
    if room:
        return '📍 Кабинет ' + escape(room[1]) + ('\n👤 ' + escape(room[2]) if room[2] else '')
    return 'ℹ️ ' + escape(details) if details else ''


def label(event):
    icon, title = event_title(event)
    when = (dt.date.fromisoformat(event['date']).strftime('%d.%m.%Y') if event['date']
            else 'Еженедельно: ' + WEEKDAYS[event['weekday']])
    end = event['start'] + event['duration']
    hours = hm(event['start'])
    if event['duration']:
        hours += '–' + hm(end % 1440)
        if end >= 1440:
            hours += ' · окончание на следующий день'
    lines = [f'{icon} <b>{escape(title)}</b>', '🗓 ' + when, '🕒 ' + hours]
    details = event_details(event)
    if details:
        lines.append(details)
    if event.get('status') == 'covered':
        lines.append('🔁 Меня заменили')
    elif event.get('status') == 'extra':
        lines.append('➕ Я заменил')
    if event.get('done'):
        lines.append('✅ Завершено')
    return '\n'.join(lines)


def digest(store, chat, first, days=1, heading=None):
    items = store.occurrences(chat, first, days)
    heading = heading or ('Планы на день' if days == 1 else 'Ближайшие 7 дней')
    period = first.strftime('%d.%m.%Y')
    if days > 1:
        period += ' — ' + (first + dt.timedelta(days=days - 1)).strftime('%d.%m.%Y')
    blocks = [f'📅 <b>{escape(heading)}</b>\n{period}']
    for i in range(days):
        day = first + dt.timedelta(days=i)
        a = dt.datetime.combine(day, dt.time(), TZ)
        b = a + dt.timedelta(days=1)
        relevant = [e for e in items if e['at'] < b and (e['end'] > a or (not e['duration'] and e['at'] >= a))]
        lines = [f'<b>{WEEKDAYS[day.weekday()].capitalize()}, {day:%d.%m}</b>']
        if not relevant:
            lines.append('Записанных планов нет.')
        for e in relevant:
            icon, title = event_title(e)
            when = e['at'].strftime('%H:%M')
            if e['at'] < a:
                title = 'Продолжение: ' + title.lower()
                when = 'До ' + e['end'].strftime('%H:%M') + f" · началось {e['at']:%d.%m} в {e['at']:%H:%M}"
            elif e['duration']:
                when += '–' + e['end'].strftime('%H:%M')
                if e['end'].date() > day:
                    when += f" · до {e['end']:%d.%m}"
            card = f'{icon} <b>{escape(title)}</b>\n🕒 {when}'
            details = event_details(e)
            if details:
                card += '\n' + details
            lines.append(card)
        for ix, x in enumerate(relevant):
            for y in relevant[ix + 1:]:
                if x['duration'] and y['duration'] and max(x['at'], y['at'], a) < min(x['end'], y['end'], b):
                    start, end = max(x['at'], y['at'], a), min(x['end'], y['end'], b)
                    titles = escape(event_title(x)[1]) + ' ↔ ' + escape(event_title(y)[1])
                    lines.append(f'⚠️ <b>Пересечение {start:%H:%M}–{end:%H:%M}</b>\n{titles}')
        blocks.append('\n\n'.join(lines))
    return '\n\n━━━━━━━━━━━━\n\n'.join(blocks)
