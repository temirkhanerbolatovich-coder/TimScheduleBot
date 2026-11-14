"""Оплата смен, отметки и итоги месяца."""
import datetime as dt
from html import escape

from formatting import SHIFT_LABELS
from parsing import TZ

# Ставка за 12 часов, тенге.
DEFAULT_RATES = {'Админ': 12000, 'Помощник': 10000}
STATUS_MARKS = {'worked': '✅', 'covered': '🔁', 'extra': '➕'}
STATUS_NAMES = {'worked': 'отработана', 'covered': 'меня заменили', 'extra': 'я заменил'}
SHORT_DAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
MONTH_NAMES = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь']
SHORT_TYPE = {'Работа — ночь': 'Ночь', 'Работа — день': 'День', 'Работа — 18 часов': '18 часов',
              'Работа — 6 день': '6 день', 'Работа — 24 часа': '24 часа'}


def money(value):
    return f'{round(value):,}'.replace(',', ' ') + ' тг'


def is_shift(event):
    return event['title'] in SHIFT_LABELS and event['date'] is not None


def shift_pay(event, rates):
    """Оплата пропорциональна часам: ставка за 12 часов делится на 12."""
    role = event.get('role')
    if event.get('status') == 'covered' or role not in rates:
        return 0
    return event['duration'] / 60 * event.get('rate_per12', rates[role]) / 12


def pay_state(event, now):
    if event.get('status') == 'covered':
        return 'excluded'
    if event.get('status') in ('worked', 'extra'):
        return 'confirmed'
    return 'pending' if shift_end(event) <= now else 'planned'


def pay_breakdown(events, rates, now):
    totals = dict(confirmed=0.0, pending=0.0, planned=0.0)
    for event in events:
        state = pay_state(event, now)
        if state in totals:
            totals[state] += shift_pay(event, rates)
    return ('Подтверждено: ' + money(totals['confirmed']) +
            '\nОжидает отметки: ' + money(totals['pending']) +
            '\nЗапланировано: ' + money(totals['planned']))


def month_shifts(store, chat, year, month):
    prefix = f'{year:04d}-{month:02d}-'
    return sorted((e for e in store.events(chat) if is_shift(e) and e['date'].startswith(prefix)),
                  key=lambda e: (e['date'], e['start']))


def shift_end(event):
    start = dt.datetime.combine(dt.date.fromisoformat(event['date']), dt.time(), TZ) + dt.timedelta(minutes=event['start'])
    return start + dt.timedelta(minutes=event['duration'])


def parse_month(text, today):
    """«11.2026», «2026-11» или пусто (текущий месяц)."""
    text = (text or '').strip()
    if not text:
        return today.year, today.month
    for sep, order in (('.', 'my'), ('-', 'ym')):
        parts = text.split(sep)
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            month, year = (int(parts[0]), int(parts[1])) if order == 'my' else (int(parts[1]), int(parts[0]))
            if 1 <= month <= 12 and 2000 <= year <= 2100:
                return year, month
    raise ValueError('Нужен месяц в формате ММ.ГГГГ, например 11.2026')


def shift_month(year, month, delta):
    index = year * 12 + month - 1 + delta
    return index // 12, index % 12 + 1


def month_title(year, month):
    return f'{MONTH_NAMES[month - 1]} {year}'


def month_keyboard(year, month, current='sum'):
    py, pm = shift_month(year, month, -1)
    ny, nm = shift_month(year, month, 1)
    other = ('list', '📋 Список смен') if current == 'sum' else ('sum', '💰 Итоги')
    return [[{'text': '◀️ ' + MONTH_NAMES[pm - 1], 'callback_data': f'month:{current}:{py:04d}-{pm:02d}'},
             {'text': MONTH_NAMES[nm - 1] + ' ▶️', 'callback_data': f'month:{current}:{ny:04d}-{nm:02d}'}],
            [{'text': other[1], 'callback_data': f'month:{other[0]}:{year:04d}-{month:02d}'}]]


def month_report(store, chat, year, month, now):
    rates = store.rates(chat)
    shifts = month_shifts(store, chat, year, month)
    head = f'💰 <b>Итоги · {month_title(year, month)}</b>'
    if not shifts:
        return head + '\n\nСмен в этом месяце нет.'
    by_role, by_type = {}, {}
    unassigned = covered = extra = 0
    for e in shifts:
        status = e.get('status')
        if status == 'covered':
            covered += 1
            continue
        extra += status == 'extra'
        hours = e['duration'] / 60
        short = SHORT_TYPE[e['title']]
        by_type[short] = by_type.get(short, 0) + 1
        role = e.get('role')
        if role not in rates:
            unassigned += 1
            continue
        pay = shift_pay(e, rates)
        item = by_role.setdefault(role, [0, 0.0, 0.0])
        item[0] += 1
        item[1] += hours
        item[2] += pay
    lines = [head, '']
    total = 0.0
    for role in rates:
        if role in by_role:
            count, hours, pay = by_role[role]
            total += pay
            lines.append(f'🛡 <b>{escape(role)}</b>: {count} · {hours:g} ч · {money(pay)}')
    lines.append(f'\n💵 <b>Итого: {money(total)}</b>')
    lines.append(pay_breakdown(shifts, rates, now))
    lines.append('\nПо типам: ' + ', '.join(f'{name} — {count}' for name, count in by_type.items()))
    if extra:
        lines.append(f'➕ Замены (я заменил): {extra}')
    if covered:
        lines.append(f'🔁 Меня заменили: {covered} (не оплачиваются)')
    if unassigned:
        lines.append(f'⚠️ Без роли: {unassigned} — не посчитаны. Открой смену в «Мои записи» и выбери роль.')
    return '\n'.join(lines)


def month_list(store, chat, year, month, now):
    rates = store.rates(chat)
    shifts = month_shifts(store, chat, year, month)
    head = f'📋 <b>Смены · {month_title(year, month)}</b>'
    if not shifts:
        return head + '\n\nСмен в этом месяце нет.'
    lines = [head, '']
    for e in shifts:
        day = dt.date.fromisoformat(e['date'])
        status = e.get('status')
        mark = STATUS_MARKS.get(status) or ('❔' if shift_end(e) <= now else '⏳')
        pay = shift_pay(e, rates)
        role = e.get('role') or 'без роли'
        lines.append(f'{mark} {day:%d.%m} {SHORT_DAYS[day.weekday()]} · {SHORT_TYPE[e["title"]]} · {escape(role)} · '
                     f'{e["duration"] / 60:g} ч · ' + (money(pay) if pay else '—'))
    lines.append('\n✅ подтверждено · ❔ ожидает отметки · ⏳ запланировано · 🔁 меня заменили · ➕ я заменил')
    return '\n'.join(lines)
