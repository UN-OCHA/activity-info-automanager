"""Shared field controls and argument names, independent of command requirements.

All controls start optional and without creation defaults. Commands configure
their own requirements/defaults with dotted-path overrides, including row fields.
"""

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from activity_info_api_client.models.form_schema_element_parameter_capture_methods_item import (
    FormSchemaElementParameterCaptureMethodsItem,
)
from activity_info_api_client.models.form_schema_element_parameter_file_types_item import (
    FormSchemaElementParameterFileTypesItem,
)
from activity_info_api_client.models.form_schema_element_type import FormSchemaElementType

from .command_creator import ParameterDefinition as P
from .schema_identity import field_code_error


FIELD_TYPES = tuple(item.value for item in FormSchemaElementType)
FIELD_TYPES_BY_CASE = {value.casefold(): value for value in FIELD_TYPES}
UPLOAD_TYPES = tuple(item.value for item in FormSchemaElementParameterCaptureMethodsItem)
FILE_TYPES = tuple(item.value for item in FormSchemaElementParameterFileTypesItem)


def field_type_error(value):
    if not isinstance(value, str) or value.casefold() not in FIELD_TYPES_BY_CASE:
        return 'Field type must be one of the listed ActivityInfo field types.'
    return None


def for_types(*types):
    accepted = {value.casefold() for value in types}
    return lambda values: str(values.get('field_type', '')).casefold() in accepted


# UI-confirmed profiles; see docs/field-parameter-applicability.md.
INPUT_TYPES = frozenset(('FREE_TEXT', 'NARRATIVE', 'quantity', 'date', 'month',
                         'epiweek', 'fortnight', 'enumerated', 'reference',
                         'multiselectreference', 'attachment', 'geopoint', 'subform'))
KEY_TYPES = frozenset(('FREE_TEXT', 'date', 'month', 'epiweek', 'fortnight', 'reference', 'enumerated'))
DEFAULT_TYPES = frozenset(('FREE_TEXT', 'NARRATIVE', 'quantity', 'date', 'month',
                           'epiweek', 'fortnight', 'enumerated', 'reference', 'geopoint'))
READ_ONLY_TYPES = DEFAULT_TYPES
DEFAULT_FORMULA_TYPES = frozenset(('FREE_TEXT', 'NARRATIVE', 'quantity', 'date'))


FIELD_VARIANTS = {
    'free_text': ('text', 'barcode'),
    'enumerated': ('single', 'multiple'),
    'reference': ('reference', 'user', 'metadata'),
}


def variants_for(values):
    return FIELD_VARIANTS.get(str(values.get('field_type') or '').casefold(), ())


def variant_required(values):
    return bool(variants_for(values))


def metadata_enabled(values):
    return for_types('reference')(values) and (values.get('field_variant') == 'metadata' or values.get('metadata') in ('ADDED_BY', 'LAST_EDITED_BY'))


def user_reference(values):
    # User is a reference variant, represented by the database's virtual users form.
    return for_types('reference')(values) and (values.get('field_variant') == 'user' or str(values.get('referenced_form') or '').endswith('@users'))


def parameter_applies(key, values):
    """Shared visibility policy, independent of Add/Modify requirements/defaults."""
    kind = str(values.get('field_type') or '').casefold()
    if not kind:
        return True
    if key in ('field_type', 'label'):
        return True
    if key == 'metadata':
        return kind == 'reference' and values.get('field_variant') == 'metadata'
    if metadata_enabled(values):
        return key in ('field_code', 'hide_from_entry', 'hide_in_table')
    if key == 'field_code':
        return kind not in ('section', 'note')
    if key == 'description':
        return True
    if key in ('key', 'unique'):
        types = KEY_TYPES | ({'calculated'} if key == 'unique' else set())
        return kind == 'serial' or kind in {item.casefold() for item in types} and not (
            kind == 'enumerated' and (values.get('field_variant') == 'multiple' or (values.get('option_list') or {}).get('cardinality') == 'multiple'))
    if key in ('required', 'required_condition', 'validation_condition', 'validation_message'):
        supported = kind in {item.casefold() for item in INPUT_TYPES}
        return supported and (key != 'required_condition' or values.get('required') is True)
    if key == 'relevance_condition':
        return kind not in ('serial', 'reversereference')
    if key == 'hide_from_entry':
        return kind not in ('section', 'note', 'reversereference')
    if key == 'hide_in_table':
        return kind not in ('section', 'note', 'attachment')
    if key == 'reviewer_only':
        return kind not in ('section', 'note', 'reversereference')
    if key == 'read_only':
        return kind in {item.casefold() for item in READ_ONLY_TYPES}
    if key == 'default_value':
        return kind in {item.casefold() for item in DEFAULT_TYPES}
    if key == 'default_formula':
        return kind in {item.casefold() for item in DEFAULT_FORMULA_TYPES}
    if key in ('translation', 'translation_field', 'translation_language'):
        return kind == 'free_text' and values.get('field_variant') != 'barcode' and values.get('barcode') is not True and (
            key == 'translation' or values.get('translation') is True)
    if key == 'referenced_form':
        return kind in ('reference', 'multiselectreference', 'subform', 'reversereference') and not user_reference(values)
    if key == 'lookup_fields':
        return kind == 'reference' and not user_reference(values)
    return True


