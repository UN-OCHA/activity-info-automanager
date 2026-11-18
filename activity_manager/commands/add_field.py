import ast
import json
import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..command_execution import ExecutionResult
from ..command_creator import CommandCreatorDefinition
from ..command_parser import command_error, parse_literal_parameters
from ..schema_identity import field_code_error, allocate_pending_field_id, pending_row_id, schema_identities
from ..field_parameters import (
    FIELD_ARGUMENTS, FIELD_VARIANTS, FIELD_TYPES_BY_CASE, FILE_TYPES, UPLOAD_TYPES, field_type_error,
)
from .add_field_parameters import ADD_FIELD_PARAMETERS


ARGUMENTS = {
    'field_type': FIELD_ARGUMENTS['field_type'],
    'position': 'position',
    'before_after_field': 'beforeAfterField',
    **{key: value for key, value in FIELD_ARGUMENTS.items() if key != 'field_type'},
}


class FieldParameterError(ValueError):
    def __init__(self, parameter, message):
        self.parameter = parameter
        super().__init__(message)


def fail(parameter, message):
    raise FieldParameterError(parameter, message)


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def literal_syntax(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return '[' + ', '.join(literal_syntax(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(f'{literal_syntax(key)}: {literal_syntax(item)}'
                               for key, item in value.items()) + '}'
    return repr(value)


@dataclass(frozen=True)
class AddFieldCommand:
    field_code: str | None = None
    label: str = ''
    field_type: str = 'FREE_TEXT'
    key: bool | None = None
    unique: bool | None = None
    required: bool | None = None
    relevance_condition: str | None = None
    required_condition: str | None = None
    validation_condition: str | None = None
    validation_message: str | None = None
    description: str | None = None
    hide_from_entry: bool | None = None
    hide_in_table: bool | None = None
    reviewer_only: bool | None = None
    read_only: bool | None = None
    translation: bool | None = None
    translation_field: str | None = None
    translation_language: str | list[str] | None = None
    indentation_level: int | None = None
    prefix_formula: str | None = None
    input_mask: str | None = None
    barcode: bool | None = None
    calculation_formula: str | None = None
    metadata: str | None = None
    units: str | None = None
    upload_type: list[str] | None = None
    attachment_cardinality: str | None = None
    allowed_file_types: list[str] | None = None
    geo_accuracy: float | None = None
    option_list: dict | None = None
    referenced_form: str | None = None
    reverse_reference_field: str | None = None
    lookup_fields: list[dict] | None = None
    default_value: Any = None
    default_formula: str | None = None
    position: str | None = None
    before_after_field: str | None = None
    field_variant: str | None = None

    def __post_init__(self):
        # Creation parses values only. Cross-parameter and schema checks belong
        # to execute(), on a copy, so UI edits never apply a command.
        if isinstance(self.field_type, str):
            object.__setattr__(self, 'field_type', FIELD_TYPES_BY_CASE.get(self.field_type.casefold(), self.field_type))
        if isinstance(self.position, str):
            positions = {value.casefold(): value for value in ('Beginning', 'End', 'Before field', 'After field')}
            object.__setattr__(self, 'position', positions.get(self.position.casefold(), self.position))
        if isinstance(self.translation_language, str):
            object.__setattr__(self, 'translation_language', [value.strip() for value in self.translation_language.split(',')])
        if self.field_variant is not None:
            if self.field_type == 'FREE_TEXT':
                object.__setattr__(self, 'barcode', self.field_variant == 'barcode')
            elif self.field_type == 'enumerated':
                options = deepcopy(self.option_list or {})
                options['cardinality'] = self.field_variant
                if self.field_variant == 'single':
                    options['presentation'] = 'automatic'
                else:
                    options.setdefault('presentation', 'automatic')
                object.__setattr__(self, 'option_list', options)
        value = self.default_value
        if isinstance(value, str):
            try:
                if self.field_type == 'quantity':
                    number = float(value)
                    if math.isfinite(number):
                        value = number
                elif self.field_type in ('geopoint', 'multiselectreference') or (
                    self.field_type == 'enumerated' and isinstance(self.option_list, dict)
                    and self.option_list.get('cardinality') == 'multiple'
                ):
                    value = json.loads(value)
            except (ValueError, TypeError):
                pass  # Application reports an invalid value as a diagnostic.
            object.__setattr__(self, 'default_value', value)
        for name in ARGUMENTS:
            value = getattr(self, name)
            if isinstance(value, (dict, list)):
                object.__setattr__(self, name, deepcopy(value))

    def _validate(self):
        if self.position is not None:
            positions = {value.casefold(): value for value in ('Beginning', 'End', 'Before field', 'After field')}
            if not isinstance(self.position, str) or self.position.casefold() not in positions:
                fail('position', f'AddField has an invalid position: {self.position!r}.')
            object.__setattr__(self, 'position', positions[self.position.casefold()])
        if self.position in ('Before field', 'After field'):
            if not isinstance(self.before_after_field, str) or not self.before_after_field.strip():
                fail('before_after_field', 'AddField requires beforeAfterField for this position.')
            if self.before_after_field == self.field_code:
                fail('before_after_field', 'AddField cannot position a field relative to itself.')
        elif self.before_after_field is not None:
            fail('before_after_field', 'AddField does not use beforeAfterField for this position.')
        if self.field_variant is not None and self.field_variant not in FIELD_VARIANTS.get(str(self.field_type).casefold(), ()):
            fail('field_variant', 'Invalid field variant for this field type.')
        error = field_type_error(self.field_type)
        if error:
            fail('field_type', error)
        object.__setattr__(self, 'field_type', FIELD_TYPES_BY_CASE[self.field_type.casefold()])
        error = (None if self.field_code is None and self.field_type in ('section', 'note')
                 else field_code_error(self.field_code))
        if error:
            fail('field_code', error)
        if not isinstance(self.label, str) or not self.label.strip():
            fail('label', 'Field label is required.')
        bools = ('key', 'unique', 'required', 'hide_from_entry', 'hide_in_table',
                 'reviewer_only', 'read_only', 'translation', 'barcode')
        texts = ('description', 'translation_field', 'prefix_formula', 'input_mask',
                 'calculation_formula', 'metadata', 'units', 'referenced_form', 'default_formula',
                 'relevance_condition', 'required_condition', 'validation_condition', 'validation_message',
                 'reverse_reference_field', 'attachment_cardinality')
        for name in bools + texts:
            value = getattr(self, name)
            expected = bool if name in bools else str
            if value is not None and type(value) is not expected:
                fail(name, f'{ARGUMENTS[name]} must be a {expected.__name__}.')
        if self.reviewer_only is not None:
            fail('reviewer_only', 'Reviewer only is disabled until its API mapping is confirmed.')
        applicable = {
            'translation': ('FREE_TEXT', 'NARRATIVE', 'calculated'),
            'translation_field': ('FREE_TEXT', 'NARRATIVE', 'calculated'),
            'translation_language': ('FREE_TEXT', 'NARRATIVE', 'calculated'),
            'indentation_level': ('section',), 'prefix_formula': ('serial',),
            'input_mask': ('FREE_TEXT',), 'barcode': ('FREE_TEXT',), 'calculation_formula': ('calculated',),
            'metadata': ('reference',), 'units': ('quantity',),
            'upload_type': ('attachment',), 'allowed_file_types': ('attachment',),
            'attachment_cardinality': ('attachment',),
            'geo_accuracy': ('geopoint',), 'option_list': ('enumerated',),
            'referenced_form': ('reference', 'multiselectreference', 'subform', 'reversereference'),
            'lookup_fields': ('reference',),
            'reverse_reference_field': ('reversereference',),
        }
        for name, types in applicable.items():
            if getattr(self, name) is not None and self.field_type not in types:
                fail(name, f'{ARGUMENTS[name]} does not apply to {self.field_type}.')
        if self.translation:
            if field_code_error(self.translation_field):
                fail('translation_field', 'Translation field must be a field code.')
            languages = self.translation_language
            if isinstance(languages, str):
                languages = [language.strip() for language in languages.split(',')]
            if not isinstance(languages, list) or not languages or any(
                not isinstance(language, str) or not language.strip() for language in languages
            ):
                fail('translation_language', 'Translation requires at least one language code.')
            object.__setattr__(self, 'translation_language', list(dict.fromkeys(languages)))
        elif self.translation_field is not None or self.translation_language is not None:
            fail('translation', 'Translation field/language require translation=True.')
        if self.indentation_level is not None and (
            type(self.indentation_level) is not int or self.indentation_level < 0
        ):
            fail('indentation_level', 'Indentation level must be a non-negative integer.')
        if self.geo_accuracy is not None and (
            not finite_number(self.geo_accuracy) or self.geo_accuracy < 0
        ):
            fail('geo_accuracy', 'Geo accuracy must be a finite non-negative number.')
        if self.field_type == 'calculated' and not (self.calculation_formula or '').strip():
            fail('calculation_formula', 'Calculated fields require a calculation formula.')
        for name, choices in (('upload_type', UPLOAD_TYPES), ('allowed_file_types', FILE_TYPES)):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, list) or any(type(item) is not str or item not in choices for item in value):
                    fail(name, f'{ARGUMENTS[name]} must be a list of supported choices.')
                object.__setattr__(self, name, list(dict.fromkeys(value)))
        if self.allowed_file_types and self.upload_type is not None and 'FILE' not in self.upload_type:
            fail('allowed_file_types', 'Allowed file types require FILE upload capture.')
        if self.attachment_cardinality is not None and self.attachment_cardinality not in ('single', 'multiple'):
            fail('attachment_cardinality', 'Attachment cardinality must be single or multiple.')
        if self.metadata is not None:
            if self.metadata not in ('ADDED_BY', 'LAST_EDITED_BY'):
                fail('metadata', 'Metadata must be ADDED_BY or LAST_EDITED_BY.')
            if self.read_only is False:
                fail('read_only', 'Metadata fields must be read-only.')
            if self.default_value is not None or self.default_formula is not None:
                fail('default_value', 'Server-maintained metadata fields cannot have client defaults.')
        if self.field_type in ('reference', 'multiselectreference', 'subform', 'reversereference'):
            if not self.metadata and self.field_variant != 'user' and not (self.referenced_form or '').strip():
                fail('referenced_form', 'This field type requires a referenced form ID or name.')
            if self.field_type == 'reversereference' and field_code_error(self.reverse_reference_field):
                fail('reverse_reference_field', 'Reverse references require a reference-field code.')
        if self.field_type == 'enumerated':
            self._validate_options()
        if self.lookup_fields is not None:
            if not isinstance(self.lookup_fields, list):
                fail('lookup_fields', 'Lookup fields must be a list of label/formula objects.')
            for lookup in self.lookup_fields:
                if not isinstance(lookup, dict) or set(lookup) != {'label', 'formula'} or any(
                    not isinstance(value, str) or not value.strip() for value in lookup.values()
                ):
                    fail('lookup_fields', 'Each lookup requires a non-blank label and formula.')
            object.__setattr__(self, 'lookup_fields', deepcopy(self.lookup_fields))
        if self.default_value is not None:
            object.__setattr__(self, 'default_value', self._default_value(self.default_value))

    def _validate_options(self):
        options = self.option_list
        if not isinstance(options, dict) or set(options) - {'cardinality', 'presentation', 'values'}:
            fail('option_list', 'Option list must contain cardinality, optional presentation, and values.')
        if options.get('cardinality') not in ('single', 'multiple'):
            fail('option_list', 'Option cardinality must be single or multiple.')
        if 'presentation' in options and options['presentation'] not in ('automatic', 'radio_button', 'dropdown'):
            fail('option_list', 'Unsupported option presentation.')
        values = options.get('values')
        if not isinstance(values, list) or not values:
            fail('option_list', 'Option list requires at least one option.')
        codes = set()
        for option in values:
            if not isinstance(option, dict) or set(option) not in ({'label'}, {'code', 'label'}):
                fail('option_list', 'Each option requires a label.')
            if ('code' in option and field_code_error(option['code'])) or not isinstance(option['label'], str) or not option['label'].strip():
                fail('option_list', 'Options require a non-blank label and, if supplied, a valid legacy code.')
            if 'code' in option:
                if option['code'] in codes:
                    fail('option_list', f'Duplicate option code {option["code"]!r}.')
                codes.add(option['code'])
        object.__setattr__(self, 'option_list', deepcopy(options))

    def _default_value(self, value):
        if self.field_type == 'quantity':
            try:
                if type(value) not in (str, int, float):
                    raise ValueError
                result = float(value)
                if not math.isfinite(result):
                    raise ValueError
                return result
            except (TypeError, ValueError, OverflowError):
                fail('default_value', 'Quantity default value must be a finite number.')
        multiple = self.field_type == 'multiselectreference' or (
            self.field_type == 'enumerated' and self.option_list['cardinality'] == 'multiple'
        )
        if self.field_type == 'geopoint' or multiple:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    fail('default_value', 'Use JSON for coordinates or multiple default values.')
            if multiple:
                if not isinstance(value, list) or any(
                    not isinstance(item, str) and not (self.field_type == 'multiselectreference' and finite_number(item))
                    for item in value
                ):
                    fail('default_value', 'Multiple defaults require strings, or numbers for reference labels.')
            else:
                if not isinstance(value, dict) or set(value) != {'latitude', 'longitude'}:
                    fail('default_value', 'Geopoint defaults require latitude and longitude.')
                for key, limit in (('latitude', 90), ('longitude', 180)):
                    number = value[key]
                    if not finite_number(number) or abs(number) > limit:
                        fail('default_value', f'Invalid default {key}.')
        elif self.field_type == 'reference' and finite_number(value):
            pass
        elif not isinstance(value, str):
            fail('default_value', 'This field type requires a string default value.')
        return deepcopy(value)

    def to_syntax(self) -> str:
        return 'AddField(' + ', '.join(
            f'{argument}={literal_syntax(getattr(self, key))}'
            for key, argument in ARGUMENTS.items() if getattr(self, key) is not None
            and not (key == 'barcode' and self.field_variant is not None)
        ) + ')'

    def execute(self, schema: dict[str, Any]) -> ExecutionResult:
        candidate = deepcopy(self)
        try:
            candidate._validate()
        except FieldParameterError as exc:
            return ExecutionResult.error(schema, str(exc), f'invalid_{exc.parameter}')
        return candidate._apply(schema)

    def _apply(self, schema: dict[str, Any]) -> ExecutionResult:
        if not isinstance(schema, dict) or not isinstance(schema.get('elements'), list):
            return ExecutionResult.error(None, 'AddField requires a form schema with an elements list.', 'invalid_schema')
        if self.field_code is not None and any(isinstance(element, dict) and element.get('code') == self.field_code
               for element in schema['elements']):
            return ExecutionResult.error(schema, f'AddField field code {self.field_code!r} already exists.', 'duplicate_field')
        result = deepcopy(schema)
        used = schema_identities(schema)
        identity = allocate_pending_field_id(self.field_code, used, field_type=self.field_type)
        field = {'id': identity,
                 'label': self.label, 'type': self.field_type,
                 'required': self.required if self.required is not None else False}
        if self.field_code is not None:
            field['code'] = self.field_code
        direct = {'key': 'key', 'description': 'description', 'unique': 'unique', 'read_only': 'readOnly',
                  'default_value': 'defaultValue', 'default_formula': 'defaultValueFormula',
                  'relevance_condition': 'relevanceCondition', 'required_condition': 'requiredCondition',
                  'validation_condition': 'validationCondition', 'validation_message': 'validationMessage'}
        for name, wire_key in direct.items():
            value = getattr(self, name)
            if value is not None:
                field[wire_key] = deepcopy(value)
        if self.field_type == 'serial':
            field.update(key=True, unique=True)
        for name, wire_key in (('hide_from_entry', 'dataEntryVisible'), ('hide_in_table', 'tableVisible')):
            if getattr(self, name) is not None:
                field[wire_key] = not getattr(self, name)
        parameters = {}
        type_values = {'indentation_level': 'indentationLevel', 'prefix_formula': 'prefixFormula',
                       'input_mask': 'inputMask', 'barcode': 'barcode', 'calculation_formula': 'formula', 'units': 'units',
                       'upload_type': 'captureMethods', 'allowed_file_types': 'fileTypes',
                       'attachment_cardinality': 'cardinality',
                       'geo_accuracy': 'requiredAccuracy'}
        for name, wire_key in type_values.items():
            value = getattr(self, name)
            if value is not None:
                parameters[wire_key] = deepcopy(value)
        if self.field_type == 'attachment' and self.attachment_cardinality is None:
            parameters['cardinality'] = 'multiple'
        if self.translation:
            parameters['translationConfig'] = {
                'fieldId': 'field-code:' + self.translation_field,
                'languages': list(self.translation_language),
            }
        if self.option_list:
            parameters.update({key: value for key, value in self.option_list.items() if key != 'values'})
            parameters['values'] = []
            option_tokens = {}
            for index, option in enumerate(self.option_list['values']):
                token = pending_row_id('pending-option:', identity, option.get('code', index), used)
                parameters['values'].append({'id': token, 'label': option['label']})
                if 'code' in option:
                    option_tokens[option['code']] = token
            if self.default_value is not None:
                token = lambda code: option_tokens.get(code, code)
                field['defaultValue'] = [token(code) for code in self.default_value] if isinstance(
                    self.default_value, list
                ) else token(self.default_value)
        if self.field_variant == 'user':
            if not schema.get('databaseId'):
                return ExecutionResult.error(schema, 'User fields require a database ID.', 'missing_database')
            parameters['range'] = [{'formId': f"{schema['databaseId']}@users"}]
        if self.referenced_form:
            if self.field_type in ('reference', 'multiselectreference'):
                parameters['range'] = [{'formId': self.referenced_form}]
            else:
                parameters['formId'] = self.referenced_form
        if self.lookup_fields is not None:
            parameters['lookupConfigs'] = [
                {'id': pending_row_id('pending-lookup:', identity, index, used),
                 'formula': lookup['formula'], 'lookupLabel': lookup['label']}
                for index, lookup in enumerate(self.lookup_fields)
            ]
        if self.reverse_reference_field:
            parameters['fieldId'] = 'field-code:' + self.reverse_reference_field
        if self.metadata:
            database_id = schema.get('databaseId')
            if not isinstance(database_id, str) or not database_id:
                return ExecutionResult.error(schema, 'Metadata fields require a database ID in the form schema.', 'missing_database')
            target = f'{database_id}@users'
            if self.referenced_form and self.referenced_form != target:
                return ExecutionResult.error(schema, "Metadata fields must reference this database's users.", "invalid_metadata_target")
            if any(isinstance(element, dict) and element.get('metadata') == self.metadata
                   for element in schema['elements']):
                return ExecutionResult.error(schema, f'The form already contains {self.metadata} metadata.', 'duplicate_metadata')
            field['metadata'] = self.metadata
            field['readOnly'] = True
            parameters['range'] = [{'formId': target}]
        if parameters:
            field['typeParameters'] = parameters
        elements = result['elements']
        if self.position == 'Beginning':
            elements.insert(0, field)
        elif self.position in ('Before field', 'After field'):
            relative_index = next((index for index, element in enumerate(elements)
                                   if isinstance(element, dict)
                                   and element.get('code') == self.before_after_field), None)
            if relative_index is None:
                return ExecutionResult.error(schema, f'AddField could not find before/after field code {self.before_after_field!r}.', 'missing_relative_target')
            elements.insert(relative_index + (self.position == 'After field'), field)
        else:
            elements.append(field)
        return ExecutionResult(result)


def create_add_field(values: Mapping[str, Any]) -> AddFieldCommand:
    arguments = {key: value for key, value in values.items() if key in ARGUMENTS}
    if arguments.get('field_type') == 'enumerated' and isinstance(arguments.get('option_list'), dict):
        options = deepcopy(arguments['option_list'])
        if options.get('cardinality') == 'single':
            options['presentation'] = 'automatic'
        else:
            options.setdefault('presentation', 'automatic')
        arguments['option_list'] = options
    return AddFieldCommand(**arguments)


def parse_add_field(text: str, call: ast.Call) -> AddFieldCommand:
    types = {argument: str for argument in ARGUMENTS.values()}
    for key in ('key', 'unique', 'required', 'hide_from_entry', 'hide_in_table', 'reviewer_only',
                'read_only', 'translation', 'barcode'):
        types[ARGUMENTS[key]] = bool
    types.update({'indentationLevel': int, 'geoAccuracy': (int, float),
                  'translationLanguage': (str, list), 'uploadType': list, 'allowedFileTypes': list,
                  'optionList': dict, 'lookupFields': list, 'defaultValue': (str, int, float, list, dict)})
    values = parse_literal_parameters(
        text, call, 'AddField', types=types, required={'label'},
        allow_empty=set(types) - {'fieldType', 'fieldCode', 'label'},
    )
    for argument, error in (
        ('fieldCode', None if values.get('fieldType', '').casefold() in ('section', 'note')
         and 'fieldCode' not in values else field_code_error(values.get('fieldCode'))),
        ('fieldType', field_type_error(values.get('fieldType', 'FREE_TEXT'))),
        ('label', None if values['label'].strip() else 'Field label is required.'),
    ):
        if error:
            node = next((keyword.value for keyword in call.keywords
                         if keyword.arg and keyword.arg.casefold() == argument.casefold()), call)
            raise command_error(text, node, error)
    by_argument = {argument: key for key, argument in ARGUMENTS.items()}
    return AddFieldCommand(**{by_argument[key]: value for key, value in values.items()})


ADD_FIELD = CommandCreatorDefinition(
    key='add_field', label='Add field', create_command=create_add_field,
    parameters=ADD_FIELD_PARAMETERS,
)
