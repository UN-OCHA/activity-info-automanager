import ast
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..command_creator import CommandCreatorDefinition, ParameterDefinition
from ..command_parser import parse_string_parameters
from .field_selector import find_field_index
from .models import CommandDefinition
from ..command_execution import ExecutionResult


@dataclass(frozen=True)
class DeleteFieldCommand:
    field_code: str

    def to_syntax(self) -> str:
        return f'DeleteField(fieldCode={json.dumps(self.field_code, ensure_ascii=False)})'

    def execute(self, schema: dict[str, Any], *, context=None) -> ExecutionResult:
        if not isinstance(schema, dict):
            return ExecutionResult.error(None, 'DeleteField requires a form schema object.', 'invalid_schema')
        elements = schema.get('elements')
        if not isinstance(elements, list):
            return ExecutionResult.error(schema, 'DeleteField requires the schema to contain an elements list.', 'invalid_schema')

        result = deepcopy(schema)
        result_elements = result['elements']
        try:
            field_index = find_field_index(self.field_code, result_elements,
                                           exact_code=bool(context and context.exact_field_code))
        except ValueError as exc:
            return ExecutionResult.error(schema, f'DeleteField: {exc}', 'ambiguous_target')
        if field_index is None:
            return ExecutionResult.warning(schema, f'DeleteField could not find field {self.field_code!r}. Command skipped; schema unchanged.', 'missing_target')

        field = result_elements.pop(field_index)
        return ExecutionResult(result, target_member_ids=(field['id'],) if field.get('id') else ())


def create_delete_field(values: Mapping[str, Any]) -> DeleteFieldCommand:
    return DeleteFieldCommand(field_code=values['field_code'])


def parse_delete_field(text: str, call: ast.Call) -> DeleteFieldCommand:
    """Parse and validate a DeleteField call from the Text view."""
    values = parse_string_parameters(
        text,
        call,
        'DeleteField',
        allowed={'fieldCode'},
        required={'fieldCode'},
    )
    return DeleteFieldCommand(field_code=values['fieldCode'])


DELETE_FIELD = CommandCreatorDefinition(
    key='delete_field',
    label='Delete field',
    create_command=create_delete_field,
    parameters=(
        ParameterDefinition(
            key='field_code',
            label='Field code',
            control='combobox',
            required=True,
            option_source='field_codes',
        ),
    ),
)


COMMAND = CommandDefinition('DeleteField', parse_delete_field, DELETE_FIELD)
