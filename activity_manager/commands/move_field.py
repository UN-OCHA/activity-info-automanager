import ast
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..command_creator import CommandCreatorDefinition, ParameterDefinition
from ..command_parser import command_error, parse_string_parameters
from .field_selector import find_field_index
from .models import CommandDefinition
from ..command_execution import ExecutionResult


@dataclass(frozen=True)
class MoveFieldCommand:
    field_code: str
    position: str
    before_after_field: str | None = None

    def to_syntax(self) -> str:
        arguments = [
            f'fieldCode={json.dumps(self.field_code, ensure_ascii=False)}',
            f'position={json.dumps(self.position, ensure_ascii=False)}',
        ]
        if self.position in ('Before field', 'After field'):
            arguments.append(
                f'beforeAfterField={json.dumps(self.before_after_field, ensure_ascii=False)}'
            )
        return f'MoveField({", ".join(arguments)})'

    def execute(self, schema: dict[str, Any], *, context=None) -> ExecutionResult:
        if not isinstance(schema, dict):
            return ExecutionResult.error(None, 'MoveField requires a form schema object.', 'invalid_schema')
        elements = schema.get('elements')
        if not isinstance(elements, list):
            return ExecutionResult.error(schema, 'MoveField requires the schema to contain an elements list.', 'invalid_schema')
        if self.position not in ('Beginning', 'End', 'Before field', 'After field'):
            return ExecutionResult.error(schema, f'MoveField has an invalid position: {self.position!r}.', 'invalid_position')

        try:
            field_index = find_field_index(self.field_code, elements,
                                           exact_code=bool(context and context.exact_field_code))
        except ValueError as exc:
            return ExecutionResult.error(schema, f'MoveField: {exc}', 'ambiguous_target')
        if field_index is None:
            return ExecutionResult.warning(schema, f'MoveField could not find field {self.field_code!r}. Command skipped; schema unchanged.', 'missing_target')

        if self.position in ('Before field', 'After field'):
            if not self.before_after_field:
                return ExecutionResult.error(schema, 'MoveField requires beforeAfterField for this position.', 'missing_relative_field')
            try:
                relative_index = find_field_index(self.before_after_field, elements,
                                                  exact_code=bool(context and context.exact_anchor))
            except ValueError as exc:
                return ExecutionResult.error(schema, f'MoveField beforeAfterField: {exc}', 'ambiguous_relative_target')
            if relative_index is None:
                return ExecutionResult.warning(schema, f'MoveField could not find before/after field {self.before_after_field!r}. Command skipped; schema unchanged.', 'missing_relative_target')
            if relative_index == field_index:
                return ExecutionResult.error(schema, 'MoveField cannot position a field relative to itself.', 'self_relative_move')

        result = deepcopy(schema)
        result_elements = result['elements']
        field = result_elements.pop(field_index)
        if self.position == 'Beginning':
            result_elements.insert(0, field)
        elif self.position == 'End':
            result_elements.append(field)
        else:
            insertion_index = relative_index - (field_index < relative_index) + (self.position == 'After field')
            result_elements.insert(insertion_index, field)
        return ExecutionResult(result, target_member_ids=(field['id'],) if field.get('id') else ())


def create_move_field(values: Mapping[str, Any]) -> MoveFieldCommand:
    return MoveFieldCommand(
        field_code=values['field_code'],
        position=values['position'],
        before_after_field=values.get('relative_field_code'),
    )


def parse_move_field(text: str, call: ast.Call) -> MoveFieldCommand:
    """Parse and validate a MoveField call from the Text view."""
    values = parse_string_parameters(
        text,
        call,
        'MoveField',
        allowed={'fieldCode', 'position', 'beforeAfterField'},
        required={'fieldCode', 'position'},
    )
    allowed_positions = ('Beginning', 'End', 'Before field', 'After field')
    positions_by_case = {position.casefold(): position for position in allowed_positions}
    position = positions_by_case.get(values['position'].casefold())
    if position is None:
        position_node = next(
            keyword.value
            for keyword in call.keywords
            if keyword.arg and keyword.arg.casefold() == 'position'
        )
        raise command_error(
            text,
            position_node,
            f'MoveField has an invalid position {values["position"]!r}.',
        )

    relative_field = values.get('beforeAfterField')
    if position in ('Before field', 'After field') and relative_field is None:
        raise command_error(
            text,
            call,
            f'MoveField requires parameter {"beforeAfterField"!r} for position {position!r}.',
        )
    if position in ('Beginning', 'End') and relative_field is not None:
        relative_node = next(
            keyword
            for keyword in call.keywords
            if keyword.arg and keyword.arg.casefold() == 'beforeafterfield'
        )
        raise command_error(
            text,
            relative_node,
            f'MoveField does not use parameter {"beforeAfterField"!r} '
            f'for position {position!r}.',
        )
    return MoveFieldCommand(
        field_code=values['fieldCode'],
        position=position,
        before_after_field=relative_field,
    )


MOVE_FIELD = CommandCreatorDefinition(
    key='move_field',
    label='Move field',
    create_command=create_move_field,
    parameters=(
        ParameterDefinition(
            key='field_code',
            label='Field code',
            control='combobox',
            required=True,
            option_source='field_codes',
        ),
        ParameterDefinition(
            key='position',
            label='Position',
            control='single_select',
            required=True,
            options=('Beginning', 'End', 'Before field', 'After field'),
        ),
        ParameterDefinition(
            key='relative_field_code',
            label='Before/After field',
            control='combobox',
            required=True,
            option_source='field_codes',
            visible_when=('position', ('Before field', 'After field')),
        ),
    ),
)


COMMAND = CommandDefinition('MoveField', parse_move_field, MOVE_FIELD)
