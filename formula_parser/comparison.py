"""Conservative structural equivalence and top-level Boolean inclusion."""

from .parser import Expression


def normalize(expression: Expression) -> Expression:
    children = tuple(normalize(child) for child in expression.children)
    if expression.kind == 'logical':
        flattened = []
        for child in children:
            flattened.extend(child.children if child.kind == 'logical' and child.value == expression.value
                             else (child,))
        children = tuple(sorted(set(flattened)))
        if len(children) == 1:
            return children[0]
    return Expression(expression.kind, expression.value, children)


def includes(current: Expression, candidate: Expression, operator: str) -> bool:
    """Check equality or inclusion among top-level terms of AND/OR only."""
    operator = operator.upper()
    if operator not in ('AND', 'OR'):
        raise ValueError('Operator must be AND or OR.')
    current, candidate = normalize(current), normalize(candidate)
    if current == candidate:
        return True

    def terms(expression):
        return frozenset(expression.children if expression.kind == 'logical' and expression.value == operator
                         else (expression,))

    return terms(candidate) <= terms(current)
