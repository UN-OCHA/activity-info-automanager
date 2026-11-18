# Command parameter functions

ModifyField supports registered function calls in all top-level text inputs,
including defaultValue, translationField, and translationLanguage.
`FUNCTION_PARAMETERS` in `commands/modify_field.py` is the authoritative command
eligibility map. The three target selectors (fieldCode, fieldType, fieldVariant),
nested rows/groups, booleans, numeric inputs, and selection controls do not accept
calls. AddField remains literal-only. A function definition must also explicitly
permit the command and parameter; both parsing and application enforce permissions.

The production catalog contains Replace, RegexReplace, AppendFormula, and Append. A call to an unregistered name is an
error. Each function belongs in its own module under
`activity_manager/functions/`, with an explicit definition registered in
`functions/catalog.py`. No dynamic imports or filesystem discovery are used.

## Adding a function

Define a pure implementation with the signature
`implementation(context: FunctionContext, *arguments) -> str`. Export a
`FunctionDefinition` with:

- A canonical public name, matched case-insensitively.
- A frozenset of permitted command names, matched case-insensitively.
- An ordered tuple of FunctionArgument definitions (name and exact allowed types).
- A frozenset of supported public parameter names, such as validationCondition.
- The implementation callable.

Import that definition explicitly in catalog.py and include it in the immutable
FUNCTIONS tuple. A read-only lookup index is derived from that tuple. Duplicate
names, including case-only differences, are rejected. Keep each module independent
of command/UI code and the catalog to avoid circular imports. Argument validation
lives in validation.py; execution lives in evaluation.py. Parsed function names
serialize using the canonical spelling from their definition.

All declared arguments are required and positional. Arguments support strings,
booleans, finite numbers, None, lists, and string-keyed dictionaries; the definition
restricts their top-level types further. Implementations validate any semantic or
nested-container constraints. No nested calls, variables, expressions, attribute
access, keyword arguments, or argument expansion are allowed. Python code is never
evaluated from command text.

## Current value and execution

FunctionContext is frozen and contains the command name, public parameter name, and its current
text value in the Names representation (None when absent/null). Existing formula
conversion handles IDs, field codes, related-form paths, and quoted literals;
ordinary text is unchanged. Commands receive cached related schemas through the
shared CommandContext; functions receive only the resulting plain text. The context
is supplied internally; it is not
a user-visible positional argument. This leaves future functions free to decide
how their public signatures use the existing value. Functions receive no schema
reference. They must not perform I/O or mutate external state; this is a contract
for trusted registered implementations, not a sandbox for arbitrary Python.

Parsing validates the call without running it. FunctionCall stores detached literal
arguments and serializes back to an unquoted call. Quoted call-shaped text remains
literal text. Permitted creator text inputs use the declarative parse_text hook to
recognize registered function calls through the same AST parser and permission
checks as Text mode. A leading registered name followed by an opening parenthesis
identifies a call; malformed calls produce input errors and disable Add. Ordinary
text and ActivityInfo formulas beginning with other names remain literal. Quoting
an entire call explicitly produces literal call-shaped text, and Text/List round
trips preserve that distinction. Calls are also available through command Text
mode and programmatic FunctionCall values. The creator never evaluates functions.

During application, target lookup and applicability checks precede evaluation.
Every function sees its own parameter's value from before this command's edits.
All functions run and their text results are validated before any edits are applied
to the copied schema. Empty/whitespace output follows the existing text-edit rule:
it requests removal, subject to required-setting validation. Function failures,
invalid return types, and subsequent edit-validation failures return the unchanged
incoming schema with an Error. Function errors identify the function and parameter.
The stored command keeps the original call for subsequent previews/serialization.
Existing ActivityInfo formula resolution runs after command transformation.

Tests use temporary catalogs for synthetic functions; production exposes Replace, RegexReplace, AppendFormula, and Append.

## Replace

```python
ModifyField(fieldCode="target", fieldType="quantity",
            validationCondition=Replace("population", "households"))
```