def applies(key):
    return lambda values: parameter_applies(key, values)


def translation_enabled(values):
    return parameter_applies('translation_field', values)


def referenced_form_visible(values):
    return parameter_applies('referenced_form', values)


def serial_flag(values):
    return True if for_types('serial')(values) else None


FIELD_PARAMETERS = (
    P('field_type', 'Field type', 'combobox', options=FIELD_TYPES, validate=field_type_error),
    P('field_variant', 'Field variant', 'select', options=('text', 'barcode', 'single', 'multiple', 'reference', 'user', 'metadata'), options_when=variants_for, visible_when=variant_required),
    P('field_code', 'Field code', 'text', validate=field_code_error),
    P('label', 'Field label', 'text'),
    P('description', 'Description', 'text'),
    P('key', 'Key', 'boolean'),
    P('unique', 'Unique', 'boolean'),
    P('required', 'Required', 'boolean'),
    P('relevance_condition', 'Relevance condition', 'combobox', multiline=True, option_source='field_codes'),
    P('required_condition', 'Required condition', 'combobox', multiline=True, option_source='field_codes',
      visible_when=applies('required_condition')),
    P('validation_condition', 'Validation condition', 'combobox', multiline=True, option_source='field_codes'),
    P('validation_message', 'Validation message', 'text'),
    P('hide_from_entry', 'Hide from entry', 'boolean'),
    P('hide_in_table', 'Hide in table', 'boolean'),
    P('reviewer_only', 'Reviewer only', 'boolean', enabled=False,
      help='Disabled until the API mapping is confirmed in the generated client.'),
    P('read_only', 'Read only', 'boolean'),
    P('translation', 'Translation', 'boolean', visible_when=applies('translation')),
    P('translation_field', 'Translation field', 'combobox', option_source='field_codes',
      visible_when=translation_enabled),
    P('translation_language', 'Translation language', 'combobox', options=('en', 'fr', 'es', 'ar', 'de', 'pt'),
      visible_when=translation_enabled,
      help='Language codes; separate multiple languages with commas.'),
    P('indentation_level', 'Indentation level', 'text', value_type='integer', visible_when=for_types('section')),
    P('prefix_formula', 'Prefix formula', 'combobox', multiline=True,
      option_source='field_codes', visible_when=for_types('serial')),
    P('input_mask', 'Input mask', 'text', visible_when=for_types('FREE_TEXT')),
    P('barcode', 'Barcode', 'boolean', visible_when=for_types('FREE_TEXT')),
    P('calculation_formula', 'Calculation formula', 'combobox', multiline=True, option_source='field_codes',
      visible_when=for_types('calculated')),
    P('metadata', 'Added/last-edited by', 'single_select', options=('ADDED_BY', 'LAST_EDITED_BY'),
      option_labels={'ADDED_BY': 'Added by', 'LAST_EDITED_BY': 'Last edited by'},
      visible_when=applies('metadata'),
      help='Server-maintained author field; the user reference, read-only flag, and reserved ID are set automatically.'),
    P('units', 'Units', 'text', visible_when=for_types('quantity')),
    P('upload_type', 'Upload type', 'multi_select', options=UPLOAD_TYPES, visible_when=for_types('attachment')),
    P('attachment_cardinality', 'Attachment cardinality', 'single_select',
      options=('single', 'multiple'), option_labels={'single': 'Single', 'multiple': 'Multiple'},
      visible_when=lambda values: False),
    P('allowed_file_types', 'Allowed file types', 'multi_select', options=FILE_TYPES,
      visible_when=lambda values: for_types('attachment')(values)
      and (values.get('upload_type') is None or 'FILE' in values.get('upload_type', []))),
    P('geo_accuracy', 'Geo accuracy (meters)', 'text', value_type='number', visible_when=for_types('geopoint')),
    P('option_list', 'Option list', 'group', visible_when=for_types('enumerated'),
      children=(
          P('cardinality', 'Cardinality', 'single_select', options=('single', 'multiple')),
          P('presentation', 'Present as dropdown', 'toggle', options=('automatic', 'dropdown'),
            option_labels={'automatic': 'Off', 'dropdown': 'On'},
            visible_when=lambda values: values.get('field_variant') == 'multiple' or values.get('cardinality') == 'multiple'),
          P('values', 'Options', 'repeat', inline_rows=True, children=(
              P('label', 'Option label', 'text'),
          )),
      )),
    P('referenced_form', 'Referenced form', 'combobox', option_source='form_ids',
      visible_when=referenced_form_visible,
      help='Form ID or local form name; use Database/Form for a remote form. Names resolve when the command is applied.'),
    P('reverse_reference_field', 'Reverse-reference field', 'combobox', option_source='reference_field_codes',
      visible_when=for_types('reversereference'),
      validate=field_code_error),
    P('lookup_fields', 'Lookup Fields', 'repeat', inline_rows=True,
      visible_when=applies('lookup_fields'), children=(
        P('label', 'Lookup label', 'text'),
        P('formula', 'Lookup formula', 'combobox', option_source='reference_field_codes',
          help='Field code or formula on the referenced form.'),
    )),
    P('default_value', 'Default value', 'text',
      visible_when=applies('default_value'),
      help='Text for text/date fields; a number for quantity; JSON coordinates for geopoint; '
           'option codes or labels for selection (a JSON list for multiple selection); '
           'formId:recordId or record labels for references.'),
    P('default_formula', 'Default formula', 'combobox', multiline=True, option_source='field_codes',
      visible_when=applies('default_formula')),
)

