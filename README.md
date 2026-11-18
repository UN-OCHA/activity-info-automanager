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
| `WithForm` | Selects the active form and database for subsequent commands. |

### Multiple forms

The initial database and form selectors establish the starting context.
Single-target `WithForm` takes a required `form` parameter, using the same selection rules as
referenced forms and variable lookups:

```python
AddField(fieldCode="new", label="New field")
WithForm(form="Other database/Second form")
ModifyField(fieldCode="total", fieldType="calculated",
            calculationFormula="SOURCE.new")
WithForm(form="Original database/First form")
DeleteField(fieldCode="old")
```

Unqualified form names resolve in the active form's database. Names are exact and
case-sensitive and must resolve uniquely; form IDs are also accepted using the
existing shared resolver. `form` supports `${vars.name}` substitutions. Variables assigned outside a multiple-form scope remain available across switches.
For multiple forms, use `WithForm(formPattern=r"Site (?P<region>.*)")`.
The pattern matches whole form names in the active database only, in name order
(case-insensitive sort, then ID). Matches, captures and one-based indices are frozen
when WithForm is reached. Duplicate names select separate form IDs. A new
WithForm replaces the scope; zero matches warn and skip its following applications.

Following commands apply sequentially to each matched form: A1, B1, A2, B2.
An error stops remaining forms and later commands. Each authored command keeps
one card, with a count of concrete applications and combined console messages.
Expand upward to inspect each application, labeled with its form and field context.
For two fields in A and three in B, the count is 5x, ordered A/i, A/ii,
B/i, B/ii, B/iii. WithForm and single commands inside its scope also expand,
with one child per form. Selecting a child shows all form tabs at that exact
application snapshot and selects the affected form. Fixed schema slots accumulate edits within
the scope, and execution writes those final schemas in slot order. Earlier visits
and command previews remain frozen.

Variables set before the scope are inherited independently by each form.
SetVariable inside the scope changes only that form's values. On leaving the scope,
any variable name assigned inside it becomes unavailable, including an inherited
name that was reassigned. Other inherited names remain available.

Each successful switch to a different form appends a schema snapshot, loading a
new form only when the switch is reached. Revisiting a form copies its latest
local version. Selecting the active form again is a no-op. Cross-form schema
references use the latest local versions, ahead of cached online schemas. Variable
lookups still query online record data.

Result tabs run vertically down the left, with one tab per distinct form. Selecting
a command shows the forms and latest snapshots reached at that point, and selects
that command's active form. Revisits reuse the existing tab.

Execute checks every participating form for external changes before writing, then
checks again before each update. It writes snapshots in visit order, skips
unchanged snapshots, and verifies every write. A failed step stops execution and
offers **Leave**, **Roll back last update**, or **Roll back all updates**. Rollback
restores saved pre-update schemas in reverse order, verifying each restoration;
the first rollback failure stops recovery. Rollback submits original field IDs
through the normal schema endpoint. Restoration of deleted field data through
this mechanism has not yet been established against the live API.

### SetVariable

`SetVariable` stores text for later `${vars.name}` substitutions. Names are
case-sensitive and must match `[A-Za-z_][A-Za-z0-9_]*`. Reassignment replaces the
previous value only after the new value is valid. Each command-list evaluation
starts with no variables; reordering or deleting commands recalculates them.

```python
SetVariable(name="year", method="literal", value="2027")
SetVariable(name="threshold", method="lookup",
            referencedForm="Configuration/Targets",
            filter="YEAR == ${vars.year}", returnFormula="TARGET * 2")
ModifyField(fieldCode="amount", fieldType="quantity",
            validationCondition="amount <= ${vars.threshold}")
```

Literal values are stored as text; numeric and boolean literals are also accepted
and converted to text (`0` becomes `"0"`, `False` becomes `"False"`). Lookup uses
the same form ID, local form name, or `Database/Form` selection as AddField.
`filter` and `returnFormula` are ActivityInfo formulas evaluated by the POST query
endpoint, after variable substitution. `returnFormula` is used because `return`
is reserved in the command syntax. Formula text is sent directly to ActivityInfo.

A lookup uses the first record in API response order. Multiple matches produce a
warning and later commands continue; no stable ordering is implied. No records,
a missing/null value, empty text, or whitespace-only text produce an error and
stop the chain. The same blank-value rule applies to literals, including after
substitution. Zero and false are valid; nonblank text retains its whitespace.
Lookup/API/form-resolution errors stop the chain without replacing a prior value.

`ModifyField` identifies its target by exact field code, then falls back to an exact
label with the specified type and variant. `MoveField` and `DeleteField` identify
fields by code. Missing targets produce a warning and leave the schema unchanged.

### Multiple fields and variables

