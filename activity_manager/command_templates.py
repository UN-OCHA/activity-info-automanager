"""Single-pass text substitution; no expression evaluation or implicit coercion."""

import re
from dataclasses import fields, is_dataclass, replace

from .functions import FunctionCall


REFERENCE = re.compile(r'(?:match\.(?:code|[A-Za-z_]\w*|[1-9]\d*)|iteration\.index|vars\.[A-Za-z_]\w*)\Z')


def substitute_text(text, variables=None):
    result = []
    index = 0
    while index < len(text):
        if text.startswith('$${', index):
            result.append('${' if variables is not None else '$${')
            index += 3
        elif text.startswith('${', index):
            end = text.find('}', index + 2)
            reference = text[index + 2:end] if end >= 0 else ''
            if not REFERENCE.fullmatch(reference):
                raise ValueError(f'Invalid variable placeholder at character {index + 1}: {text[index:]!r}.')
            if variables is None:
                result.append(text[index:end + 1])
            else:
                if reference not in variables or variables[reference] is None:
                    raise ValueError(f'Variable ${{{reference}}} is undefined or its capture group did not match.')
                result.append(str(variables[reference]))
            index = end + 1
        else:
            result.append(text[index])
            index += 1
    return ''.join(result)


def substitute_value(value, variables=None):
    if isinstance(value, str):
        return substitute_text(value, variables)
    if isinstance(value, FunctionCall):
        return FunctionCall(value.name, [substitute_value(item, variables) for item in value.arguments])
    if isinstance(value, dict):
        return {key: substitute_value(item, variables) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(substitute_value(item, variables) for item in value)
    return value


def command_parameters(command, variables=None):
    """Selectors stay literal. Reconstruct commands to validate resolved values."""
    if not is_dataclass(command):
        return command
    values = {field.name: substitute_value(getattr(command, field.name), variables)
              for field in fields(command) if field.init and field.name != 'field_code'}
    if variables is not None and 'changes' in values:
        from .commands.modify_field import MODIFY_FIELD_PARAMETERS, normalize_blank_edit
        definitions = {parameter.key: parameter for parameter in MODIFY_FIELD_PARAMETERS}
        values['changes'] = {
            key: normalize_blank_edit(definitions[key], value)
            if value != command.changes[key] else value
            for key, value in values['changes'].items()
        }
    return replace(command, **values)
