"""Standalone ModifyField creation, serialization and atomic schema updates."""

from copy import deepcopy
from dataclasses import dataclass, field
import json
import math

from ..command_creator import CommandCreatorDefinition
from .models import CommandDefinition
from ..command_execution import ExecutionResult
from ..command_parser import command_error, parse_literal_parameters
from ..functions import FunctionCall, FunctionContext, evaluate_function, validate_function
from ..field_parameters import FIELD_ARGUMENTS, FIELD_TYPES_BY_CASE, FIELD_VARIANTS, field_type_error
from .add_field import AddFieldCommand, literal_syntax
from ..schema_identity import pending_row_id, schema_identities
from ..schema_names import readable_schema
from .modify_field_parameters import MODIFY_FIELD_PARAMETERS


EDIT_ARGUMENTS = {
    'new_code': 'newCode',
    **{parameter.key: FIELD_ARGUMENTS[parameter.key] for parameter in MODIFY_FIELD_PARAMETERS
       if parameter.key not in ('field_code', 'field_type', 'field_variant', 'new_code')},
}


@dataclass(frozen=True)
class ModifyFieldCommand:
    field_code: str
    field_type: str
    field_variant: str | None = None
    changes: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.field_code, str) or not self.field_code.strip():
            raise ValueError('Existing field code or label is required.')
        error = field_type_error(self.field_type)
        if error:
            raise ValueError(error)
        object.__setattr__(self, 'field_type', FIELD_TYPES_BY_CASE[self.field_type.casefold()])
        variants = FIELD_VARIANTS.get(self.field_type.casefold(), ())
        if variants and self.field_variant not in variants:
            raise ValueError('Select a field variant for this field type.')
        if not variants and self.field_variant is not None:
            raise ValueError('This field type has no field variant.')
        if set(self.changes) - EDIT_ARGUMENTS.keys():
            raise ValueError('Unknown or immutable ModifyField property.')
        definitions = {parameter.key: parameter for parameter in MODIFY_FIELD_PARAMETERS}
        for key, value in self.changes.items():
            validate_edit(definitions[key], value)
        object.__setattr__(self, 'changes', deepcopy(self.changes))

    def to_syntax(self):
        arguments = [('fieldCode', self.field_code), ('fieldType', self.field_type)]
        if self.field_variant is not None:
            arguments.append(('fieldVariant', self.field_variant))
        arguments.extend((argument, self.changes[key]) for key, argument in EDIT_ARGUMENTS.items()
                         if key in self.changes)
        return 'ModifyField(' + ', '.join(f'{key}={edit_syntax(value)}' for key, value in arguments) + ')'

    def execute(self, schema, *, context=None):
        if not isinstance(schema, dict) or not isinstance(schema.get('elements'), list):
            return ExecutionResult.error(None, 'ModifyField requires a form schema with an elements list.', 'invalid_schema')
        try:
            # Revalidate mutable nested values before touching the schema copy.
            command = ModifyFieldCommand(self.field_code, self.field_type, self.field_variant, self.changes)
            index = find_target(command, schema['elements'])
            if index is None:
                return ExecutionResult.warning(schema, f'ModifyField could not find field {self.field_code!r}.', 'missing_target')
            if not matches_target(command, schema['elements'][index]):
                return ExecutionResult.warning(schema,
                    f'Field code {self.field_code!r} has a different type or variant. '
                    'Command skipped; schema unchanged.', 'target_type_mismatch')
            check_applicability(command, schema['elements'][index])
            command = resolve_function_edits(command, schema, index, context)
            result = deepcopy(schema)
            target = result['elements'][index]
            apply_edits(command, target, result, context.reserved_identities if context else None)
            validate_result(command, target, result, index)
            return ExecutionResult(result)
        except ValueError as exc:
            return ExecutionResult.error(schema, str(exc), 'invalid_modify_field')


# Wire mappings are explicit so absent arguments never acquire creation defaults.
DIRECT_PROPERTIES = {
    'new_code': 'code', 'label': 'label', 'description': 'description',
    'key': 'key', 'unique': 'unique', 'required': 'required', 'read_only': 'readOnly',
    'relevance_condition': 'relevanceCondition', 'required_condition': 'requiredCondition',
    'validation_condition': 'validationCondition', 'validation_message': 'validationMessage',
    'default_value': 'defaultValue', 'default_formula': 'defaultValueFormula',
}
TYPE_PROPERTIES = {
    'units': 'units', 'input_mask': 'inputMask', 'prefix_formula': 'prefixFormula',
    'calculation_formula': 'formula', 'indentation_level': 'indentationLevel',
    'upload_type': 'captureMethods', 'allowed_file_types': 'fileTypes',
    'geo_accuracy': 'requiredAccuracy',
}


