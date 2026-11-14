"""Split Telegram text without breaking HTML entities, tags or emoji limits."""
from html import escape
from html.parser import HTMLParser


class MessageSplitter(HTMLParser):
    def __init__(self, limit):
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.parts, self.tags, self.current = [], [], []
        self.units = 0

    def flush(self):
        if self.units:
            self.parts.append(''.join(self.current) + ''.join(f'</{tag}>' for tag in reversed(self.tags)))
        self.current = [f'<{tag}>' for tag in self.tags]
        self.units = 0

    def handle_starttag(self, tag, attrs):
        if tag not in ('b', 'i', 'code') or attrs:
            raise ValueError('Unsupported generated HTML')
        self.tags.append(tag)
        self.current.append(f'<{tag}>')

    def handle_endtag(self, tag):
        if not self.tags or self.tags[-1] != tag:
            raise ValueError('Unbalanced generated HTML')
        self.tags.pop()
        self.current.append(f'</{tag}>')

    def handle_data(self, data):
        for char in data:
            size = 2 if ord(char) > 0xffff else 1
            if self.units + size > self.limit:
                self.flush()
            self.current.append(escape(char, quote=False))
            self.units += size


def message_chunks(text, formatted=False, limit=3500):
    """Return valid independent messages below Telegram's UTF-16 text budget."""
    if limit < 2:
        raise ValueError('Message budget must allow a Unicode character')
    if formatted:
        parser = MessageSplitter(limit)
        parser.feed(text)
        parser.close()
        if parser.tags:
            raise ValueError('Unclosed generated HTML')
        parser.flush()
        return parser.parts or ['—']
    parts, current, units = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xffff else 1
        if units + size > limit:
            parts.append(''.join(current))
            current, units = [], 0
        current.append(char)
        units += size
    return parts + ([''.join(current)] if current else []) or ['—']
