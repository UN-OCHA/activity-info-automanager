"""Parse a documented expression grammar without evaluating formulas."""

from dataclasses import dataclass
import re


@dataclass(frozen=True, order=True)
class Expression:
    kind: str
    value: str
    children: tuple['Expression', ...] = ()


class FormulaSyntaxError(ValueError):
    def __init__(self, message: str, source: str, offset: int):
        self.offset = offset
        self.line = source.count('\n', 0, offset) + 1
        self.column = offset - source.rfind('\n', 0, offset)
        super().__init__(f'{message} (line {self.line}, column {self.column}).')


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    offset: int


_NUMBER = re.compile(r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')
_NAME = re.compile(r'(?:@parent\b|[^\W\d]\w*)', re.UNICODE)
_PRECEDENCE = {'OR': 1, 'AND': 2, '=': 3, '==': 3, '!=': 3, '<>': 3,
               '<': 3, '>': 3, '<=': 3, '>=': 3, '+': 4, '-': 4,
               '*': 5, '/': 5, '%': 5, '^': 7}


def _tokens(source):
    position = 0
    while position < len(source):
        char = source[position]
        if char.isspace():
            position += 1
            continue
        start = position
        if char in ('"', "'"):
            quote = char
            position += 1
            while position < len(source):
                if source[position] == '\\':
                    position += 2
                elif source[position] == quote:
                    if position + 1 < len(source) and source[position + 1] == quote:
                        position += 2
                    else:
                        position += 1
                        break
                else:
                    position += 1
            else:
                raise FormulaSyntaxError('Unterminated quoted string', source, start)
            yield _Token('string', source[start:position], start)
            continue
        if char == '[':
            end = source.find(']', position + 1)
            if end < 0:
                raise FormulaSyntaxError('Unterminated bracketed reference', source, start)
            if not source[position + 1:end].strip():
                raise FormulaSyntaxError('Empty bracketed reference', source, start)
            position = end + 1
            yield _Token('reference', source[start:position], start)
            continue
        match = _NUMBER.match(source, position) or _NAME.match(source, position)
        if match:
            position = match.end()
            kind = 'number' if char.isdecimal() or char == '.' else 'name'
            yield _Token(kind, match.group(), start)
            continue
        pair = source[position:position + 2]
        if pair in ('&&', '||', '==', '!=', '<>', '<=', '>='):
            position += 2
            yield _Token('operator', pair, start)
        elif char in '()+-*/%^<>=!,.':
            position += 1
            yield _Token('punctuation', char, start)
        else:
            raise FormulaSyntaxError(f'Unsupported character {char!r}', source, start)
    yield _Token('end', '', len(source))


class _Parser:
    def __init__(self, source):
        self.source = source
        self.tokens = list(_tokens(source))
        self.index = 0

    @property
    def current(self):
        return self.tokens[self.index]

    def advance(self):
        token = self.current
        self.index += 1
        return token

    def fail(self, message):
        raise FormulaSyntaxError(message, self.source, self.current.offset)

    def require(self, value):
        if self.current.value != value:
            self.fail(f'Expected {value!r}')
        self.advance()

    def expression(self, minimum=1):
        token = self.current
        upper = token.value.upper()
        # NOT(...) is interpreted as logical negation, including its grouping.
        if token.value in ('!', '+', '-') or token.kind == 'name' and upper == 'NOT':
            self.advance()
            binding = 3 if token.kind == 'name' and upper == 'NOT' else 6
            left = Expression('unary', 'NOT' if upper in ('!', 'NOT') else token.value,
                              (self.expression(binding),))
        elif token.value == '(':
            self.advance()
            left = self.expression()
            self.require(')')
        elif token.kind in ('name', 'reference', 'string', 'number'):
            self.advance()
            if token.kind == 'name' and upper in ('AND', 'OR') and self.current.value != '(':
                self.fail('Expected an operand')
            left = Expression(token.kind, token.value)
        else:
            self.fail('Expected an operand')

        while self.current.value in ('.', '('):
            if self.current.value == '.':
                self.advance()
                if self.current.kind not in ('name', 'reference'):
                    self.fail('Expected a reference after dot')
                member = self.advance()
                left = Expression('member', member.value, (left,))
            else:
                if left.kind not in ('name', 'member'):
                    self.fail('Expected a function name before opening parenthesis')
                self.advance()
                arguments = []
                if self.current.value != ')':
                    arguments.append(self.expression())
                    while self.current.value == ',':
                        self.advance()
                        arguments.append(self.expression())
                self.require(')')
                left = Expression('call', '', (left, *arguments))

        while True:
            token = self.current
            operator = {'&&': 'AND', '||': 'OR'}.get(token.value, token.value.upper())
            # Quoted strings and bracketed references never become operators.
            precedence = _PRECEDENCE.get(operator) if token.kind in ('name', 'operator', 'punctuation') else None
            if precedence is None or precedence < minimum:
                return left
            self.advance()
            right = self.expression(precedence if operator == '^' else precedence + 1)
            if precedence == 3 and left.kind == 'binary' and _PRECEDENCE.get(left.value) == 3:
                raise FormulaSyntaxError('Chained comparisons are unsupported', self.source, token.offset)
            left = Expression('logical' if operator in ('AND', 'OR') else 'binary',
                              operator, (left, right))


def parse(source: str) -> Expression:
    """Parse text into immutable nodes; unsupported or malformed syntax raises."""
    parser = _Parser(source)
    result = parser.expression()
    if parser.current.kind != 'end':
        parser.fail('Unexpected token')
    return result
