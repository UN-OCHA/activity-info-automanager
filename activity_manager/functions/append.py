"""Add text at one end unless it matches the whole value or boundary."""

from .append_formula import APPEND_FORMULA
from .models import FunctionArgument, FunctionContext, FunctionDefinition
from .replace import REPLACE


def append(context: FunctionContext, position: str, separator: str, add_text: str) -> str:
    position = position.casefold()
    if position not in ('start', 'end'):
        raise ValueError('Position must be Start or End.')
    current = context.current_value
    if current is None or current == '':
        return add_text
    current_folded = current.casefold()
    if current_folded == add_text.casefold():
        return current
    if position == 'start':
        prefix = add_text + separator
        return current if current_folded.startswith(prefix.casefold()) else prefix + current
    suffix = separator + add_text
    return current if current_folded.endswith(suffix.casefold()) else current + suffix


APPEND = FunctionDefinition(
    name='Append',
    commands=REPLACE.commands | {'AddModifyField'},
    arguments=(FunctionArgument('Position'), FunctionArgument('Separator'), FunctionArgument('AddText')),
    parameters=REPLACE.parameters - APPEND_FORMULA.parameters,
    implementation=append,
)
