"""Move selected forms within their database and establish the active scope."""

from dataclasses import dataclass
import json

from ..command_creator import CommandCreatorDefinition, ParameterDefinition
from ..command_execution import ExecutionResult
from ..command_parser import parse_literal_parameters
from .models import CommandDefinition
from .with_form import WITH_FORM, WithFormCommand


@dataclass(frozen=True)
class MoveFormCommand:
    folder: str
    form: str | None = None
    form_pattern: str | None = None

    def __post_init__(self):
        WithFormCommand(self.form, self.form_pattern)
        if not isinstance(self.folder, str) or not self.folder.strip():
            raise ValueError('MoveForm requires a destination folder.')

    def to_syntax(self):
        values = {'form': self.form} if self.form_pattern is None else {'formPattern': self.form_pattern}
        values['folder'] = self.folder
        return 'MoveForm(' + ', '.join(f'{key}={json.dumps(value, ensure_ascii=False)}'
                                      for key, value in values.items()) + ')'

    def execute(self, schema, *, context=None):
        return ExecutionResult.error(schema, 'MoveForm requires a connected folder loader.', 'folder_unavailable')


def parse_move_form(text, call):
    values = parse_literal_parameters(text, call, 'MoveForm', required={'folder'},
                                      types={'form': str, 'formPattern': str, 'folder': str})
    if 'formPattern' in values:
        values['form_pattern'] = values.pop('formPattern')
    return MoveFormCommand(**values)


MOVE_FORM = CommandCreatorDefinition(
    key='move_form', label='Move form', create_command=lambda values: MoveFormCommand(**values),
    parameters=(*WITH_FORM.parameters,
        ParameterDefinition('folder', 'Destination folder', 'combobox', required=True, option_source='folder_paths',
                            help='Folder name, full path or ID in the target database. / selects the database root.')),
)
COMMAND = CommandDefinition('MoveForm', parse_move_form, MOVE_FORM)
