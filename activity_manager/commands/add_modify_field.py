"""Dispatch a field command against the current applied schema."""

from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial

from .add_field import (
    ADD_FIELD_PARAMETERS, ARGUMENTS, AddFieldCommand, FieldParameterError, parse_add_field_arguments,
)
from .field_selector import find_field_index
from .models import CommandDefinition
from .modify_field import (
    EDIT_ARGUMENTS, FUNCTION_PARAMETERS, cached_target_form, check_reference_target,
    create_modify_field, edit_syntax, matches_target, resolve_target_form,
    target_mismatch,
)
from .move_field import MoveFieldCommand
from ..command_creator import CommandCreatorDefinition
from ..command_execution import CommandContext, ExecutionResult
from ..command_parser import parse_function_input
from ..field_parameters import FIELD_TYPES_BY_CASE, FIELD_VARIANTS, field_type_error
from ..functions import FunctionCall, FunctionContext, evaluate_function, validate_function
from ..schema_identity import field_code_error


NAME = 'AddModifyField'
EDIT_KEYS = tuple(key for key in EDIT_ARGUMENTS if key in ARGUMENTS)
FUNCTION_KEYS = FUNCTION_PARAMETERS.keys() & set(EDIT_KEYS)
REFERENCE_TYPES = ('reference', 'multiselectreference', 'subform', 'reversereference')