FUNCTION_PARAMETERS = {
    parameter.key: EDIT_ARGUMENTS[parameter.key]
    for parameter in MODIFY_FIELD_PARAMETERS
    if parameter.key in EDIT_ARGUMENTS
    and parameter.control in ('text', 'combobox') and parameter.value_type == 'string'
}


def edit_syntax(value):
    if isinstance(value, FunctionCall):
        return value.name + '(' + ', '.join(literal_syntax(arg) for arg in value.arguments) + ')'
    return literal_syntax(value)


def resolve_function_edits(command, schema, index, context=None):
    changes = dict(command.changes)
    if not any(isinstance(value, FunctionCall) for value in changes.values()):
        return command
    # Only the function input uses readable text; the original IDs schema remains
    # the basis for applying edits and for the existing post-command resolver.
    names = readable_schema(schema,
        context.related_schemas if context else None,
        context.database_names if context else None)
    target = names['elements'][index]
    definitions = {parameter.key: parameter for parameter in MODIFY_FIELD_PARAMETERS}
    for key, call in command.changes.items():
        if not isinstance(call, FunctionCall):
            continue
        params = target.get('typeParameters') or {}
        if key in ('translation_field', 'translation_language'):
            config = params.get('translationConfig') or {}
            value = config.get('fieldId' if key == 'translation_field' else 'languages')
            if key == 'translation_language' and isinstance(value, list) and all(isinstance(item, str) for item in value):
                value = ', '.join(value)
        else:
            container = target if key in DIRECT_PROPERTIES else params
            wire_key = DIRECT_PROPERTIES[key] if key in DIRECT_PROPERTIES else TYPE_PROPERTIES[key]
            value = container.get(wire_key)
        function_context = FunctionContext(FUNCTION_PARAMETERS[key],
            str(value) if isinstance(value, str) else value, command='ModifyField')
        changes[key] = normalize_blank_edit(definitions[key], evaluate_function(call, function_context))
    return ModifyFieldCommand(command.field_code, command.field_type, command.field_variant,
                             changes)


def schema_variant(element):
    kind = str(element.get('type', '')).casefold()
    params = element.get('typeParameters') or {}
    if not isinstance(params, dict):
        raise ValueError('Field typeParameters must be an object.')
    if kind == 'free_text':
        return 'barcode' if params.get('barcode') is True else 'text'
    if kind == 'enumerated':
        # ActivityInfo's legacy omitted cardinality means single selection.
        cardinality = params.get('cardinality', 'single')
        if cardinality not in ('single', 'multiple'):
            raise ValueError('Enumerated field has invalid cardinality.')
        return cardinality
    if kind == 'reference':
        if element.get('metadata'):
            return 'metadata'
        ranges = params.get('range') or []
        return 'user' if any(isinstance(entry, dict) and
            str(entry.get('formId', '')).endswith('@users') for entry in ranges) else 'reference'
    return None


def matches_target(command, element):
    return str(element.get('type', '')).casefold() == command.field_type.casefold() and schema_variant(element) == command.field_variant


def find_target(command, elements):
    coded = [index for index, element in enumerate(elements)
             if isinstance(element, dict) and element.get('code') == command.field_code]
    if len(coded) > 1:
        raise ValueError(f'Ambiguous field code {command.field_code!r}.')
    if coded:
        return coded[0]
    labelled = [index for index, element in enumerate(elements)
                if isinstance(element, dict) and element.get('label') == command.field_code
                and matches_target(command, element)]
    if len(labelled) > 1:
        raise ValueError(f'Ambiguous field label {command.field_code!r} for this type and variant.')
    return labelled[0] if labelled else None


def set_or_remove(container, key, value):
    if value is None:
        container.pop(key, None)
    else:
        container[key] = deepcopy(value)


