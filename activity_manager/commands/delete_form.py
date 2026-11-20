"""Delete selected forms without establishing a new active form."""
from dataclasses import dataclass, replace
import json

from ..command_creator import CommandCreatorDefinition, ParameterDefinition
from ..command_execution import ExecutionResult
from ..command_parser import parse_literal_parameters
from .models import CommandDefinition
from .with_form import WITH_FORM, WithFormCommand


@dataclass(frozen=True)
class DeleteFormCommand:
    current_form: bool
    form: str | None = None
    form_pattern: str | None = None

    def __post_init__(self):
        if type(self.current_form) is not bool:
            raise ValueError('Current Form must be a boolean.')
        if self.current_form:
            if self.form is not None or self.form_pattern is not None:
                raise ValueError('Current Form cannot be combined with form or formPattern.')
        else:
            WithFormCommand(self.form, self.form_pattern)

    def to_syntax(self):
        values = {'currentForm': self.current_form}
        if not self.current_form:
            values.update({'form': self.form} if self.form_pattern is None else {'formPattern': self.form_pattern})
        return 'DeleteForm(' + ', '.join(f'{key}=' + (str(value) if type(value) is bool else json.dumps(value, ensure_ascii=False))
                                        for key, value in values.items()) + ')'

    def execute(self, schema, *, context=None):
        return ExecutionResult.error(schema, 'DeleteForm requires a connected form loader.', 'form_unavailable')


def parse_delete_form(text, call):
    values = parse_literal_parameters(text, call, 'DeleteForm', required={'currentForm'},
        types={'currentForm': bool, 'form': str, 'formPattern': str})
    values['current_form'] = values.pop('currentForm')
    if 'formPattern' in values:
        values['form_pattern'] = values.pop('formPattern')
    return DeleteFormCommand(**values)


DELETE_FORM = CommandCreatorDefinition(
    key='delete_form', label='Delete form', create_command=lambda values: DeleteFormCommand(**values),
    parameters=(ParameterDefinition('current_form', 'Current Form', 'boolean', required=True,
                                    value_type='boolean', default=True),
        *(replace(parameter, visible_when=('current_form', (False,))) for parameter in WITH_FORM.parameters)),
)
COMMAND = CommandDefinition('DeleteForm', parse_delete_form, DELETE_FORM)
