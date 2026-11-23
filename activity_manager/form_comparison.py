"""Generate authored commands from saved form schemas; never perform writes."""

from copy import deepcopy
from dataclasses import dataclass

from .command_parser import parse_command_text
from .commands.add_field import AddFieldCommand, literal_syntax
from .commands.add_form import AddFormCommand
from .commands.delete_field import DeleteFieldCommand
from .commands.modify_field import DIRECT_PROPERTIES, TYPE_PROPERTIES, EDIT_ARGUMENTS, schema_variant
from .commands.modify_field_parameters import MODIFY_FIELD_PARAMETERS
from .commands.modify_form import ModifyFormCommand
from .commands.move_field import MoveFieldCommand
from .field_parameters import parameter_applies
from .schema_names import readable_schema


@dataclass(frozen=True)
class ComparisonOptions:
    create_new: bool = False
    delete_unmatched: bool = True
    preserve_order: bool = False
    overwrite_missing: bool = True


def comparison_fields(schema):
    return [field for field in (schema or {}).get('elements', [])
            if isinstance(field, dict) and field.get('type', '').casefold() != 'subform']


def field_selector(field):
    return field.get('code') or field.get('label') or ''


def field_caption(field):
    code, label = field.get('code'), field.get('label')
    return f'{code} ({label})' if code and label else code or label or '(Unnamed field)'


def reference_ids(field):
    params = field.get('typeParameters') or {}
    return tuple(entry.get('formId') for entry in params.get('range') or []) or (
        (params['formId'],) if params.get('formId') else ())


def compatible(model_field, initial_field, model, initial, related):
    if (model_field.get('type', '').casefold(), schema_variant(model_field)) != (
            initial_field.get('type', '').casefold(), schema_variant(initial_field)):
        return False
    left, right = reference_ids(model_field), reference_ids(initial_field)
    if len(left) != len(right):
        return False
    for model_id, initial_id in zip(left, right):
        m, f = related.get(model_id, {}), related.get(initial_id, {})
        model_local = m.get('databaseId') == model.get('databaseId') if m else False
        initial_local = f.get('databaseId') == initial.get('databaseId') if f else False
        if model_local and initial_local:
            if not m.get('label') or m.get('label') != f.get('label'):
                return False
        elif model_local != initial_local or model_id != initial_id:
            return False
    return True


def mapping_choices(model, initial, related):
    """Return compatible initial-field indexes and case-insensitive code defaults."""
    related = {**related, model.get('id'): model, initial.get('id'): initial}
    choices, mapping, used = [], [], set()
    source = comparison_fields(initial)
    for field in comparison_fields(model):
        candidates = [i for i, other in enumerate(source)
                      if compatible(field, other, model, initial, related)]
        choices.append(candidates)
        code = field.get('code')
        match = next((i for i in candidates if i not in used and code and
                      str(source[i].get('code') or '').casefold() == code.casefold()), None)
        mapping.append(match)
        if match is not None:
            used.add(match)
    return choices, mapping


def field_properties(field):
    """Project wire properties into the existing Add/ModifyField parameter vocabulary."""
    params = field.get('typeParameters') or {}
    values = {key: deepcopy(field[wire]) for key, wire in DIRECT_PROPERTIES.items()
              if key != 'new_code' and field.get(wire) is not None}
    values.update({key: deepcopy(params[wire]) for key, wire in TYPE_PROPERTIES.items()
                   if params.get(wire) is not None})
    for key, wire in (('hide_from_entry', 'dataEntryVisible'), ('hide_in_table', 'tableVisible')):
        if field.get(wire) is not None:
            values[key] = not field[wire]
    if field.get('metadata') is not None:
        values['metadata'] = field['metadata']
    if field.get('type', '').casefold() == 'enumerated':
        options = {}
        for key in ('cardinality', 'presentation'):
            if params.get(key) is not None:
                options[key] = params[key]
        if params.get('values') is not None:
            options['values'] = [{'label': row['label']} for row in params['values']]
        values['option_list'] = options
    if params.get('lookupConfigs') is not None:
        values['lookup_fields'] = [{'label': row['lookupLabel'], 'formula': row['formula']}
                                  for row in params['lookupConfigs']]
    config = params.get('translationConfig')
    if config is not None:
        values['translation'] = bool(config)
        if config.get('fieldId') is not None:
            values['translation_field'] = config['fieldId']
        if config.get('languages') is not None:
            values['translation_language'] = ', '.join(config['languages'])
    references = reference_ids(field)
    if references:
        values['referenced_form'] = references[0]
    if field.get('type', '').casefold() == 'reversereference' and params.get('fieldId') is not None:
        values['reverse_reference_field'] = params['fieldId']
    return values


