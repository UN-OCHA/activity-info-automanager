"""Underline substitutions within NiceGUI's existing syntax-highlighted JSON."""

from html import escape
from html.parser import HTMLParser
import json

from .schema_names import SubstitutedText


def substitution_ranges(schema, error_paths=None):
    """Offsets in json.dumps(indent=2, ensure_ascii=False), including JSON escapes."""
    ranges = []
    offset = 0

    def emit(text):
        nonlocal offset
        offset += len(text)

    def visit(value, depth=0, path=()):
        begin = offset
        if isinstance(value, (dict, list)):
            mapping = isinstance(value, dict)
            emit('{' if mapping else '[')
            items = list(value.items()) if mapping else list(enumerate(value))
            for index, (key, item) in enumerate(items):
                emit((',' if index else '') + '\n' + '  ' * (depth + 1))
                if mapping:
                    emit(json.dumps(key, ensure_ascii=False) + ': ')
                visit(item, depth + 1, path + (key,))
            if items:
                emit('\n' + '  ' * depth)
            emit('}' if mapping else ']')
        else:
            if error_paths is None and isinstance(value, SubstitutedText):
                for start, end, _ in value.spans:
                    begin = offset + 1 + len(json.dumps(value[:start], ensure_ascii=False)[1:-1])
                    stop = offset + 1 + len(json.dumps(value[:end], ensure_ascii=False)[1:-1])
                    if stop > begin:
                        ranges.append((begin, stop))
            emit(json.dumps(value, ensure_ascii=False))
        if error_paths is not None and path in error_paths:
            ranges.append((begin, offset))

    visit(schema)
    return ranges


def underline_substitutions(highlighted_html, schema, error_paths=(), *, underline=True):
    """Preserve highlighting and add semantic underlines without tooltips.

    Native Element.setHTML strips class attributes under its default sanitizer.
    A standard u element survives sanitization and retains its underline.
    """
    errors = substitution_ranges(schema, set(error_paths))
    ranges = [(start, stop, 'u') for start, stop in substitution_ranges(schema) if underline
              and not any(left < stop and right > start for left, right in errors)]
    ranges.extend((start, stop, 'mark') for start, stop in errors)
    ranges.sort()

    class Underliner(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.output = []
            self.in_code = False
            self.text_offset = 0
            self.range_index = 0

        def handle_starttag(self, tag, attrs):
            self.output.append(self.get_starttag_text())
            if tag == 'code':
                self.in_code = True

        def handle_endtag(self, tag):
            self.output.append(f'</{tag}>')
            if tag == 'code':
                self.in_code = False

        def handle_data(self, data):
            if not self.in_code:
                self.output.append(escape(data, quote=False))
                return
            end = self.text_offset + len(data)
            cursor = 0
            while self.range_index < len(ranges) and ranges[self.range_index][1] <= self.text_offset:
                self.range_index += 1
            for index in range(self.range_index, len(ranges)):
                start, stop, tag = ranges[index]
                if start >= end:
                    break
                left, right = max(start, self.text_offset), min(stop, end)
                if left < right:
                    left -= self.text_offset
                    right -= self.text_offset
                    self.output.append(escape(data[cursor:left], quote=False))
                    self.output.append(f'<{tag}>' + escape(data[left:right], quote=False) + f'</{tag}>')
                    cursor = right
            self.output.append(escape(data[cursor:], quote=False))
            self.text_offset = end

    parser = Underliner()
    parser.feed(highlighted_html)
    parser.close()
    return ''.join(parser.output)
