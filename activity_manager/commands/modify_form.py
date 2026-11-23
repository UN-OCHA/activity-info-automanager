"""Edit base form settings and establish the active form scope."""

from copy import deepcopy
from dataclasses import dataclass, replace
import json

from ..command_creator import CommandCreatorDefinition, ParameterDefinition
from ..command_execution import ExecutionResult
from ..command_parser import parse_literal_parameters
from .models import CommandDefinition
from .with_form import WITH_FORM, WithFormCommand


@dataclass(frozen=True)
class ModifyFormCommand:
    current_form: bool
    form: str | None = None
    form_pattern: str | None = None
    new_name: str | None = None
    record_label_field_id: str | None = None

    def __post_init__(self):
        if type(self.current_form) is not bool:
            raise ValueError('Current Form must be a boolean.')
        if self.current_form:
            if self.form is not None or self.form_pattern is not None:
                raise ValueError('Current Form cannot be combined with form or formPattern.')
        else:
            WithFormCommand(self.form, self.form_pattern)
        if self.new_name is not None and (not isinstance(self.new_name, str) or not self.new_name.strip()):
            raise ValueError('Form name must be a nonblank string.')
        if self.record_label_field_id is not None and not isinstance(self.record_label_field_id, str):
            raise ValueError('recordLabelFieldId must be a string.')

    def to_syntax(self):
        values = {'currentForm': self.current_form}
        if not self.current_form:
            values.update({'form': self.form} if self.form_pattern is None else {'formPattern': self.form_pattern})
        if self.new_name is not None:
            values['name'] = self.new_name
        if self.record_label_field_id is not None:
            values['recordLabelFieldId'] = self.record_label_field_id
        return 'ModifyForm(' + ', '.join(f'{key}=' + (str(value) if type(value) is bool else json.dumps(value, ensure_ascii=False))
                                        for key, value in values.items()) + ')'

    def execute(self, schema, *, context=None):
        """The runner selects the form before applying this atomic edit."""
        if not self.current_form and (self.form_pattern is not None or self.form != (schema or {}).get('id')):
            return ExecutionResult.error(schema, 'ModifyForm requires a connected form loader.', 'form_unavailable')
        result = deepcopy(schema)
        if self.new_name is not None:
            result['label'] = self.new_name
        token = self.record_label_field_id
        if token is not None:
            if not token.strip():
                result.pop('recordLabelFieldId', None)
            else:
                fields = [field for field in result.get('elements', []) if isinstance(field, dict)]
                matches = [field for field in fields if field.get('id') == token]
                if not matches:
                    matches = [field for field in fields if field.get('code') == token]
                if len(matches) != 1 or not matches[0].get('id'):
                    return ExecutionResult.error(schema,
                        f'Record label field {token!r} must resolve to exactly one field ID.', 'invalid_record_label_field')
                result['recordLabelFieldId'] = matches[0]['id']
        return ExecutionResult(result, success_message=f'Modified form: {result.get("label") or result["id"]}')


def parse_modify_form(text, call):
    values = parse_literal_parameters(text, call, 'ModifyForm', required={'currentForm'},
        types={'currentForm': bool, 'form': str, 'formPattern': str, 'name': str, 'recordLabelFieldId': str},
        allow_empty={'recordLabelFieldId'})
    names = {'currentForm': 'current_form', 'formPattern': 'form_pattern', 'name': 'new_name', 'recordLabelFieldId': 'record_label_field_id'}
    return ModifyFormCommand(**{names.get(key, key): value for key, value in values.items()})


MODIFY_FORM = CommandCreatorDefinition(
    key='modify_form', label='Modify form', create_command=lambda values: ModifyFormCommand(**values),
    parameters=(ParameterDefinition('current_form', 'Current Form', 'boolean', required=True,
                                    value_type='boolean', default=True),
        *(replace(parameter, visible_when=('current_form', (False,))) for parameter in WITH_FORM.parameters),
        ParameterDefinition('new_name', 'Name', 'text', validate=lambda value:
                            None if isinstance(value, str) and value.strip() else 'Form name is required.'),
        ParameterDefinition('record_label_field_id', 'Record label field', 'combobox',
                            option_source='form_field_codes')),
)
COMMAND = CommandDefinition('ModifyForm', parse_modify_form, MODIFY_FORM)
