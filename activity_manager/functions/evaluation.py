"""Evaluate pure registered functions without exposing the mutable schema."""

from .validation import validate_function


def evaluate_function(call, context):
    try:
        definition = validate_function(call, context.parameter, command_name=context.command)
        if context.current_value is not None and type(context.current_value) is not str:
            raise ValueError('The current parameter value must be text or None.')
        value = definition.implementation(context, *call.arguments)
        if type(value) is not str:
            raise ValueError('Function must return text.')
        return value
    except Exception as exc:
        raise ValueError(f'{context.parameter}: function {call.name!r} failed: {exc}') from exc