MoveField, DeleteField, and ModifyField have a **Target** selector below the
command selector: **Single** or **Multiple** (vertical radio buttons). Multiple mode accepts
a Python regex matching the whole field code. It does not match labels. In Text
mode, supply `fieldPattern` instead of `fieldCode`:

```python
ModifyField(
    fieldPattern=r"cost_(?P<year>\d{4})",
    fieldType="quantity",
    newCode="budget_${field.year}",
    label="Budget for ${field.year}",
)
MoveField(fieldPattern=r"budget_\d{4}", position="Beginning")
```

Matches are collected once from the incoming schema and applied in their original
schema order. Renames and moves do not change that selection. ModifyField still
checks the specified type/variant for each target; an incompatible field produces
a warning and is skipped. Its single-field type/variant autofill and manual overrides remain
available. In multiple mode, choose type/variant explicitly.

MoveField places the first match at the authored destination, then places each
subsequent match immediately after the previous one, preserving match order.
The later applications use that previous field as their destination.

Text values accept direct `${...}` substitution, including nested option/lookup
values and parameter-function string arguments:

| Reference | Value |
| --- | --- |
| `${field.code}` | Original matched code |
| `${field.year}` | Named capture group `year` |
| `${field.1}` | Numbered capture group, starting at 1 |
| `${field.index}` | Iteration number, starting at 1 |
| `${vars.prefix}` | Text assigned by an earlier SetVariable command |
| `${form.name}` | Original matched form name |
| `${form.region}` / `${form.1}` | Named / numbered form capture |
| `${form.index}` | Form iteration number, starting at 1 |
| `${database.*}` | Reserved for future database iteration; unavailable currently |

`field` provides the current field iteration context; `vars` contains only values
assigned by SetVariable. The `form` namespace is available inside multiple-form scopes; `database`
remains reserved. The former `match.*` and `iteration.index`
references are no longer supported.

The capture names `code` and `index` are reserved. Write `$${field.year}` to insert the literal
`${field.year}`. Ordinary dollar signs and braces are unchanged. Undefined
references, unmatched optional capture groups, and malformed placeholders are
errors. Substitution runs once, before parameter functions and schema resolution;
inserted values and function outputs are never substituted again. It inserts
text without evaluating expressions, quoting formula literals, or converting
text to numeric/boolean values. Existing parameter types and selection choices
still apply. Target selectors (`fieldCode` and `fieldPattern`) support
`${vars.name}` and enclosing `${form.name}` / capture substitutions, resolved before selecting fields. `field` references in selectors are errors because that context does not yet
exist. Pattern variables are inserted as regex text without automatic escaping;
the resolved pattern is then compiled and matched. Use `$${...}` for literal
placeholder text. Selectors and inserted values are never substituted twice.
Iteration variables are local to their application, so using `${field.year}` in a single-field command is an error.

The List view keeps one parent command with an iteration count and an expansion
button. Expand it to see smaller iteration boxes above the parent, each with its
own status. Select a child for its interim schema and console; select the parent
for the final reached schema and combined, iteration-prefixed console messages.
Collapsing a selected child selects the parent and updates the result accordingly.
Reorder/delete controls operate on the entire authored command. Text mode stores
the original pattern and placeholders, not the expanded applications.

Field targets are planned across all selected forms before application. The
count includes unexecuted applications after an error. A form with zero field
matches contributes no child, while its warning remains on the parent and in
the combined console. Zero matches produces a warning and leaves the schema unchanged. Warnings allow
later iterations to continue. An error stops the remaining iterations and later
commands and disables Execute. Earlier successful changes remain visible for
inspection; the failed iteration's result follows the normal execution pipeline.
No preview changes are written to ActivityInfo until Execute is confirmed.

Name resolution is optional for display: an existing form ID whose schema cannot
be read stays unchanged, with a warning and highlighting on the reference. Failed
record-label reads likewise retain the record ID. Unresolved formula references
remain in the formula with warnings and may be submitted to the API. Other
name-to-ID conversions (referenced forms, record defaults, translation/reverse
targets, and option defaults) must resolve before execution can proceed. Existing
form IDs are recognized from the loaded source schema; a new, unresolved name is
not accepted merely because it resembles an ID.

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
not count as a field. Spacing is exact and is not trimmed; whitespace-only current
text counts as non-empty.

For example, `"Notes - REVIEWED"` remains unchanged with the call above, while
`"Notes Reviewed"` becomes `"Notes Reviewed - Reviewed"` because the separator
does not field. Invalid positions cause an error without applying the command.
Formula inputs use `AppendFormula` instead. Normal text-value validation and
parameter-removal rules still apply.

## Running locally

```powershell
uv sync --locked
uv run --locked python main.py
```

Run offline checks with `uv run --locked python -m unittest discover -s test -t . -v`.
