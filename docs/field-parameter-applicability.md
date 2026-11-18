# Field parameter applicability

Confirmed by the user in the normal ActivityInfo designer, 2026-11-17.
These UI checks take precedence over broader or incomplete API descriptions.
This is the shared applicability contract for AddField and the ModifyField creator.
It describes exposed settings, not every property the API can preserve.

## Common parameters

Y = editable/supported; - = not exposed; F = fixed true, checked and uneditable.
Validation includes both condition and message. Every type supporting Required
also supports Required condition (shown when Required is true).
Both hides means Hide from entry and Hide in table.

| Type/variant | Label | Code | Description | Key | Unique | Required | Entry hide | Table hide | Relevance | Validation | Reviewer | Read-only | Default value | Default formula | Translation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| FREE_TEXT, Barcode=false | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| FREE_TEXT, Barcode=true | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | - |
| NARRATIVE | Y | Y | Y | - | - | Y | Y | Y | Y | Y | Y | Y | Y | Y | - |
| quantity | Y | Y | Y | - | - | Y | Y | Y | Y | Y | Y | Y | Y | Y | - |
| date | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | - |
| month | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | - | - |
| epiweek (Week) | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | - | - |
| fortnight | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | - | - |
| enumerated, single | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | - | - |
| enumerated, multiple | Y | Y | Y | - | - | Y | Y | Y | Y | Y | Y | Y | Y | - | - |
| reference | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | - | - |
| reference, User | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | - | - |
| multiselectreference | Y | Y | Y | - | - | Y | Y | Y | Y | Y | Y | - | - | - | - |
| attachment | Y | Y | Y | - | - | Y | Y | - | Y | Y | Y | - | - | - | - |
| geopoint | Y | Y | Y | - | - | Y | Y | Y | Y | Y | Y | Y | Y | - | - |
| serial | Y | Y | Y | F | F | - | Y | Y | - | - | Y | - | - | - | - |
| calculated | Y | Y | Y | - | Y | - | Y | Y | Y | - | Y | - | - | - | - |
| reversereference | Y | Y | Y | - | - | - | - | Y | - | - | - | - | - | - | - |
| section | Y | - | Y | - | - | - | - | - | Y | - | - | - | - | - | - |
| note | Y | - | Y | - | - | - | - | - | Y | - | - | - | - | - | - |
| reference, Metadata | Y | Y | - | - | - | - | Y | Y | - | - | - | - | - | - | - |

Subform was not independently checked in this UI review. Retain the documented
baseline: label/code/description, Required/Required condition, both hides,
relevance, validation/message and Reviewer-only; no Key, Unique, Read-only,
defaults or Translation. Referenced form identifies the child form. Revisit if
the designer differs.

## Type-specific parameters

| Type/variant | Additional controls and fixed behaviour |
|---|---|
| FREE_TEXT | Text/Barcode variant selector, Input mask. Translation target and languages only for Text. |
| quantity | Units. |
| enumerated, single | Single/Multiple variant selector, label-only option rows. Presentation always `automatic`; no presentation control. |
| enumerated, multiple | Single/Multiple variant selector, label-only option rows. Present as dropdown toggle: off=`automatic`, on=`dropdown`. No `radio_button` choice. |
| reference | Referenced form; optional Lookup Fields (label/formula rows). |
| reference, User | References `<databaseId>@users`. No Lookup Fields. |
| reference, Metadata | Choose Added by / Last edited by; target, read-only behaviour and reserved identity are automatic. No ordinary reference settings. |
| multiselectreference | Referenced form only. No Lookup Fields. |
| attachment | Upload type; Allowed file types for FILE capture. Cardinality is `multiple`, without a control. |
| geopoint | Geo accuracy. Default value holds latitude and longitude. |
| serial | Prefix formula. Key and Unique fixed true. |
| calculated | Calculation formula. |
| reversereference | Referenced form and reference field being reversed. |
| section | Indentation level. |
| note, NARRATIVE, date/month/epiweek/fortnight | No additional type-specific controls. |

## Command conventions and implementation boundaries

- Shared definitions keep optional activation and no creation defaults. AddField
  supplies its own requirements/defaults; ModifyField uses explicit optional edits.
- AddField requires label and code where code applies; Note/Section omit code.
  Their preview IDs are deterministic pending identities, allocated on write.
- AddField explicitly defaults Required=false where applicable. The shared variant
  selector defines Barcode and enumerated cardinality; FREE_TEXT starts as Text.
  Options need labels only; calculations need a formula, references need their target, and included
  lookup rows need both label and formula. Translation needs target and languages.
- Reviewer-only applicability is confirmed, but its generated-client/API mapping
  remains unconfirmed. Applicable controls remain disabled until mapping exists.
- User is an explicit Reference variant; AddField automatically targets the current
  database virtual users form (`<databaseId>@users`) at application time.
- Creator edits omit inapplicable drafts. This map is not a blanket migration or
  prohibition on legacy properties in existing schemas or text commands.
- Attachment cardinality remains parseable for existing text commands; newly
  created fields default to multiple. Legacy presentation values remain parseable.
- API `required=false` can remain present even when Required is not exposed; an
  inherent read-only type does not need an editable Read-only checkbox.

## Shared type/variant selectors and ModifyField creation

MoveField, DeleteField, and ModifyField also expose Single field / Multiple fields
target mode. Multiple mode uses `fieldPattern` (whole-code regex) instead of
`fieldCode`; type/variant autofill applies only in single mode. Multiple mode
retains explicit type/variant selectors and validates each matched field against
them; incompatible matches warn and are skipped. Editable text
values, including nested option/lookup values, support `${field.name}` capture
substitution. Parameter types and applicability remain as documented below.

