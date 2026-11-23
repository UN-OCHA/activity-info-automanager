"""Safe parsing of the editable command-list text format."""

import ast
import io
import tokenize
import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommandTextError(ValueError):
    """A source-located command text parsing or validation error."""

    message: str
    line: int
    column: int
    source_line: str

    def __str__(self) -> str:
        return self.message


def command_error(text: str, node: ast.AST, message: str) -> CommandTextError:
    """Create an error located at an AST node."""
    line = getattr(node, 'lineno', 1)
    column = getattr(node, 'col_offset', 0) + 1
    return CommandTextError(message, line, column, _source_line(text, line))


def parse_string_parameters(
    text: str,
    call: ast.Call,
    command_name: str,
    *,
    allowed: Collection[str],
    required: Collection[str],
) -> dict[str, str]:
    """Validate named string parameters shared by the current commands."""
    return parse_literal_parameters(
        text, call, command_name, types={name: str for name in allowed}, required=required,
    )


def parse_literal_parameters(
    text: str,
    call: ast.Call,
    command_name: str,
    *,
    types: Mapping[str, type | tuple[type, ...]],
    required: Collection[str],
    allow_empty: Collection[str] = (),
    function_parameters: Collection[str] = (),
    blank_parameters: Collection[str] = (),
) -> dict[str, Any]:
    """Accept only explicitly typed constants, without evaluating expressions."""
    canonical_names = {name.casefold(): name for name in types}
    values: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise command_error(
                text,
                keyword.value,
                f'{command_name} does not support expanded keyword arguments.',
            )
        canonical_name = canonical_names.get(keyword.arg.casefold())
        if canonical_name is None:
            raise command_error(
                text,
                keyword,
                f'{command_name} has an unknown parameter {keyword.arg!r}.',
            )
        if canonical_name in values:
            raise command_error(
                text,
                keyword,
                f'{command_name} parameter {canonical_name!r} is specified more than once.',
            )
        expected_type = types[canonical_name]
        if isinstance(keyword.value, ast.Call) and canonical_name in function_parameters:
            values[canonical_name] = _function_value(text, keyword.value, canonical_name, command_name)
            continue
        value = _literal_value(text, keyword.value)
        accepted_types = expected_type if isinstance(expected_type, tuple) else (expected_type,)
        if type(value) not in accepted_types and not (value == '' and canonical_name in blank_parameters):
            literal_name = 'string' if expected_type is str else 'boolean' if expected_type is bool else 'supported'
            raise command_error(
                text,
                keyword.value,
                f'{command_name} parameter {canonical_name!r} must be a {literal_name} literal.',
            )
        if value == '' and canonical_name not in allow_empty:
            raise command_error(
                text,
                keyword.value,
                f'{command_name} parameter {canonical_name!r} cannot be empty.',
            )
        values[canonical_name] = value

    for name in required:
        if name not in values:
            raise command_error(
                text,
                call,
                f'{command_name} requires parameter {name!r}.',
            )
    return values


def parse_function_input(value: str, *, parameter: str, command_name: str):
    """Recognize registered calls in creator text, preserving ordinary formulas."""
    from .functions import catalog

    def starts_with_function(text):
        match = re.match(r'\s*([^\W\d]\w*)\s*\(', text)
        return match is not None and match.group(1).casefold() in catalog.FUNCTIONS_BY_NAME

    text = value.strip()
    # Quoting a whole call explicitly requests call-shaped literal text.
    if text.startswith(('"', "'")):
        try:
            node = ast.parse(text, mode='eval').body
        except SyntaxError:
            return value
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and starts_with_function(node.value):
            return node.value
        return value
    if not starts_with_function(text):
        return value
    try:
        node = ast.parse(text, mode='eval').body
    except SyntaxError as exc:
        raise CommandTextError(exc.msg, exc.lineno or 1, exc.offset or 1,
                               _source_line(text, exc.lineno or 1)) from None
    if not isinstance(node, ast.Call):
        raise command_error(text, node, 'Expected a function call with positional literal arguments only.')
    return _function_value(text, node, parameter, command_name)


