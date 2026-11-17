import ast
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..command_creator import CommandCreatorDefinition, ParameterDefinition
from ..command_parser import parse_string_parameters
from ..command_execution import ExecutionResult


@dataclass(frozen=True)
class DeleteFieldCommand:
    field_code: str

    def to_syntax(self) -> str:
        return f'DeleteField(fieldCode={json.dumps(self.field_code, ensure_ascii=False)})'

    def execute(self, schema: dict[str, Any]) -> ExecutionResult:
        if not isinstance(schema, dict):
            return ExecutionResult.error(None, 'DeleteField requires a form schema object.', 'invalid_schema')
        elements = schema.get('elements')
        if not isinstance(elements, list):
            return ExecutionResult.error(schema, 'DeleteField requires the schema to contain an elements list.', 'invalid_schema')

        result = deepcopy(schema)
        result_elements = result['elements']
        field_index = next(
            (
                index for index, element in enumerate(result_elements)
                if isinstance(element, dict) and element.get('code') == self.field_code
            ),
            None,
        )
        if field_index is None:
            return ExecutionResult.warning(schema, f'DeleteField could not find field code {self.field_code!r}. Command skipped; schema unchanged.', 'missing_target')

        result_elements.pop(field_index)
        return ExecutionResult(result)


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
