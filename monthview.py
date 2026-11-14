"""Календарь месяца сеткой кнопок и итог недели."""
import calendar
import datetime as dt
from html import escape

from formatting import event_title
from parsing import TZ
from payroll import SHORT_DAYS, MONTH_NAMES, is_shift, money, shift_month, shift_pay

SHIFT_MARKS = {'Работа — ночь': '🌙', 'Работа — день': '☀️', 'Работа — 18 часов': '🕑',
               'Работа — 6 день': '🌤', 'Работа — 24 часа': '🕗'}


def day_marks(store, chat, year, month):
    """День месяца -> значок: смена важнее обычных событий."""
    first = dt.date(year, month, 1)
    days = calendar.monthrange(year, month)[1]
    marks = {}
    for e in store.occurrences(chat, first, days):
        day = e['at'].date()
        if day.year != year or day.month != month:
            continue
        mark = SHIFT_MARKS.get(e['title'])
        if mark:
            marks[day.day] = mark
        else:
            marks.setdefault(day.day, '•')
    return marks


def month_grid(store, chat, year, month, today):
    marks = day_marks(store, chat, year, month)
    py, pm = shift_month(year, month, -1)
    ny, nm = shift_month(year, month, 1)
    rows = [[{'text': '◀️', 'callback_data': f'cal:m:{py:04d}-{pm:02d}'},
             {'text': f'{MONTH_NAMES[month - 1].capitalize()} {year}', 'callback_data': 'cal:x'},
             {'text': '▶️', 'callback_data': f'cal:m:{ny:04d}-{nm:02d}'}],
            [{'text': d, 'callback_data': 'cal:x'} for d in SHORT_DAYS]]
    for week in calendar.Calendar(0).monthdayscalendar(year, month):
        row = []
        for number in week:
            if not number:
                row.append({'text': ' ', 'callback_data': 'cal:x'})
                continue
            text = f'{number}{marks.get(number, "")}'
            if dt.date(year, month, number) == today:
                text = f'[{text}]'
            row.append({'text': text, 'callback_data': f'cal:d:{year:04d}-{month:02d}-{number:02d}'})
        rows.append(row)
    rows.append([{'text': '📊 Итог недели', 'callback_data': f'wk:{today - dt.timedelta(days=today.weekday())}'},
                 {'text': '💰 Итоги', 'callback_data': f'month:sum:{year:04d}-{month:02d}'}])
    text = (f'📆 <b>{MONTH_NAMES[month - 1].capitalize()} {year}</b>\n\n'
            '🌙 ночь · ☀️ день · 🕑 18 ч · 🌤 6 день · 🕗 24 ч · • другие события · [ ] сегодня\n'
            'Нажми на число, чтобы открыть день.')
    return text, rows


def week_summary(store, chat, monday, now):
    rates = store.rates(chat)
    sunday = monday + dt.timedelta(days=6)
    items = store.occurrences(chat, monday, 7)
    shifts = [e for e in store.events(chat) if is_shift(e) and monday.isoformat() <= e['date'] <= sunday.isoformat()]
    counted = [e for e in shifts if e.get('status') != 'covered']
    hours = sum(e['duration'] for e in counted) / 60
    nights = sum(1 for e in counted if e['title'] == 'Работа — ночь')
    pay = sum(shift_pay(e, rates) for e in counted)
    unassigned = sum(1 for e in counted if e.get('role') not in rates)
    lines = [f'📊 <b>Итог недели</b>\n{monday:%d.%m} — {sunday:%d.%m.%Y}\n']
    if counted:
        lines.append(f'Смен: {len(counted)} · {hours:g} ч · ночных: {nights}')
        lines.append(f'💵 Заработок: <b>{money(pay)}</b>')
        if unassigned:
            lines.append(f'⚠️ Без роли: {unassigned} — не посчитаны')
    else:
        lines.append('Смен на этой неделе нет.')
    free, day_lines = [], []
    for i in range(7):
        day = monday + dt.timedelta(days=i)
        a = dt.datetime.combine(day, dt.time(), TZ)
        b = a + dt.timedelta(days=1)
        busy = [e for e in items if e['at'] < b and (e['end'] > a or (not e['duration'] and e['at'] >= a))]
        if not busy:
            free.append(f'{SHORT_DAYS[i]} {day:%d.%m}')
        starts = [e for e in items if e['at'].date() == day]
        if starts:
            names = '; '.join(f'{event_title(e)[0]} {escape(event_title(e)[1])}' for e in starts[:4])
            more = f' и ещё {len(starts) - 4}' if len(starts) > 4 else ''
            day_lines.append(f'<b>{SHORT_DAYS[i]} {day:%d.%m}</b> — {names}{more}')
    lines.append('\n🆓 Свободные дни: ' + (', '.join(free) if free else 'нет'))
    if day_lines:
        lines.append('\n' + '\n'.join(day_lines))
    prev, nxt = monday - dt.timedelta(days=7), monday + dt.timedelta(days=7)
    keyboard = [[{'text': '◀️ Неделя', 'callback_data': f'wk:{prev}'}, {'text': 'Неделя ▶️', 'callback_data': f'wk:{nxt}'}],
                [{'text': '📆 Месяц', 'callback_data': f'cal:m:{monday:%Y-%m}'}]]
    return '\n'.join(lines), keyboard
