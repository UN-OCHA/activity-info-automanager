"""Create one top-level form in the active database."""

from dataclasses import dataclass
import json

from ..command_creator import CommandCreatorDefinition, ParameterDefinition
from ..command_execution import ExecutionResult
from ..command_parser import parse_literal_parameters
from .models import CommandDefinition


@dataclass(frozen=True)
class AddFormCommand:
    form_name: str
    folder: str = '/'

    def __post_init__(self):
        if not isinstance(self.form_name, str) or not self.form_name.strip():
            raise ValueError('Form name is required.')
        if not isinstance(self.folder, str) or not self.folder.strip():
            raise ValueError('Destination folder is required.')

    def to_syntax(self):
        return 'AddForm(name=' + json.dumps(self.form_name, ensure_ascii=False) + ', folder=' + json.dumps(self.folder, ensure_ascii=False) + ')'

    def execute(self, schema, *, context=None):
        return ExecutionResult.error(schema, 'AddForm requires a connected database resource loader.', 'database_unavailable')


def parse_add_form(text, call):
    values = parse_literal_parameters(text, call, 'AddForm', required={'name'}, types={'name': str, 'folder': str})
    values['form_name'] = values.pop('name')
    return AddFormCommand(**values)


ADD_FORM = CommandCreatorDefinition(
    key='add_form', label='Add form', create_command=lambda values: AddFormCommand(**values),
    parameters=(ParameterDefinition('form_name', 'Name', 'combobox', required=True, option_source='form_names'),
        ParameterDefinition('folder', 'Destination folder', 'combobox', required=True, default='/',
            option_source='folder_paths', help='Folder name, full path or ID in the active database. / selects the database root.')),
)
COMMAND = CommandDefinition('AddForm', parse_add_form, ADD_FORM)
