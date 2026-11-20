"""Move selected forms within their database and establish the active scope."""

from dataclasses import dataclass, replace
import json

from ..command_creator import CommandCreatorDefinition, ParameterDefinition
from ..command_execution import ExecutionResult
from ..command_parser import parse_literal_parameters
from .models import CommandDefinition
from .with_form import WITH_FORM, WithFormCommand


@dataclass(frozen=True)
class MoveFormCommand:
    current_form: bool
    folder: str
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
        if not isinstance(self.folder, str) or not self.folder.strip():
            raise ValueError('MoveForm requires a destination folder.')

    def to_syntax(self):
        values = {'currentForm': self.current_form}
        if not self.current_form:
            values.update({'form': self.form} if self.form_pattern is None else {'formPattern': self.form_pattern})
        values['folder'] = self.folder
        arguments = [f'{key}=' + (str(value) if type(value) is bool else json.dumps(value, ensure_ascii=False))
                     for key, value in values.items()]
        return 'MoveForm(' + ', '.join(arguments) + ')'

    def execute(self, schema, *, context=None):
        return ExecutionResult.error(schema, 'MoveForm requires a connected folder loader.', 'folder_unavailable')


def parse_move_form(text, call):
    values = parse_literal_parameters(text, call, 'MoveForm', required={'currentForm', 'folder'},
                                      types={'currentForm': bool, 'form': str, 'formPattern': str, 'folder': str})
    values['current_form'] = values.pop('currentForm')
    if 'formPattern' in values:
        values['form_pattern'] = values.pop('formPattern')
    return MoveFormCommand(**values)


MOVE_FORM = CommandCreatorDefinition(
    key='move_form', label='Move form', create_command=lambda values: MoveFormCommand(**values),
    parameters=(ParameterDefinition('current_form', 'Current Form', 'boolean', required=True,
                                    value_type='boolean', default=True),
        *(replace(parameter, visible_when=('current_form', (False,))) for parameter in WITH_FORM.parameters),
        ParameterDefinition('folder', 'Destination folder', 'combobox', required=True, option_source='folder_paths',
                            help='Folder name, full path or ID in the target database. / selects the database root.')),
)
COMMAND = CommandDefinition('MoveForm', parse_move_form, MOVE_FORM)