def _function_value(text, node, parameter, command_name):
    from .functions import FunctionCall, validate_function

    if not isinstance(node.func, ast.Name):
        raise command_error(text, node.func, 'Expected a simple registered function name.')
    if node.keywords:
        raise command_error(text, node.keywords[0], 'Functions accept positional literal arguments only.')
    arguments = [_literal_value(text, argument) for argument in node.args]
    try:
        call = FunctionCall(node.func.id, arguments)
        definition = validate_function(call, parameter, command_name=command_name)
        return FunctionCall(definition.name, arguments)
    except ValueError as exc:
        raise command_error(text, node, str(exc)) from None


def _literal_value(text: str, node: ast.AST):
    """Read JSON-like literals plus Python booleans; never evaluate calls/code."""
    if isinstance(node, ast.Constant) and type(node.value) in (str, bool, int, float, type(None)):
        if isinstance(node.value, float) and not math.isfinite(node.value):
            raise command_error(text, node, 'Numbers must be finite.')
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        if isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float):
            value = _literal_value(text, node.operand)
            return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.List):
        return [_literal_value(text, item) for item in node.elts]
    if isinstance(node, ast.Dict):
        result = {}
        for key, value in zip(node.keys, node.values):
            if key is None or not isinstance(key, ast.Constant) or type(key.value) is not str:
                raise command_error(text, node, 'Object keys must be string literals; expansion is not supported.')
            if key.value in result:
                raise command_error(text, key, f'Duplicate object key {key.value!r}.')
            result[key.value] = _literal_value(text, value)
        return result
    raise command_error(text, node, 'Expected a literal value; expressions and calls are not supported.')


def parse_command_text(text: str) -> list[Any]:
    """Parse a complete command document without evaluating any user code."""
    try:
        tree = ast.parse(text, mode='exec')
    except SyntaxError as exc:
        line = exc.lineno or 1
        column = exc.offset or 1
        message = exc.msg
        if message.startswith('unterminated string literal'):
            message += ' Multiline values must use triple quotes (""" or \'\'\').'
        raise CommandTextError(
            message,
            line,
            column,
            _source_line(text, line),
        ) from None

    _reject_semicolons(text)

    # Imported lazily so command modules can use the shared validation helpers
    # above without creating an import cycle during module initialization.
    from .commands import COMMANDS_BY_NAME

    commands = []
    previous_line = 0
    for statement in tree.body:
        if statement.lineno == previous_line:
            raise command_error(
                text,
                statement,
                'Each command must start on a new line.',
            )
        previous_line = statement.lineno
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            raise command_error(
                text,
                statement,
                'Expected a command in the form CommandName(parameter="value").',
            )
        call = statement.value
        if not isinstance(call.func, ast.Name):
            raise command_error(text, call.func, 'Expected a simple command name.')
        definition = COMMANDS_BY_NAME.get(call.func.id.casefold())
        if definition is None:
            raise command_error(text, call.func, f'Unknown command {call.func.id!r}.')
        if call.args:
            raise command_error(
                text,
                call.args[0],
                f'{call.func.id} accepts named parameters only.',
            )
        from .command_iteration import parse_authored_command
        commands.append(parse_authored_command(text, call, definition))
    return commands


def _reject_semicolons(text: str) -> None:
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type == tokenize.OP and token.string == ';':
                line, column = token.start
                raise CommandTextError(
                    'Each command must be followed by a line break, not a semicolon.',
                    line,
                    column + 1,
                    _source_line(text, line),
                )
    except (tokenize.TokenError, IndentationError):
        # ast.parse provides the more useful source-located syntax error first.
        return


def _source_line(text: str, line: int) -> str:
    lines = text.splitlines()
    return lines[line - 1] if 0 < line <= len(lines) else ''
