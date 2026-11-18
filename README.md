# ActivityInfo Manager

Tools for updating ActivityInfo form schemas. The feature set is under construction;
this reference covers the available commands and functions.

## Commands

Use the command creator to see the available parameters for each command.

| Command | Purpose |
| --- | --- |
| `AddField` | Adds a new field with the selected type and settings, optionally positioning it within the form. |
| `ModifyField` | Updates selected settings on an existing field while preserving its identity and unrelated settings. Omitted settings remain unchanged; explicit empty values clear settings where permitted. The field's type and variant cannot be changed. |
| `MoveField` | Moves an existing field to the beginning or end of the form, or before or after another field. |
| `DeleteField` | Removes an existing field from the form. It does not check for references to that field elsewhere. |

`ModifyField` identifies its target by exact field code, then falls back to an exact
label with the specified type and variant. `MoveField` and `DeleteField` identify
fields by code. Missing targets produce a warning and leave the schema unchanged.

## Functions

Functions transform a parameter's existing value. Type a call directly into a
permitted text input in the command creator, or write it as a parameter value in
command Text mode. There is no function selector.

For example, enter `Replace("old", "new")` in **New field code** to rename the
existing code by replacing its `old` occurrences with `new`. The creator checks
the call's syntax and arguments; replacement runs when the command is applied.

Write a call directly as the parameter value, without quoting the entire call:

```python
ModifyField(
    fieldCode="population",
    fieldType="quantity",
    validationCondition=Replace("old_code", "new_code"),
)
```

Command and function names are case-insensitive. Function arguments are positional:
write `Replace("old", "new")`, not `Replace(FindText="old", ReplaceText="new")`.
Nested function calls and expressions are not supported. A quoted value such as
`'Replace("old", "new")'` is ordinary text and does not invoke a function. In the
creator, quote the whole call this way when you want to enter it as literal text.
Only registered function names are recognized in creator inputs; other text and
ordinary ActivityInfo formulas retain their existing behavior.

### Replace(FindText, ReplaceText)

Replaces text within the parameter's current value. The target value is implied;
do not supply it as an argument.

| Argument | Type | Meaning |
| --- | --- | --- |
| `FindText` | String | Literal text to find. Must not be empty. |
| `ReplaceText` | String | Text to insert in its place. May be empty to delete matches. |

Replacement is case-sensitive and affects all non-overlapping occurrences. It uses
literal matching, without regular expressions. If there is no match, the current
text is unchanged. Matching also affects substrings within longer identifiers and
quoted formula strings.

For formulas, the current value is converted from IDs to the Names representation
before replacement. The result then goes through the existing name-to-ID resolver.
For example, if the stored formula refers to the field code `population`, this call
can redirect it to `households` without knowing either field's ID:

```python
ModifyField(
    fieldCode="total",
    fieldType="calculated",
    calculationFormula=Replace("population", "households"),
)
```

`Replace` is allowed only in `ModifyField`, in top-level text inputs other than
`fieldCode`, `fieldType`, and `fieldVariant`. This includes the editable `newCode`,
text defaults, and translation settings. Calls inside option or lookup rows are
not supported; boolean, numeric, and selection inputs also remain literal-only.

The current value must contain text. A missing value, a non-text value, or an empty
`FindText` causes an error and leaves all edits in that command unapplied. An empty
or whitespace-only result requests removal of the parameter, subject to its normal
validation rules. Unresolved formula references retain their spelling and produce
the usual warnings.

For translation settings, `Replace` sees the target field's readable code and the
comma-separated language list. A scalar option default is presented as its option
label. Numeric defaults, lists, and objects are not converted to text.

### RegexReplace(FindPattern, ReplaceText)

Works like `Replace`, using a Python regular expression instead of literal search
text. It has the same permitted inputs, implied current value, Names conversion,
and subsequent ID resolution.

| Argument | Type | Meaning |
| --- | --- | --- |
| `FindPattern` | String | Python regex pattern to find. Must not be empty. |
| `ReplaceText` | String | Replacement text, including Python replacement-group references if needed. May be empty to delete matches. |

