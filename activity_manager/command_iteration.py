"""Authored multi-field selectors and their stable, ordered application plans."""

import ast
import json
import re
from copy import deepcopy
from dataclasses import dataclass, replace

from .command_templates import FIELD_BUILTINS, command_parameters, substitute_selector


ITERABLE_COMMANDS = frozenset({'movefield', 'deletefield', 'modifyfield'})
ITERABLE_CREATORS = frozenset({'move_field', 'delete_field', 'modify_field'})


def compile_pattern(pattern, *, kind='field'):
    label = 'Form name' if kind == 'form' else 'Field code'
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f'{label} pattern is required.')
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f'Invalid {label.lower()} pattern: {exc}') from None
    builtins = {'name', 'index'} if kind == 'form' else FIELD_BUILTINS
    for name in sorted(builtins.intersection(compiled.groupindex)):
        raise ValueError(f'Capture group {name!r} is reserved for built-in {kind} context.')
    return compiled


def validate_pattern(pattern, *, kind='field'):
    """Validate authored templates; compile after substitution at selection time."""
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f'{"Form name" if kind == "form" else "Field code"} pattern is required.')
    substitute_selector(pattern)
    if '${' not in pattern:
        compile_pattern(pattern, kind=kind)


@dataclass(frozen=True)
class IterativeCommand:
    command: object
    field_pattern: str

    def __post_init__(self):
        validate_pattern(self.field_pattern)
        command_parameters(self.command)

    def to_syntax(self):
        call = ast.parse(self.command.to_syntax(), mode='eval').body
        arguments = []
        for keyword in call.keywords:
            if keyword.arg == 'fieldCode':
                arguments.append('fieldPattern=' + json.dumps(self.field_pattern, ensure_ascii=False))
            else:
                arguments.append(keyword.arg + '=' + ast.unparse(keyword.value))
        return f'{call.func.id}({", ".join(arguments)})'

    def targets(self, schema, variables=None):
        if not isinstance(schema, dict) or not isinstance(schema.get('elements'), list):
            raise ValueError('Multi-field commands require a form schema with an elements list.')
        pattern = compile_pattern(substitute_selector(self.field_pattern, variables if variables is not None else {}))
        targets = []
        for element in schema['elements']:
            if not isinstance(element, dict) or not isinstance(element.get('code'), str):
                continue
            code = element['code']
            match = pattern.fullmatch(code)
            if match is None:
                continue
            field_variables = {'field.code': code, 'field.index': len(targets) + 1}
            field_variables.update({f'field.{index}': value for index, value in enumerate(match.groups(), 1)})
            field_variables.update({f'field.{key}': value for key, value in match.groupdict().items()})
            targets.append(IterationTarget(element.get('id'), code, field_variables))
        return tuple(targets)


@dataclass(frozen=True)
class IterationTarget:
    field_id: str | None
    original_code: str
    variables: dict

    def prepare(self, authored, schema, previous=None, variables=None):
        elements = schema['elements']
        matches = [field for field in elements if isinstance(field, dict) and (
            field.get('id') == self.field_id if self.field_id else field.get('code') == self.original_code)]
        if len(matches) != 1:
            raise ValueError(f'Target {self.original_code!r} no longer identifies exactly one field.')
        code = matches[0].get('code')
        if not isinstance(code, str) or sum(isinstance(field, dict) and field.get('code') == code
                                         for field in elements) != 1:
            raise ValueError(f'Target {self.original_code!r} has an ambiguous or missing current code.')
        command = command_parameters(replace(authored.command, field_code=code),
                                     {**(variables or {}), **self.variables}, selector_resolved=True)
        from .commands.move_field import MoveFieldCommand
        if isinstance(command, MoveFieldCommand) and previous is not None:
            command = replace(command, position='After field', before_after_field=previous)
        return PreparedCommand(command)


@dataclass(frozen=True)
class PreparedCommand:
    """Concrete values must never undergo a second substitution pass."""
    command: object

    def execute(self, schema, *, context=None):
        return self.command.execute(schema, context=context)


def parse_authored_command(text, call, definition):
    from .command_parser import command_error
    patterns = [keyword for keyword in call.keywords if keyword.arg and keyword.arg.casefold() == 'fieldpattern']
    try:
        if not patterns:
            command = definition.parser(text, call)
            command_parameters(command)
            return command
        if definition.name.casefold() not in ITERABLE_COMMANDS:
            raise ValueError(f'{definition.name} does not support multiple fields.')
        if len(patterns) != 1 or any(keyword.arg and keyword.arg.casefold() == 'fieldcode' for keyword in call.keywords):
            raise ValueError('Specify exactly one of fieldCode or fieldPattern.')
        pattern_node = patterns[0].value
        if not isinstance(pattern_node, ast.Constant) or not isinstance(pattern_node.value, str):
            raise ValueError('fieldPattern must be a string literal.')
        pattern = pattern_node.value
        validate_pattern(pattern)
        concrete = deepcopy(call)
        for keyword in concrete.keywords:
            if keyword.arg and keyword.arg.casefold() == 'fieldpattern':
                keyword.arg = 'fieldCode'
                keyword.value = ast.copy_location(ast.Constant('__iteration_target__'), keyword.value)
        return IterativeCommand(definition.parser(text, concrete), pattern)
    except ValueError as exc:
        from .command_parser import CommandTextError
        if isinstance(exc, CommandTextError):
            raise
        raise command_error(text, call, str(exc)) from None
