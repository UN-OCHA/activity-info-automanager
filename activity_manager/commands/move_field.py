import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..command_creator import CommandCreatorDefinition, ParameterDefinition


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

    def execute(self, schema: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(schema, dict):
            raise TypeError('MoveField requires a form schema object.')
        elements = schema.get('elements')
        if not isinstance(elements, list):
            raise ValueError('MoveField requires the schema to contain an elements list.')
        if self.position not in ('Beginning', 'End', 'Before field', 'After field'):
            raise ValueError(f'MoveField has an invalid position: {self.position!r}.')

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
            raise ValueError(f'MoveField could not find field code {self.field_code!r}.')

        if self.position in ('Before field', 'After field'):
            if not self.before_after_field:
                raise ValueError('MoveField requires beforeAfterField for this position.')
            if self.before_after_field == self.field_code:
                raise ValueError('MoveField cannot position a field relative to itself.')

        field = result_elements.pop(field_index)
        if self.position == 'Beginning':
            result_elements.insert(0, field)
        elif self.position == 'End':
            result_elements.append(field)
        else:
            relative_index = next(
                (
                    index for index, element in enumerate(result_elements)
                    if isinstance(element, dict)
                    and element.get('code') == self.before_after_field
                ),
                None,
            )
            if relative_index is None:
                raise ValueError(
                    f'MoveField could not find before/after field code '
                    f'{self.before_after_field!r}.'
                )
            insertion_index = relative_index + (self.position == 'After field')
            result_elements.insert(insertion_index, field)
        return result


def create_move_field(values: Mapping[str, Any]) -> MoveFieldCommand:
    return MoveFieldCommand(
        field_code=values['field_code'],
        position=values['position'],
        before_after_field=values.get('relative_field_code'),
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