def replace_rows(existing, requested, label_key, prefix, owner, schema, reserved=None):
    if requested is None:
        return None
    if not isinstance(existing, list) or any(not isinstance(row, dict) for row in existing):
        raise ValueError('Existing collection must contain objects.')
    by_label = {}
    for row in existing:
        by_label.setdefault(row.get(label_key), []).append(row)
    labels = set()
    used = schema_identities(schema) | (reserved or set())
    result = []
    for row in requested:
        label = row['label']
        if label in labels:
            raise ValueError(f'Duplicate requested label {label!r}.')
        labels.add(label)
        matches = by_label.get(label, [])
        if len(matches) > 1:
            raise ValueError(f'Ambiguous existing collection label {label!r}.')
        item = deepcopy(matches[0]) if matches else {}
        if matches and (not isinstance(item.get('id'), str) or not item['id']):
            raise ValueError(f'Existing row {label!r} has no usable ID.')
        if not matches:
            item['id'] = pending_row_id(prefix, owner, 0, used)
        item[label_key] = label
        if label_key == 'lookupLabel':
            item['formula'] = row['formula']
        result.append(item)
    return result


def check_applicability(command, target):
    context = {'field_type': command.field_type, 'field_variant': command.field_variant,
               'required': True, 'translation': True,
               'upload_type': None}
    definitions = {parameter.key: parameter for parameter in MODIFY_FIELD_PARAMETERS}
    for key, value in command.changes.items():
        definition = definitions[key]
        condition = definition.visible_when
        relevant = condition(context) if callable(condition) else (
            condition is None or context.get(condition[0]) in condition[1])
        if not relevant or not definition.enabled:
            raise ValueError(f'{EDIT_ARGUMENTS[key]} cannot be modified for this field type/variant.')
        if key == 'option_list' and value is not None and 'presentation' in value and command.field_variant != 'multiple':
            raise ValueError('Presentation can only be modified for multiple selection.')


def apply_edits(command, target, schema, reserved=None):
    check_applicability(command, target)
    if not isinstance(target.get('id'), str) or not target['id']:
        raise ValueError('The target field must have an ID.')
    params = target.get('typeParameters') or {}
    if not isinstance(params, dict):
        raise ValueError('Field typeParameters must be an object.')
    params = deepcopy(params)
    for key, value in command.changes.items():
        if key in DIRECT_PROPERTIES:
            if key == 'default_value' and value is not None:
                helper = AddFieldCommand(field_type=command.field_type, option_list={
                    'cardinality': command.field_variant})
                value = helper._default_value(value)
            set_or_remove(target, DIRECT_PROPERTIES[key], value)
        elif key in TYPE_PROPERTIES:
            set_or_remove(params, TYPE_PROPERTIES[key], value)
        elif key in ('hide_from_entry', 'hide_in_table'):
            set_or_remove(target, 'dataEntryVisible' if key == 'hide_from_entry' else 'tableVisible',
                          None if value is None else not value)
    if 'option_list' in command.changes:
        options = command.changes['option_list']
        if options is None:
            raise ValueError('The required enumerated option list cannot be removed.')
        if 'presentation' in options:
            set_or_remove(params, 'presentation', options['presentation'])
        if 'values' in options:
            rows = replace_rows(params.get('values') or [], options['values'], 'label',
                                'pending-option:', target['id'], schema, reserved)
            set_or_remove(params, 'values', rows)
    if 'lookup_fields' in command.changes:
        rows = replace_rows(params.get('lookupConfigs') or [], command.changes['lookup_fields'],
                            'lookupLabel', 'pending-lookup:', target['id'], schema, reserved)
        set_or_remove(params, 'lookupConfigs', rows)
    translation_keys = {'translation', 'translation_field', 'translation_language'}
    if translation_keys & command.changes.keys():
        changes = command.changes
        if 'translation' in changes and changes['translation'] in (False, None):
            if any(changes.get(key) is not None for key in ('translation_field', 'translation_language')):
                raise ValueError('Translation details conflict with disabling translation.')
            params.pop('translationConfig', None)
        else:
            config = deepcopy(params.get('translationConfig') or {})
            if 'translation_field' in changes:
                value = changes['translation_field']
                set_or_remove(config, 'fieldId', None if value is None else 'field-code:' + value)
            if 'translation_language' in changes:
                value = changes['translation_language']
                languages = [language.strip() for language in value.split(',')] if isinstance(value, str) else value
                set_or_remove(config, 'languages', languages)
            params['translationConfig'] = config
    if params:
        target['typeParameters'] = params
    elif isinstance(target.get('typeParameters'), dict):
        # Retain an existing empty container; never synthesize unrelated properties.
        target['typeParameters'] = params


