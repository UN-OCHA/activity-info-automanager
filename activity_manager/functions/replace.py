"""Literal replacement in the parameter's readable current text."""

from .models import FunctionArgument, FunctionContext, FunctionDefinition


def replace(context: FunctionContext, find_text: str, replace_text: str) -> str:
    if not find_text:
        raise ValueError('FindText cannot be empty.')
    if context.current_value is None:
        raise ValueError('The parameter has no current text value to replace.')
    return context.current_value.replace(find_text, replace_text)


REPLACE = FunctionDefinition(
    name='Replace',
    commands=frozenset({'ModifyField'}),
    arguments=(FunctionArgument('FindText'), FunctionArgument('ReplaceText')),
    parameters=frozenset({
        'newCode', 'label', 'description', 'relevanceCondition', 'requiredCondition',
        'validationCondition', 'validationMessage', 'defaultFormula', 'units',
        'inputMask', 'prefixFormula', 'calculationFormula',
        'defaultValue', 'translationField', 'translationLanguage',
    }),
    implementation=replace,
)
