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


def translation_enabled(values):
    return for_types('FREE_TEXT', 'NARRATIVE', 'calculated')(values) and values.get('translation') is True


def metadata_enabled(values):
    return for_types('reference')(values) and values.get('metadata') in ('ADDED_BY', 'LAST_EDITED_BY')


def referenced_form_visible(values):
    return for_types('reference', 'multiselectreference', 'subform', 'reversereference')(values) and not metadata_enabled(values)


FIELD_PARAMETERS = (
    P('field_type', 'Field type', 'combobox', options=FIELD_TYPES, validate=field_type_error),
    P('field_code', 'Field code', 'text', validate=field_code_error),
    P('label', 'Field label', 'text'),
    P('description', 'Description', 'text'),
    P('key', 'Key', 'boolean'),
    P('unique', 'Unique', 'boolean'),
    P('required', 'Required', 'boolean'),
    P('relevance_condition', 'Relevance condition', 'combobox', multiline=True, option_source='field_codes'),
    P('required_condition', 'Required condition', 'combobox', multiline=True, option_source='field_codes',
      visible_when=('required', (True,))),
    P('validation_condition', 'Validation condition', 'combobox', multiline=True, option_source='field_codes'),
    P('validation_message', 'Validation message', 'text'),
    P('hide_from_entry', 'Hide from entry', 'boolean'),
    P('hide_in_table', 'Hide in table', 'boolean'),
    P('reviewer_only', 'Reviewer only', 'boolean', enabled=False,
      help='Not described in the v2.0.2 schema. Disabled until the API mapping is confirmed.'),
    P('read_only', 'Read only', 'boolean'),
    P('translation', 'Translation', 'boolean', visible_when=for_types('FREE_TEXT', 'NARRATIVE', 'calculated')),
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
      visible_when=for_types('reference'),
      help='Server-maintained author field; the user reference, read-only flag, and reserved ID are set automatically.'),
    P('units', 'Units', 'text', visible_when=for_types('quantity')),
    P('upload_type', 'Upload type', 'multi_select', options=UPLOAD_TYPES, visible_when=for_types('attachment')),
    P('attachment_cardinality', 'Attachment cardinality', 'single_select',
      options=('single', 'multiple'), option_labels={'single': 'Single', 'multiple': 'Multiple'},
      visible_when=for_types('attachment')),
    P('allowed_file_types', 'Allowed file types', 'multi_select', options=FILE_TYPES,
      visible_when=lambda values: for_types('attachment')(values)
      and (values.get('upload_type') is None or 'FILE' in values.get('upload_type', []))),
    P('geo_accuracy', 'Geo accuracy (meters)', 'text', value_type='number', visible_when=for_types('geopoint')),
    P('option_list', 'Option list', 'group', visible_when=for_types('enumerated'),
      children=(
          P('cardinality', 'Cardinality', 'single_select', options=('single', 'multiple')),
          P('presentation', 'Presentation', 'single_select', options=('automatic', 'radio_button', 'dropdown')),
          P('values', 'Options', 'repeat', children=(
              P('code', 'Option code', 'text', validate=field_code_error),
              P('label', 'Option label', 'text'),
          )),
      )),
    P('referenced_form', 'Referenced form', 'combobox', option_source='form_ids',
      visible_when=referenced_form_visible,
      help='Existing form ID, or <databaseId>@users for a user reference.'),
    P('reverse_reference_field', 'Reverse-reference field', 'combobox', option_source='reference_field_codes',
      visible_when=for_types('reversereference'),
      validate=field_code_error),
    P('lookup_fields', 'Lookup Fields', 'repeat', inline_rows=True,
      visible_when=lambda values: for_types('reference')(values) and not metadata_enabled(values), children=(
        P('label', 'Lookup label', 'text'),
        P('formula', 'Lookup formula', 'combobox', option_source='reference_field_codes',
          help='Field code or formula on the referenced form.'),
    )),
    P('default_value', 'Default value', 'text',
      visible_when=lambda values: not metadata_enabled(values),
      help='Text for text/date fields; a number for quantity; JSON coordinates for geopoint; '
           'option codes for selection (a JSON list for multiple selection); record IDs for references.'),
    P('default_formula', 'Default formula', 'combobox', multiline=True, option_source='field_codes',
      visible_when=lambda values: not metadata_enabled(values)),
)

# Order follows the editor; these are command names, not necessarily wire keys.
FIELD_ARGUMENT_NAMES = (
    'fieldType', 'fieldCode', 'label', 'description', 'key', 'unique', 'required',
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

    For example, 'option_list.values.code' addresses a field within each option
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