def validate_result(command, target, schema, target_index):
    if not isinstance(target.get('label'), str) or not target['label'].strip():
        raise ValueError('The required field label cannot be removed or blank.')
    if 'new_code' in command.changes and target.get('code') is not None:
        if any(index != target_index and isinstance(element, dict) and element.get('code') == target['code']
               for index, element in enumerate(schema['elements'])):
            raise ValueError(f'Field code {target["code"]!r} already exists.')
    params = target.get('typeParameters') or {}
    if command.field_type == 'calculated' and not str(params.get('formula') or '').strip():
        raise ValueError('Calculated fields require a calculation formula.')
    if command.field_type == 'enumerated':
        if not params.get('values'):
            raise ValueError('Enumerated fields require at least one option.')
        if target.get('defaultValue') is not None:
            values = target['defaultValue'] if isinstance(target['defaultValue'], list) else [target['defaultValue']]
            for value in values:
                matches = [row for row in params['values'] if value in (row.get('id'), row.get('label'))]
                if len(matches) != 1:
                    raise ValueError(f'Option default {value!r} does not identify exactly one remaining option.')
    if 'indentation_level' in command.changes and params.get('indentationLevel') is not None:
        if not 0 <= params['indentationLevel'] <= 3:
            raise ValueError('Indentation level must be between 0 and 3.')
    if 'geo_accuracy' in command.changes and params.get('requiredAccuracy', 0) < 0:
        raise ValueError('Geo accuracy must be non-negative.')
    if {'allowed_file_types', 'upload_type'} & command.changes.keys():
        if params.get('fileTypes') and 'captureMethods' in params and 'FILE' not in params['captureMethods']:
            raise ValueError('Allowed file types require FILE upload capture.')
    if {'required', 'hide_from_entry'} & command.changes.keys():
        if target.get('required') is True and target.get('dataEntryVisible') is False:
            raise ValueError('A field hidden from entry cannot be required.')
    if {'required_condition', 'required'} & command.changes.keys():
        if target.get('requiredCondition') and not target.get('required'):
            raise ValueError('Required condition requires Required=true; clear the condition in the same command.')
    if {'translation', 'translation_field', 'translation_language'} & command.changes.keys():
        if 'translationConfig' in params:
            config = params['translationConfig']
            if not config.get('fieldId') or not isinstance(config.get('languages'), list) or not config['languages'] or any(
                not isinstance(language, str) or not language.strip() for language in config['languages']):
                raise ValueError('Translation requires a target field and at least one language.')
    if schema_variant(target) != command.field_variant:
        raise ValueError('The field variant cannot be changed.')


def validate_edit(definition, value, *, allow_functions=True):
    """Check literal structure only; schema-dependent validity is deferred."""
    if isinstance(value, FunctionCall):
        parameter = FUNCTION_PARAMETERS.get(definition.key)
        if parameter is None or not allow_functions:
            raise ValueError(f'{definition.label} does not support function calls.')
        validate_function(value, parameter, command_name='ModifyField')
        return
    if value is None:
        if definition.required:
            raise ValueError(f'{definition.label} is required.')
        return
    control = definition.control
    if control == 'group':
        if not isinstance(value, dict):
            raise ValueError(f'{definition.label} must be an object.')
        children = {child.key: child for child in definition.children}
        if set(value) - children.keys():
            raise ValueError(f'{definition.label} contains an unknown or immutable property.')
        for key, item in value.items():
            validate_edit(children[key], item, allow_functions=False)
    elif control == 'repeat':
        if not isinstance(value, list):
            raise ValueError(f'{definition.label} must be a list of rows.')
        children = {child.key: child for child in definition.children}
        for row in value:
            if not isinstance(row, dict) or set(row) != children.keys():
                raise ValueError(f'{definition.label} rows require {", ".join(children)}.')
            for key, item in row.items():
                validate_edit(children[key], item, allow_functions=False)
    elif control == 'boolean':
        if type(value) is not bool:
            raise ValueError(f'{definition.label} must be a boolean.')
    elif control == 'multi_select':
        if not isinstance(value, list) or any(item not in definition.options for item in value):
            raise ValueError(f'{definition.label} must contain listed choices.')
    elif control in ('single_select', 'select', 'toggle'):
        if value not in definition.options:
            raise ValueError(f'{definition.label} must be a listed choice.')
    elif definition.value_type in ('integer', 'number'):
        accepted = (int,) if definition.value_type == 'integer' else (int, float)
        if type(value) not in accepted or not math.isfinite(value):
            raise ValueError(f'{definition.label} must be a finite {definition.value_type}.')
    elif definition.key != 'default_value':
        if not isinstance(value, str) or definition.required and not value.strip():
            raise ValueError(f'{definition.label} requires text.')
    else:
        validate_default_literal(value)
    if definition.validate:
        error = definition.validate(value)
        if error:
            raise ValueError(error)