Replace takes exactly two positional strings, FindText and ReplaceText. The target
is implicitly the parameter's readable current value. It replaces all
non-overlapping, case-sensitive literal matches, including occurrences within
quoted formula strings and longer identifiers. No regular expressions are used.
No match returns the current text unchanged. Empty FindText or a missing current
value is an application Error. Empty ReplaceText deletes matches; an empty or
whitespace-only result follows the normal parameter-removal rules.

Replace is permitted only in ModifyField, for every top-level text input except
the three target selectors. newCode is an editable text parameter and is included.
defaultValue must currently contain a scalar string; numbers, lists, and objects
are rejected without coercion. Enumerated scalar defaults are converted from
option IDs to labels before evaluation. translationField uses its readable field
code, and translationLanguage uses the comma-separated text form of its language
list; normal translation validation and ID resolution apply afterward. Calls
inside option rows, lookup rows, and other nested structures remain unsupported.

For example, a stored formula `oldid > 0` becomes `population > 0` before Replace
runs. After replacement, `households > 0` passes through the same existing resolver
as an ordinary literal edit and becomes `newid > 0`. Unresolved names retain their
spelling and produce the existing formula warnings. This happens during connected
command application; direct execute() returns the intermediate transformation,
like a name-based literal edit. Formula inputs use the pre-edit schema for Names
conversion; resolution uses the transformed schema. Neither conversion nor Replace
changes the original source schema.


## RegexReplace

`RegexReplace(FindPattern, ReplaceText)` has the same command/parameter permissions
as Replace, sharing its immutable permission sets. Its implementation calls
`re.sub(find_pattern, replace_text, context.current_value)` directly, with Python's
default global replacement behavior and no delimiter stripping or flags argument.
Inline flags and replacement-group references use standard Python regex semantics.
Empty FindPattern and missing current text are Errors, matching Replace's contract.
Pattern and replacement errors propagate through the shared function error handler,
so no edits are applied. Raw string literals work in both the creator and Text mode;
serialization preserves their values with ordinary escaped string literals.

## AppendFormula

`AppendFormula(Operator, Formula)` is limited to ModifyField's six top-level formula
inputs. Operator accepts case-insensitive AND/OR only. It parses Formula even if
the current value is absent/blank, then returns Formula directly for those empty
values. Otherwise, the standalone `formula_parser` package checks normalized
whole-expression equality or top-level inclusion under the requested operator.
If absent, the result is `(current) && (Formula)` or `(current) || (Formula)`.
Skipped appends preserve the readable current text exactly. Malformed/unsupported
formula syntax raises an atomic function error. Names conversion and subsequent
ID resolution are supplied by the existing shared framework. Formula arguments
use readable field codes. See `formula_parser/README.md` for the dependency-free
parse/normalize/includes API and supported grammar.

## Append

`Append(Position, Separator, AddText)` permits ModifyField's top-level non-formula
text inputs (Replace's permission set minus AppendFormula's). All three arguments
are positional strings. Position accepts case-insensitive Start/End. None or an
empty current string produces AddText directly. For a non-empty current string,
Start checks for AddText+Separator at the beginning, while End checks for
Separator+AddText at the end. Casefolded comparisons preserve original text on a
match; otherwise the exact boundary string is prepended/appended. No whitespace
is trimmed, and whitespace-only current text is non-empty. Separator/AddText may
be empty. Shared context, validation, serialization, and atomic errors apply.

## Alignment with commands

Commands use the same explicit catalog pattern. Each command module exports a
frozen COMMAND definition connecting its public name, parser, and creator metadata.
The COMMANDS tuple in commands/catalog.py is the single registration point;
COMMANDS_BY_NAME and COMMAND_CREATORS are derived from it. The catalog order
controls the UI menu. Adding a command requires no edits to page.py or the text
parser. Both catalogs use the shared index_definitions helper to create read-only,
case-insensitive indexes and reject duplicate names. Neither supports runtime
registration. Command and function definitions remain separate types because their
execution and metadata contracts differ.
