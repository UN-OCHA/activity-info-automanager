"""Select, create, or move one top-level form by its exact name."""

from dataclasses import dataclass, replace
import json

from .add_form import ADD_FORM, AddFormCommand
from .models import CommandDefinition
from ..command_creator import CommandCreatorDefinition
from ..command_execution import ExecutionResult
from ..command_parser import parse_literal_parameters


@dataclass(frozen=True)
class AddModifyFormCommand:
    form_name: str
    folder: str | None = None

    def __post_init__(self):
        self.as_add_form()

    def as_add_form(self):
        if self.folder is None:
            return AddFormCommand(self.form_name)
        return AddFormCommand(self.form_name, self.folder)

    def to_syntax(self):
        arguments = [f'name={json.dumps(self.form_name, ensure_ascii=False)}']
        if self.folder is not None:
            arguments.append(f'folder={json.dumps(self.folder, ensure_ascii=False)}')
        return f'AddModifyForm({", ".join(arguments)})'

    def execute(self, schema, *, context=None):
        return ExecutionResult.error(
            schema, 'AddModifyForm requires a connected database resource loader.', 'database_unavailable')


def parse_add_modify_form(text, call):
    values = parse_literal_parameters(text, call, 'AddModifyForm', required={'name'},
                                      types={'name': str, 'folder': str})
    return AddModifyFormCommand(values['name'], values.get('folder'))


name_parameter, folder_parameter = ADD_FORM.parameters
ADD_MODIFY_FORM = CommandCreatorDefinition(
    key='add_modify_form', label='Add or modify form',
    create_command=lambda values: AddModifyFormCommand(**values),
    parameters=(name_parameter, replace(folder_parameter, required=False, default=None, blank_action='unset',
        help='Folder name, path or ID in the active database. Omit to keep an existing form in place or create at the root.')),
)
COMMAND = CommandDefinition('AddModifyForm', parse_add_modify_form, ADD_MODIFY_FORM)
