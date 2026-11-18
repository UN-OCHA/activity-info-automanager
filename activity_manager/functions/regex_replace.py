"""Python regex replacement in the parameter's readable current text."""

import re

from .models import FunctionArgument, FunctionContext, FunctionDefinition
from .replace import REPLACE


def regex_replace(context: FunctionContext, find_pattern: str, replace_text: str) -> str:
    if not find_pattern:
        raise ValueError('FindPattern cannot be empty.')
    if context.current_value is None:
        raise ValueError('The parameter has no current text value to replace.')
    return re.sub(find_pattern, replace_text, context.current_value)


REGEX_REPLACE = FunctionDefinition(
    name='RegexReplace',
    commands=REPLACE.commands,
    arguments=(FunctionArgument('FindPattern'), FunctionArgument('ReplaceText')),
    parameters=REPLACE.parameters,
    implementation=regex_replace,
)
