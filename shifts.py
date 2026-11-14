"""Шаблоны смен: тип, роль и дата выбираются кнопками."""
import datetime as dt

from parsing import WEEKDAYS, parse_note

# ключ кнопки: (подпись, слово для разбора, название события)
SHIFT_TYPES = {
    'night': ('🌙 Ночь', 'ночь', 'Работа — ночь'),
    'day': ('☀️ День', 'день', 'Работа — день'),
    'h18': ('🕑 18 часов', '18 часов', 'Работа — 18 часов'),
    'd6': ('☀️ 6 день', '6 день', 'Работа — 6 день'),
    'h24': ('🕗 24 часа', '24', 'Работа — 24 часа'),
}
ROLES = {'a': 'Админ', 'h': 'Помощник'}


def type_keyboard():
    keys = list(SHIFT_TYPES)
    rows = [[{'text': SHIFT_TYPES[k][0], 'callback_data': 'tpl:t:' + k} for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
    rows.append([{'text': '🏠 Главное меню', 'callback_data': 'nav:menu'}])
    return rows


def role_keyboard(type_key):
    return [[{'text': '🛡 ' + name, 'callback_data': f'tpl:r:{type_key}:{key}'} for key, name in ROLES.items()],
            [{'text': '◀️ Назад', 'callback_data': 'tpl:back'}]]


def date_keyboard(type_key, role_key, today, days=12):
    buttons = []
    for i in range(days):
        day = today + dt.timedelta(days=i)
        name = ('Сегодня' if i == 0 else 'Завтра' if i == 1 else WEEKDAYS[day.weekday()][:2].capitalize())
        buttons.append({'text': f'{name} {day:%d.%m}', 'callback_data': f'tpl:d:{type_key}:{role_key}:{day.isoformat()}'})
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([{'text': '📅 Другая дата', 'callback_data': f'tpl:o:{type_key}:{role_key}'}])
    rows.append([{'text': '◀️ Назад', 'callback_data': 'tpl:t:' + type_key}])
    return rows


def build_event(type_key, role_key, day, defaults=None):
    if type_key not in SHIFT_TYPES or role_key not in ROLES:
        raise ValueError('Неизвестный шаблон смены')
    word = SHIFT_TYPES[type_key][1]
    events, errors = parse_note(f'{day:%d.%m.%Y} - {word}', day, defaults)
    if errors or len(events) != 1:
        raise ValueError('Не удалось создать смену')
    return dict(events[0], role=ROLES[role_key])
