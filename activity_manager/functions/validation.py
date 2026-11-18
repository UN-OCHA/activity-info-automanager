"""Lookup and argument validation for registered parameter functions."""

from . import catalog
from .models import FunctionCall


def validate_function(call: FunctionCall, parameter: str, *, command_name: str):
    definition = catalog.FUNCTIONS_BY_NAME.get(call.name.casefold())
    if definition is None:
        raise ValueError(f'Unknown function {call.name!r}.')
    if command_name.casefold() not in {name.casefold() for name in definition.commands}:
        raise ValueError(f'Function {call.name!r} is not supported in {command_name}.')
    if parameter not in definition.parameters:
        raise ValueError(f'Function {call.name!r} is not supported for {parameter}.')
    values = call.arguments
    if len(values) != len(definition.arguments):
        raise ValueError(f'Function {call.name!r} requires {len(definition.arguments)} positional arguments.')
    for value, argument in zip(values, definition.arguments):
        if type(value) not in argument.types:
            raise ValueError(f'Function {call.name!r} argument {argument.name!r} has an invalid literal type.')
    return definition