def validate_default_literal(value):
    if type(value) is float and not math.isfinite(value):
        raise ValueError('Default values must contain finite numbers.')
    if isinstance(value, dict):
        for item in value.values():
            validate_default_literal(item)
    elif isinstance(value, list):
        for item in value:
            validate_default_literal(item)
    elif type(value) not in (str, int, float, type(None)):
        raise ValueError('Default values must contain supported literals.')


def normalize_blank_edit(definition, value):
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, (list, dict)) and not value:
        return None
    if definition.control == 'group' and isinstance(value, dict):
        children = {child.key: child for child in definition.children}
        return {key: normalize_blank_edit(children[key], item) if key in children else item
                for key, item in value.items()}
    return value


def create_modify_field(values):
    definitions = {parameter.key: parameter for parameter in MODIFY_FIELD_PARAMETERS}
    # The creator's explicit blanks request removal. Row members remain required.
    changes = {key: normalize_blank_edit(definitions[key], value)
               for key, value in values.items() if key in EDIT_ARGUMENTS}
    default = changes.get('default_value')
    if isinstance(default, str):
        kind = values['field_type'].casefold()
        if kind == 'quantity':
            try:
                changes['default_value'] = float(default)
                if not math.isfinite(changes['default_value']):
                    raise ValueError
            except ValueError:
                raise ValueError('Quantity default must be a finite number.') from None
        elif kind == 'geopoint' or kind == 'enumerated' and values.get('field_variant') == 'multiple':
            try:
                changes['default_value'] = json.loads(default)
            except ValueError:
                raise ValueError('Use JSON for coordinates or multiple defaults.') from None
    return ModifyFieldCommand(values['field_code'], values['field_type'], values.get('field_variant'), changes)


def parse_modify_field(text, call):
    types = {'fieldCode': str, 'fieldType': str, 'fieldVariant': str}
    for parameter in MODIFY_FIELD_PARAMETERS:
        if parameter.key not in EDIT_ARGUMENTS:
            continue
        expected = (bool,) if parameter.control == 'boolean' else (str,)
        if parameter.control == 'group':
            expected = (dict,)
        elif parameter.control in ('repeat', 'multi_select'):
            expected = (list,)
        elif parameter.value_type == 'integer':
            expected = (int,)
        elif parameter.value_type == 'number':
            expected = (int, float)
        if parameter.key == 'default_value':
            expected = (str, int, float, dict, list)
        types[EDIT_ARGUMENTS[parameter.key]] = (*expected, type(None))
    values = parse_literal_parameters(text, call, 'ModifyField', types=types,
                                      required={'fieldCode', 'fieldType'},
                                      function_parameters=set(FUNCTION_PARAMETERS.values()),
                                      allow_empty=set(types) - {'fieldCode', 'fieldType', 'fieldVariant'})
    names = {'fieldCode': 'field_code', 'fieldType': 'field_type', 'fieldVariant': 'field_variant',
             **{argument: key for key, argument in EDIT_ARGUMENTS.items()}}
    try:
        return create_modify_field({names[key]: value for key, value in values.items()})
    except ValueError as exc:
        raise command_error(text, call, str(exc)) from None


def target_defaults(schema, target):
    """Suggest target selectors from cached fields, preferring exact codes."""
    elements = schema.get('elements', []) if schema else []
    if not target or not isinstance(elements, list):
        return {}
    fields = [element for element in elements if isinstance(element, dict)]
    matches = [element for element in fields if element.get('code') == target]
    if not matches:
        matches = [element for element in fields if element.get('label') == target]
    if len(matches) != 1:
        return {}
    field = matches[0]
    kind = FIELD_TYPES_BY_CASE.get(str(field.get('type', '')).casefold())
    if kind is None:
        return {}
    try:
        variant = schema_variant(field)
    except ValueError:
        return {}
    return {'field_type': kind, 'field_variant': variant}


MODIFY_FIELD = CommandCreatorDefinition('modify_field', 'Modify field', MODIFY_FIELD_PARAMETERS, create_modify_field,
                                        target_defaults=target_defaults)


COMMAND = CommandDefinition('ModifyField', parse_modify_field, MODIFY_FIELD)