@dataclass(frozen=True)
class AddModifyFieldCommand(AddFieldCommand):
    label: str | FunctionCall | None = None

    def __post_init__(self):
        # Keep authored values so the update path never inherits creation defaults.
        for key, argument in ARGUMENTS.items():
            value = getattr(self, key)
            if isinstance(value, FunctionCall):
                validate_function(value, argument, command_name=NAME)
            elif isinstance(value, (dict, list)):
                object.__setattr__(self, key, deepcopy(value))

    def to_syntax(self):
        return NAME + '(' + ', '.join(
            f'{argument}={edit_syntax(getattr(self, key))}'
            for key, argument in ARGUMENTS.items() if getattr(self, key) is not None
        ) + ')'

    def _target(self, schema):
        if not isinstance(schema, dict) or not isinstance(schema.get('elements'), list):
            raise ValueError(f'{NAME} requires a form schema with an elements list.')
        error = field_type_error(self.field_type)
        if error:
            raise ValueError(error)
        kind = FIELD_TYPES_BY_CASE.get(self.field_type.casefold(), self.field_type)
        if self.field_code is not None or kind not in ('section', 'note'):
            error = field_code_error(self.field_code)
            if error:
                raise ValueError(error)
        return find_field_index(self.field_code, schema['elements'], exact_code=True)

    def _add(self, schema, context):
        arguments = {}
        for key, argument in ARGUMENTS.items():
            value = getattr(self, key)
            if isinstance(value, FunctionCall):
                value = evaluate_function(value, FunctionContext(argument, None, command=NAME))
            arguments[key] = value
        return AddFieldCommand(**arguments).execute(schema, context=context)

    def _expected_variant(self, kind):
        if self.field_variant is not None:
            if self.field_variant not in FIELD_VARIANTS.get(kind.casefold(), ()):
                raise ValueError('Invalid field variant for this field type.')
        if kind == 'FREE_TEXT':
            return self.field_variant or ('barcode' if self.barcode is True else 'text')
        if kind == 'enumerated':
            options = self.option_list if isinstance(self.option_list, dict) else {}
            return self.field_variant or options.get('cardinality')
        if kind == 'reference':
            if self.metadata:
                return 'metadata'
            if isinstance(self.referenced_form, str) and self.referenced_form.endswith('@users'):
                return 'user'
            return self.field_variant or 'reference'
        return None

    def _match(self, schema, target):
        kind = FIELD_TYPES_BY_CASE.get(self.field_type.casefold(), self.field_type)
        variant = self._expected_variant(kind)
        if not matches_target(kind, variant, target):
            return target_mismatch(schema, self.field_code, 'type or variant')
        params = target.get('typeParameters') or {}
        if self.metadata is not None and target.get('metadata') != self.metadata:
            return target_mismatch(schema, self.field_code, 'metadata kind')
        if kind == 'attachment' and params.get('cardinality', 'multiple') != (self.attachment_cardinality or 'multiple'):
            return target_mismatch(schema, self.field_code, 'attachment cardinality')
        if (kind == 'geopoint' and self.manual_entry_allowed is not None
                and params.get('manualEntryAllowed', True) != self.manual_entry_allowed):
            return target_mismatch(schema, self.field_code, 'manual coordinate entry')
        return kind, variant

    def _requested_form(self, schema, variant):
        if self.referenced_form is not None:
            return self.referenced_form
        if variant in ('user', 'metadata') and schema.get('databaseId'):
            return f"{schema['databaseId']}@users"
        return None

    def _check_reference(self, schema, target, kind, requested, context):
        if kind not in REFERENCE_TYPES:
            return None
        mismatch = check_reference_target(schema, self.field_code, target, requested, required=True)
        if mismatch is not None:
            return mismatch
        if kind == 'reversereference' and self.reverse_reference_field is not None:
            params = target.get('typeParameters') or {}
            related = (context.related_schemas or {}) if context else {}
            source = schema if requested == schema.get('id') else related.get(requested, {})
            codes = [field.get('id') for field in source.get('elements', [])
                     if field.get('code') == self.reverse_reference_field]
            if len(codes) != 1 or codes[0] != params.get('fieldId'):
                return target_mismatch(schema, self.field_code, 'reverse-reference source field')
        return None

    def _modify(self, schema, index, kind, variant, context):
        try:
            changes = {key: getattr(self, key) for key in EDIT_KEYS if getattr(self, key) is not None}
            options = changes.get('option_list')
            if kind == 'enumerated' and isinstance(options, dict) and options:
                options = {key: deepcopy(value) for key, value in options.items() if key != 'cardinality'}
                if variant == 'single' and options.get('presentation') == 'automatic':
                    options.pop('presentation')
                if 'values' in options:
                    option_codes = {}
                    for row in options['values']:
                        if isinstance(row, dict) and 'code' in row:
                            code = row.pop('code')
                            if field_code_error(code) or code in option_codes:
                                raise ValueError(f'Invalid or duplicate option code {code!r}.')
                            option_codes[code] = row.get('label')
                    default = changes.get('default_value')
                    if isinstance(default, (str, list)):
                        # AddField also accepts legacy option codes in defaults.
                        parsed = AddFieldCommand(field_type=kind, field_variant=variant,
                                                 default_value=default).default_value
                        if isinstance(parsed, list):
                            changes['default_value'] = [
                                option_codes.get(value, value) if isinstance(value, str) else value
                                for value in parsed
                            ]
                        elif isinstance(parsed, str):
                            changes['default_value'] = option_codes.get(parsed, parsed)
                if options:
                    changes['option_list'] = options
                else:
                    changes.pop('option_list')
            languages = changes.get('translation_language')
            if isinstance(languages, list) and all(isinstance(item, str) for item in languages):
                changes['translation_language'] = ', '.join(languages)
            if changes:
                command = create_modify_field(dict(changes, field_code=self.field_code,
                                                   field_type=kind, field_variant=variant))
                return command.apply_to_target(schema, index, context)
            return ExecutionResult(schema)
        except (TypeError, ValueError) as exc:
            return ExecutionResult.error(schema, str(exc), 'invalid_modify_field')

    def _move(self, schema, context):
        if self.position is None and self.before_after_field is None:
            return ExecutionResult(schema)
        position = AddFieldCommand(field_code=self.field_code, position=self.position,
                                   before_after_field=self.before_after_field)
        try:
            position._validate_position()
        except FieldParameterError as exc:
            return ExecutionResult.error(schema, str(exc).replace('AddField', 'MoveField'),
                                         f'invalid_{exc.parameter}')
        return MoveFieldCommand(self.field_code, position.position, self.before_after_field).execute(
            schema, context=replace(context or CommandContext(), exact_field_code=True))

    @staticmethod
    def _warnings(step, result):
        return tuple(replace(item, severity='warning', path=None,
                             message=f'{step}: {item.message}') for item in result.diagnostics)

    def _move_after_edit(self, schema, edited, context):
        current = schema if edited.application_failed else edited.schema
        moved = self._move(current, context)
        diagnostics = (self._warnings('ModifyField', edited) if edited.application_failed else edited.diagnostics)
        return ExecutionResult(current if moved.application_failed else moved.schema,
                               diagnostics + self._warnings('MoveField', moved))

    def execute(self, schema, *, context=None):
        """Apply using canonical IDs and names in the local schema cache."""
        try:
            index = self._target(schema)
            if index is None:
                return self._add(schema, context)
            target = schema['elements'][index]
            match = self._match(schema, target)
            if isinstance(match, ExecutionResult):
                return match
            kind, variant = match
            requested = self._requested_form(schema, variant)
            if requested and kind in REFERENCE_TYPES:
                requested = cached_target_form(schema, requested, context)
            mismatch = self._check_reference(schema, target, kind, requested, context)
            if mismatch is not None:
                return mismatch
            return self._move_after_edit(schema, self._modify(schema, index, kind, variant, context), context)
        except ValueError as exc:
            return ExecutionResult.error(schema, str(exc), 'invalid_add_modify_field')

    async def execute_resolved(self, schema, context, resolver):
        """Resolve edits before moving so a rejected edit cannot discard a move."""
        try:
            index = self._target(schema)
            if index is None:
                added = self._add(schema, context)
                if added.has_errors:
                    return added
                final = await resolver(added.schema)
                return replace(final, diagnostics=added.diagnostics + final.diagnostics)
            target = schema['elements'][index]
            match = self._match(schema, target)
            if isinstance(match, ExecutionResult):
                return match
            kind, variant = match
            requested = self._requested_form(schema, variant)
            related = context.related_schemas or {}
            needs_source = (kind == 'reversereference' and self.reverse_reference_field is not None
                            and requested != schema.get('id') and requested not in related)
            if requested and kind in REFERENCE_TYPES:
                requested = await resolve_target_form(schema, target, requested, resolver, load_context=needs_source)
            mismatch = self._check_reference(schema, target, kind, requested, context)
            if mismatch is not None:
                return mismatch
            edited = self._modify(schema, index, kind, variant, context)
            if not edited.application_failed and edited.schema != schema:
                try:
                    resolved = await resolver(edited.schema)
                except ValueError as exc:
                    resolved = ExecutionResult.error(schema, str(exc), 'invalid_modify_field')
                # The final resolution supplies warnings with the final field order.
                edited = resolved
            result = self._move_after_edit(schema, edited, context)
            if result.schema == schema:
                return result
            if not edited.application_failed and result.schema == edited.schema:
                return replace(edited, diagnostics=result.diagnostics)
            final = await resolver(result.schema)
            step_warnings = result.diagnostics if edited.application_failed else result.diagnostics[len(edited.diagnostics):]
            return replace(final, diagnostics=step_warnings + final.diagnostics)
        except ValueError as exc:
            return ExecutionResult.error(schema, str(exc), 'invalid_add_modify_field')