The pattern is passed directly to Python's `re.sub`, which replaces all matches.
Do not use `/.../g` delimiters or a `g` flag. Matching is case-sensitive by default;
inline Python flags such as `(?i)` can change that behavior.

In a creator text input, for example:

```python
RegexReplace(r"\bpopulation\b", "households")
```

This replaces whole-word occurrences of `population`. Raw strings (`r"..."`) keep
backslashes intact for regex patterns and replacement-group references:

```python
RegexReplace(r"old_(\d+)", r"new_\g<1>")
```

This changes `old_12 old_34` to `new_12 new_34`. With ordinary quoted strings, double
each backslash instead, for example `"\\d+"`. Invalid patterns or replacement-group
references cause an error and leave all edits in that command unapplied. As with
`Replace`, missing current text is an error and an empty result follows the normal
parameter-removal rules.

### AppendFormula(Operator, Formula)

Adds a condition to the parameter's existing formula, unless that condition is
already included. It is available only in `ModifyField` formula inputs:
relevance/required/validation conditions, default formulas, serial prefix formulas,
and calculated formulas. It works in the creator and Text mode.

| Argument | Type | Meaning |
| --- | --- | --- |
| `Operator` | String | `AND` or `OR`, case-insensitive. Produces `&&` or `||`. |
| `Formula` | String | Non-empty formula to add, using readable field codes. |

For example, in **Validation condition**:

```python
AppendFormula("AND", "population > 0")
```

If the current formula is missing, empty, or whitespace-only, `Formula` is written
directly. Otherwise, when it is not already included, the result is
`(current formula) && (Formula)` for AND, or `(current formula) || (Formula)` for OR.
Current formula references are converted to Names before comparison; the result
is resolved back to IDs through the existing pipeline.

Presence detection ignores whitespace outside quoted text and redundant grouping,
and recognizes reordered/regrouped terms within the same logical operator.
For example, adding `A` with AND to `B && A`, or adding `B && A` with AND to
`A && B && C`, leaves the existing formula unchanged. It does not skip `A` when
adding it with AND to `A || B`: the surrounding operator changes its meaning.
Whole-formula equality also skips the append, regardless of the chosen operator.

This is structural comparison, not a proof of arbitrary logical equivalence.
Arithmetic/comparison order and function argument order remain significant; quoted
text remains exact. Unsupported or malformed formulas and invalid operators cause
an error and leave the command unapplied. The standalone
[formula parser reference](formula_parser/README.md) describes its supported grammar
and reuse API. Normalization is only used for comparison; existing text is retained.

### Append(Position, Separator, AddText)

Adds regular text at the start or end of a parameter's existing value, avoiding a
duplicate at that boundary. Available in `ModifyField`'s top-level non-formula text
inputs, through the creator or Text mode.

| Argument | Type | Meaning |
| --- | --- | --- |
| `Position` | String | `Start` or `End`, case-insensitive. |
| `Separator` | String | Exact text between the existing value and `AddText`. May be empty. |
| `AddText` | String | Text to add. May be empty. |

For example, in **Description**:

```python
Append("End", " - ", "Reviewed")
```

If the current value is missing or empty, the result is `AddText`, without the
separator. Otherwise:

- `Start` checks for `AddText + Separator` at the beginning and prepends it if absent.
- `End` checks for `Separator + AddText` at the end and appends it if absent.

The boundary check is case-insensitive, including the separator. A match leaves
the existing text and capitalization unchanged. Text elsewhere in the value does
not count as a match. Spacing is exact and is not trimmed; whitespace-only current
text counts as non-empty.

For example, `"Notes - REVIEWED"` remains unchanged with the call above, while
`"Notes Reviewed"` becomes `"Notes Reviewed - Reviewed"` because the separator
does not match. Invalid positions cause an error without applying the command.
Formula inputs use `AppendFormula` instead. Normal text-value validation and
parameter-removal rules still apply.

## Running locally

```powershell
uv sync --locked
uv run --locked python main.py
```

Run offline checks with `uv run --locked python -m unittest discover -s test -t . -v`.
