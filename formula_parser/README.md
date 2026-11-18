# Formula parser

A standalone, standard-library-only Python package for parsing expressions and
checking conservative Boolean inclusion. It has no application, UI, schema, or
ActivityInfo dependencies; copy the `formula_parser/` directory into another project
to reuse it. The application tests for it are in `test/test_formula_parser.py`.

```python
from formula_parser import parse, normalize, includes

current = parse("A > 0 AND (B > 0 OR C > 0)")
candidate = parse("C > 0 || B > 0")
assert includes(current, candidate, "AND")
assert normalize(parse("A && B")) == normalize(parse("B AND (A)"))
```

## API

- `parse(source: str) -> Expression`: immutable expression tree. Malformed or
  unsupported syntax raises `FormulaSyntaxError`, with zero-based `offset` and
  one-based `line`/`column` attributes. No evaluation or name lookup takes place.
- `normalize(expression) -> Expression`: removes syntactic grouping, recursively
  flattens same-operator logical combinations, sorts their operands, and removes
  duplicate Boolean terms. All other expression order and token spelling remains
  significant. It does not alter the input tree.
- `includes(current, candidate, operator: str) -> bool`: normalizes both trees,
  checks whole-expression equality, then tests inclusion among top-level terms
  using the requested AND/OR operator (case-insensitive). It never searches
  arbitrary nested subexpressions.

Expression nodes have `kind`, `value`, and tuple `children`. Source text is not
reconstructed from normalized trees; callers retain their original strings.

## Supported grammar

Operands include identifiers, bracketed references (such as `[Field name]`), numeric
literals with optional decimal/exponent, and single/double-quoted strings.
Strings support backslash escapes and doubled matching quotes; their complete
spelling is preserved. References support dotted members and `@parent`. Function
calls accept ordered, comma-separated expression arguments, including zero
arguments. No function is executed or its meaning inferred.

Infix operators, from lowest to highest precedence:

1. `OR`, `||`
2. `AND`, `&&`
3. `=`, `==`, `!=`, `<>`, `<`, `>`, `<=`, `>=`
4. `+`, `-`
5. `*`, `/`, `%`
6. `^` (right-associative exponentiation)

Logical words are case-insensitive. Prefix `NOT` binds above AND and below
comparisons; prefix `!`, `+`, and `-` bind above multiplication and below
exponentiation. Parentheses override precedence. Only logical AND/OR combinations
are reordered; comparison/arithmetic operands and function arguments retain their
order. Equivalent quoted-string spellings and bracketed/bare reference spellings
are not unified. Function calls such as `AND(A,B)` remain calls, not logical nodes.

This is a deliberately limited expression grammar, not a complete validator for
any external formula language. Chained comparisons, comments, arrays, ternaries,
and other unlisted syntax are unsupported. Invalid syntax causes an error rather
than a guessed inclusion result. The parser performs no distribution, absorption,
De Morgan transformation, arithmetic simplification, or comparison reversal.