_COMMON_CONTROLS = {
    'field_code', 'description', 'key', 'unique', 'required', 'relevance_condition',
    'validation_condition', 'validation_message', 'hide_from_entry', 'hide_in_table',
    'reviewer_only', 'read_only',
}
FIELD_PARAMETERS = tuple(
    replace(parameter, visible_when=applies(parameter.key),
            fixed_when=serial_flag if parameter.key in ('key', 'unique') else None)
    if parameter.key in _COMMON_CONTROLS else parameter
    for parameter in FIELD_PARAMETERS
)

# Order follows the editor; these are command names, not necessarily wire keys.
FIELD_ARGUMENT_NAMES = (
    'fieldType', 'fieldVariant', 'fieldCode', 'label', 'description', 'key', 'unique', 'required',
    'relevanceCondition', 'requiredCondition', 'validationCondition', 'validationMessage',
    'hideFromEntry', 'hideInTable', 'reviewerOnly', 'readOnly', 'translation',
    'translationField', 'translationLanguage', 'indentationLevel', 'prefixFormula',
    'inputMask', 'barcode', 'calculationFormula', 'metadata', 'units', 'uploadType', 'attachmentCardinality',
    'allowedFileTypes', 'geoAccuracy', 'optionList', 'referencedForm', 'reverseReferenceField', 'lookupFields',
    'defaultValue', 'defaultFormula',
)
FIELD_ARGUMENTS = dict(zip((parameter.key for parameter in FIELD_PARAMETERS), FIELD_ARGUMENT_NAMES, strict=True))


def configure_field_parameters(overrides: Mapping[str, Mapping[str, Any]]) -> tuple[P, ...]:
    """Return independent definitions with command-specific dotted-path overrides.

    For example, 'option_list.values.label' addresses a field within each option
    row. Unknown paths fail so requirement typos cannot silently weaken validation.
    The shared catalog and definitions returned by other calls are not mutated.
    """
    matched = set()

    def configure(parameter, parent=''):
        path = f'{parent}.{parameter.key}' if parent else parameter.key
        children = tuple(configure(child, path) for child in parameter.children)
        if path in overrides:
            matched.add(path)
        return replace(parameter, children=children, **overrides.get(path, {}))

    parameters = tuple(configure(parameter) for parameter in FIELD_PARAMETERS)
    unknown = set(overrides) - matched
    if unknown:
        raise ValueError(f'Unknown field parameter paths: {", ".join(sorted(unknown))}')
    return parameters
