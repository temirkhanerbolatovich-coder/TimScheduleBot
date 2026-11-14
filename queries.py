"""Быстрые запросы: ближайшее событие и свободное время."""
import datetime as dt
from html import escape

from formatting import event_details, event_title
from parsing import TZ, WEEKDAYS


def next_text(store, chat, now):
    items = [e for e in store.occurrences(chat, now.date() - dt.timedelta(days=1), 30) if e['end'] > now or (not e['duration'] and e['at'] >= now)]
    if not items:
        return '⏭ Ближайших записей на 30 дней вперёд нет.'
    e = items[0]
    icon, title = event_title(e)
    if e['at'] <= now:
        when = f"Идёт сейчас, до {e['end']:%H:%M}" + (f" ({e['end']:%d.%m})" if e['end'].date() > now.date() else '')
    else:
        delta = e['at'] - now
        hours, minutes = divmod(int(delta.total_seconds()) // 60, 60)
        when = f"{WEEKDAYS[e['at'].weekday()].capitalize()}, {e['at']:%d.%m} в {e['at']:%H:%M} · через " + (f'{hours} ч {minutes} мин' if hours else f'{minutes} мин')
    text = f'⏭ <b>Ближайшее</b>\n\n{icon} <b>{escape(title)}</b>\n🕒 {when}'
    details = event_details(e)
    return text + ('\n' + details if details else '')


def free_text(store, chat, day, min_minutes=60):
    a = dt.datetime.combine(day, dt.time(), TZ)
    b = a + dt.timedelta(days=1)
    busy = sorted((max(e['at'], a), min(e['end'], b)) for e in store.occurrences(chat, day, 1) if e['duration'])
    gaps, cursor = [], a
    for start, end in busy:
        if start - cursor >= dt.timedelta(minutes=min_minutes):
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if b - cursor >= dt.timedelta(minutes=min_minutes):
        gaps.append((cursor, b))
    head = f'🆓 <b>Свободное время</b>\n{WEEKDAYS[day.weekday()].capitalize()}, {day:%d.%m.%Y}\n\n'
    if not gaps:
        return head + 'Свободных промежутков от часа нет.'
    lines = []
    for start, end in gaps:
        hours = (end - start).total_seconds() / 3600
        lines.append(f"{start:%H:%M}–{'24:00' if end == b else end.strftime('%H:%M')} · {hours:g} ч")
    return head + '\n'.join(lines)
