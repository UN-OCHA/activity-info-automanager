"""Data contracts for explicitly registered text transformations."""

from dataclasses import dataclass, field
import json
import keyword
import math
from collections.abc import Callable


def validate_literal(value):
    if type(value) in (str, bool, int, type(None)):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            validate_literal(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            validate_literal(item)
        return
    raise ValueError('Function arguments must be finite literal values.')


@dataclass(frozen=True, init=False)
class FunctionCall:
    """Immutable call; argument access returns detached literal values."""

    name: str
    _arguments_json: str

    def __init__(self, name: str, arguments=()):
        if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
            raise ValueError('Expected a simple function name.')
        arguments = list(arguments)
        validate_literal(arguments)
        object.__setattr__(self, 'name', name)
        object.__setattr__(self, '_arguments_json', json.dumps(arguments, ensure_ascii=False))

    @property
    def arguments(self):
        return tuple(json.loads(self._arguments_json))


@dataclass(frozen=True)
class FunctionContext:
    """Current value before any edits in this command; absent values are None."""

    parameter: str
    current_value: str | None
    command: str = field(kw_only=True)


@dataclass(frozen=True)
class FunctionArgument:
    name: str
    types: tuple[type, ...] = (str,)

    def __post_init__(self):
        object.__setattr__(self, 'types', tuple(self.types))


@dataclass(frozen=True)
class FunctionDefinition:
    """Implementation receives (context, *positional_arguments) and returns text."""

    name: str
    arguments: tuple[FunctionArgument, ...]
    parameters: frozenset[str]
    implementation: Callable[..., str]
    commands: frozenset[str] = field(kw_only=True)

    def __post_init__(self):
        FunctionCall(self.name)
        object.__setattr__(self, 'arguments', tuple(self.arguments))
        object.__setattr__(self, 'parameters', frozenset(self.parameters))
        object.__setattr__(self, 'commands', frozenset(self.commands))
        if not callable(self.implementation):
            raise ValueError('Function implementation must be callable.')