def modify_syntax(field, changes):
    arguments = {'fieldCode': field_selector(field), 'fieldType': field['type']}
    variant = schema_variant(field)
    if variant is not None:
        arguments['fieldVariant'] = variant
    arguments.update({EDIT_ARGUMENTS[key]: value for key, value in changes.items()})
    return 'ModifyField(' + ', '.join(f'{key}={literal_syntax(value)}' for key, value in arguments.items()) + ')'


def generate_commands(model, initial=None, *, mapping=None, options=ComparisonOptions(),
                      related=None, database_names=None, folder='/', record_labels=None):
    """Build and parse the complete replacement before returning any commands."""
    initial = None if options.create_new else initial
    related = {**(related or {}), model.get('id'): model}
    if initial:
        related[initial.get('id')] = initial
    if initial and model.get('id') == initial.get('id'):
        raise ValueError('Choose a model different from the initial form.')
    model_fields, initial_fields = comparison_fields(model), comparison_fields(initial)
    if initial:
        choices, defaults = mapping_choices(model, initial, related)
        mapping = defaults if mapping is None else list(mapping)
        if len(mapping) != len(model_fields):
            raise ValueError('Every model field needs a mapping selection.')
        selected = [i for i in mapping if i is not None]
        if len(set(selected)) != len(selected) or any(i is not None and i not in choices[n]
                                                    for n, i in enumerate(mapping)):
            raise ValueError('Field mappings must be compatible and one-to-one.')
    else:
        mapping = [None] * len(model_fields)
    readable = readable_schema(model, related, database_names, record_labels=record_labels)
    # readable_schema converts range references; formId references need the same qualification.
    for field in readable.get('elements', []):
        params = field.get('typeParameters') or {}
        target = related.get(params.get('formId'))
        if target and target.get('label'):
            db = target.get('databaseId')
            params['formId'] = (f'{(database_names or {}).get(db, db)}/' if db != model.get('databaseId') else '') + target['label']
    readable_fields = comparison_fields(readable)
    syntax = []
    if initial is None:
        syntax.append(AddFormCommand(model['label'], folder).to_syntax())
    used = {i for i in mapping if i is not None}
    unmatched_codes = {str(field.get('code')).casefold() for field, match in zip(model_fields, mapping)
                       if match is None and field.get('code')}
    renamed = {}
    for i, field in enumerate(initial_fields):
        if i not in used and field.get('code') and field['code'].casefold() in unmatched_codes:
            renamed[i] = field['code'] + '_OLD'
            syntax.append(modify_syntax(field, {'new_code': renamed[i]}))
    previous = None
    for field, readable_field, match in zip(model_fields, readable_fields, mapping):
        properties = field_properties(readable_field)
        kind, variant = field['type'], schema_variant(field)
        context = {'field_type': kind, 'field_variant': variant, 'required': True, 'translation': True}
        position = 'Beginning' if previous is None else 'After field'
        if match is None:
            properties = {key: value for key, value in properties.items() if parameter_applies(key, context)}
            syntax.append(AddFieldCommand(field_code=field.get('code'), field_type=kind,
                field_variant=variant, position=position, before_after_field=previous, **properties).to_syntax())
        else:
            source = initial_fields[match]
            source_properties = field_properties(source)
            changes = {'new_code': field.get('code') or ''} if source.get('code') != field.get('code') else {}
            for definition in MODIFY_FIELD_PARAMETERS:
                key = definition.key
                if key not in EDIT_ARGUMENTS or key == 'new_code' or not definition.enabled:
                    continue
                condition = definition.visible_when
                if callable(condition) and not condition(context):
                    continue
                if key in properties:
                    value = deepcopy(properties[key])
                    if key == 'option_list':
                        value.pop('cardinality', None)
                    changes[key] = value
                elif options.overwrite_missing and key in source_properties:
                    changes[key] = ''
            syntax.append(modify_syntax(source, changes))
            if not options.preserve_order:
                syntax.append(MoveFieldCommand(field_selector(field), position, previous).to_syntax())
        previous = field_selector(field)
    if initial and options.delete_unmatched:
        for i, field in enumerate(initial_fields):
            if i not in used:
                syntax.append(DeleteFieldCommand(renamed.get(i, field_selector(field))).to_syntax())
    label_id = model.get('recordLabelFieldId')
    label = next((field_selector(field) for field in model_fields if field.get('id') == label_id), '')
    syntax.append(ModifyFormCommand(True, new_name=initial['label'] if initial else None,
                                    record_label_field_id=label).to_syntax())
    return parse_command_text('\n'.join(syntax))