def parse_add_modify_field(text, call):
    return AddModifyFieldCommand(**parse_add_field_arguments(text, call,
        command_name=NAME, require_label=False,
        function_parameters={ARGUMENTS[key] for key in FUNCTION_KEYS},
        blank_parameters={ARGUMENTS[key] for key in EDIT_KEYS}))


def create_add_modify_field(values):
    arguments = {key: value for key, value in values.items() if key in ARGUMENTS}
    if str(arguments.get('field_type')).casefold() == 'serial':
        # AddField supplies these fixed flags and ModifyField keeps them intact.
        arguments.pop('key', None)
        arguments.pop('unique', None)
    return AddModifyFieldCommand(**arguments)


def configure(parameter):
    key = parameter.key
    if key in FUNCTION_KEYS:
        return replace(parameter, parse_text=partial(
            parse_function_input, parameter=ARGUMENTS[key], command_name=NAME))
    return parameter


ADD_MODIFY_FIELD = CommandCreatorDefinition(
    key='add_modify_field', label='Add or modify field',
    parameters=tuple(configure(parameter) for parameter in ADD_FIELD_PARAMETERS),
    create_command=create_add_modify_field,
)
COMMAND = CommandDefinition(NAME, parse_add_modify_field, ADD_MODIFY_FIELD)
