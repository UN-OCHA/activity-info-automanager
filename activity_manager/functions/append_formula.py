"""Append a Boolean condition unless already included at the relevant level."""

from formula_parser import includes, parse

from .models import FunctionArgument, FunctionContext, FunctionDefinition


def append_formula(context: FunctionContext, operator: str, formula: str) -> str:
    operator = operator.upper()
    if operator not in ('AND', 'OR'):
        raise ValueError('Operator must be AND or OR.')
    if not formula.strip():
        raise ValueError('Formula cannot be empty.')
    candidate = parse(formula)
    current = context.current_value
    if current is None or not current.strip():
        return formula
    if includes(parse(current), candidate, operator):
        return current
    symbol = '&&' if operator == 'AND' else '||'
    return f'({current}) {symbol} ({formula})'


APPEND_FORMULA = FunctionDefinition(
    name='AppendFormula',
    commands=frozenset({'ModifyField'}),
    arguments=(FunctionArgument('Operator'), FunctionArgument('Formula')),
    parameters=frozenset({'relevanceCondition', 'requiredCondition', 'validationCondition',
                          'defaultFormula', 'prefixFormula', 'calculationFormula'}),
    implementation=append_formula,
)
