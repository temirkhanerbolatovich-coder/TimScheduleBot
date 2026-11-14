"""Разбор текстовых заметок в события."""
import datetime as dt
import os
import re
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv('BOT_TIMEZONE', 'Asia/Qyzylorda'))
WEEKDAYS = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']
MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']
CLOCK = r'(?<!\d)([0-2]?\d):([0-5]\d)(?!\d)'


def parse_date(text, today):
    m = re.search(r'(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)', text)
    if m:
        return dt.date(int(m[3]), int(m[2]), int(m[1])), m.span()
    m = re.search(r'(?<!\d)(\d{1,2})(?:-[еe])?\s+(' + '|'.join(MONTHS) + r')(?:\s+(\d{4}))?', text, re.I)
    if m:
        return dt.date(int(m[3] or today.year), MONTHS.index(m[2].lower()) + 1, int(m[1])), m.span()
    return None, None


def clocks(text):
    values = []
    for m in re.finditer(CLOCK, text):
        hour = int(m[1])
        if hour > 23:
            raise ValueError('Час должен быть от 00 до 23')
        values.append((hour * 60 + int(m[2]), m.span()))
    if len(values) > 2:
        raise ValueError('Больше двух значений времени в строке')
    return values


def hm(minutes):
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def event_line(line, weekday, today, defaults=None):
    day, span = parse_date(line, today)
    if not day and weekday is None:
        raise ValueError('Нужна дата или заголовок дня недели')
    body = (line[:span[0]] + line[span[1]:]) if span else line
    body = re.sub(r'^г\.?', '', body.strip(), flags=re.I)
    body = re.sub(r'\((?:' + '|'.join(WEEKDAYS) + r')\)', '', body, flags=re.I)
    role = re.search(r'\(\s*(админ|пом(?:ощник)?)\.?\s*\)', body, re.I)
    if role:
        body = body[:role.start()] + ' ' + body[role.end():]
    body = re.sub(r'^\s*\d+[.)]\s*', '', body).strip(' ,.-–—')
    ts = clocks(body)
    low = body.lower()
    shift = None
    for pattern, label, start, duration in [
        (r'\b6\s+день\b', 'Работа — 6 день', 480, 360),
        (r'\b18\s*ч(?:ас(?:ов|а)?)?(?![а-яё])', 'Работа — 18 часов', 840, 1080),
        (r'^24(?:\s|$)', 'Работа — 24 часа', 480, 1440),
        (r'\bночь\b', 'Работа — ночь', 1200, 720),
        (r'\bдень\b', 'Работа — день', 480, 720),
    ]:
        if re.search(pattern, low):
            start, duration = (defaults or {}).get(label, (start, duration))
            shift = label, start, duration
            break
    if shift:
        title, start, duration = shift
        if ts:
            start = ts[0][0]
        # «День 8ч.» — смена обычного типа, но укороченная до указанных часов.
        short = re.search(r'(?<![\d:])(\d{1,2})\s*ч(?:ас(?:ов|а)?)?(?![а-яё])', body, re.I)
        if short and len(ts) < 2 and title in ('Работа — день', 'Работа — ночь'):
            if not 1 <= int(short[1]) <= 24:
                raise ValueError('Часов смены должно быть от 1 до 24')
            duration = int(short[1]) * 60
        if len(ts) == 2:
            duration = (ts[1][0] - start) % 1440 or 1440
        details = body
    else:
        if not ts:
            raise ValueError('Укажи время; задачи без времени пока не поддерживаются')
        start = ts[0][0]
        duration = (ts[1][0] - start) % 1440 if len(ts) == 2 else 0
        if len(ts) == 2 and (duration == 0 or ts[1][0] < start):
            raise ValueError('Для занятия конец должен быть позже начала')
        first, last = ts[0][1][0], ts[-1][1][1]
        title = re.sub(r'\s+(?:с|в)$', '', body[:first].strip(' ,.-–—'), flags=re.I)
        details = body[last:].strip(' ,.-–—')
        if not title:
            title, _, details = details.partition(',')
            title = title.strip()
            details = details.strip()
        if not title:
            raise ValueError('Не найдено название события')
    event = {'date': day.isoformat() if day else None, 'weekday': None if day else weekday,
             'title': title, 'start': start, 'duration': duration, 'details': details,
             'since': today.isoformat(), 'done': False}
    if role and shift:
        event['role'] = 'Админ' if role[1].lower() == 'админ' else 'Помощник'
    return event


def parse_note(text, today=None, defaults=None):
    today = today or dt.datetime.now(TZ).date()
    events, errors, weekday = [], [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        head = line.rstrip(':').lower()
        if head in WEEKDAYS:
            weekday = WEEKDAYS.index(head)
            continue
        try:
            events.append(event_line(line, weekday, today, defaults))
        except (ValueError, OverflowError) as exc:
            errors.append(f'{line}: {exc}')
    return events, errors