- Both creators use `fieldType` and conditional `fieldVariant` selectors:
  FREE_TEXT: `text` / `barcode`; enumerated: `single` / `multiple`;
  reference: `reference` / `user` / `metadata`. Other types have no variant.
- ModifyField requires `fieldCode` (code or fallback label), `fieldType`, and a
  variant where applicable. These identify the expected target, not changes.
  `newCode` and `label` are optional edits; `label` never identifies the target.
- Future matching: exact code first; a code match with a different type/variant
  warns and skips. Only when no code matches, try the supplied `fieldCode` as an exact
  label. Reject ambiguous matches. Note/Section use this same targeting argument.
- Selecting an existing ModifyField target populates its type and variant from
  the cached schema (exact code first, then an unambiguous exact label). Both
  selectors remain editable. Other edit values and rows are not populated.
- Excluded edits are omitted (unchanged). Included blank text/numbers/choices and
  empty collections produce explicit `None` (remove). Included unchecked booleans
  produce `False`. No separate Clear control or literal empty-string operation.
- Nested edits are partial: presentation can be supplied without option rows.
  Each included option row requires a label; each lookup row requires label and
  formula. Rows start blank, independent of any schema. Supplied collections replace the existing collection in the supplied order,
  matching exact labels and preserving matched IDs and unrelated row properties.
- Type, variant, reference targets, reverse-reference target fields and metadata
  kind cannot be edited. Serial's invariant Key/Unique are not ModifyField edits.
- Required condition and translation components can be edited independently of
  their parent setting, since the target's existing settings are unknown.
- ModifyField applies atomically to a copy of the schema. A missing target is a
  warning no-op; type/variant mismatches also warn and skip. Ambiguous targets and invalid edits are Errors.
- AddField still accepts legacy text commands with option codes and implicit
  variants. New creator rows expose labels only, and option IDs are internal.

Example:

```python
ModifyField(fieldCode="population", fieldType="quantity", newCode="total", description=None, required=False, units="people")
```

## ModifyField execution

- Code matching has priority. An exact code match with a different type/variant
  warns and skips, with no label fallback. Only absent code matches permit exact
  label matching filtered by type/variant. Ambiguous matches are Errors; no match
  is a warning no-op. Code and label matching is case-sensitive.
- Variant detection uses Barcode=true, enumerated cardinality (legacy omission
  means single), and reference metadata/user-range markers. Metadata has priority
  over User when identifying reference variants.
- Only included properties are changed. `None` removes a property, including
  visibility properties; booleans map to their proper wire values. Nested edits
  merge individual properties. Field ID, position and unrelated/unknown properties
  are preserved. `newCode` must not duplicate another field's code.
- Options and Lookup Fields are complete replacements when supplied. Match exact
  `label` / `lookupLabel`; preserve matched IDs, update requested content, omit
  unrequested rows and follow requested ordering. New rows get deterministic,
  collision-free pending IDs, allocated only at write time. AddField and ModifyField
  share one row-ID helper: `pending-field:REF1` owns `pending-lookup:REF1:N`
  / `pending-option:REF1:N`; removed slots remain reserved during replacement. Duplicate requested
  labels or ambiguous existing matches are Errors. Renaming a row creates a new
  identity; this command has no identity-preserving row rename operation.
- `lookupFields=None` removes custom lookups. Removing the option list or all
  options fails because an enumerated field needs options. Removing an option
  referenced by the default fails unless the same command changes/clears that
  default. Unchanged collection definitions and unrelated row metadata survive.
- Validate applicability even for text commands; disabled Reviewer-only is still
  rejected. Required labels/formulas/options cannot be cleared. Validate numeric
  defaults/accuracy/indentation, attachment capture restrictions, Required with
  entry visibility/required condition, and complete translation configurations.
- Translation components merge with existing configuration; False/None removes
  the configuration. Defaults, formulas and translation references then pass
  through the existing resolution pipeline, without commands making API calls.
- Failures return the original schema copy. The application runner may retain
  candidates with resolution diagnostics according to its existing review model.
- Field creation uses a shared allocator with collision checks and immediate
  reservation in the same identity set as options/lookups. Code-less Note/Section
  fields use that allocator too. ModifyField never allocates a replacement field
  ID on rename. Pending field and row IDs survive code renames/removal. Reusing a renamed code
  in a later AddField allocates a distinct pending namespace. Write preparation
  maps pending formula references even when their field no longer has a code,
  preserving quoted literals.

## Research references

- [API schema](https://www.activityinfo.org/support/docs/api/reference/getFormSchema.html)
- [Key](https://www.activityinfo.org/support/docs/forms/key-field.html), [Unique](https://www.activityinfo.org/support/docs/forms/unique.html)
- [Defaults](https://www.activityinfo.org/support/docs/forms/default-value.html), [Read-only](https://www.activityinfo.org/support/docs/forms/read-only.html)
- [Validation](https://www.activityinfo.org/support/docs/forms/validation-rules.html), [Required](https://www.activityinfo.org/support/docs/forms/required.html)
- [Reviewer-only](https://www.activityinfo.org/support/docs/forms/reviewer-only.html), [Metadata](https://www.activityinfo.org/support/docs/forms/metadata-field.html)

Where these sources differ from the user-confirmed matrix above (for example,
Reference uniqueness or Translation), use the matrix.
