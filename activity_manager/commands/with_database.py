"""Select the database context for subsequent commands."""

import json
from dataclasses import dataclass

from ..command_creator import CommandCreatorDefinition, ParameterDefinition
from ..command_execution import ExecutionResult
from ..command_iteration import validate_pattern
from ..command_parser import parse_literal_parameters
from .models import CommandDefinition


@dataclass(frozen=True)
class WithDatabaseCommand:
    database: str | None = None
    database_pattern: str | None = None

    def __post_init__(self):
        if self.database_pattern is not None:
            if self.database is not None:
                raise ValueError('Specify exactly one of database or databasePattern.')
            validate_pattern(self.database_pattern, kind='database')
        elif not isinstance(self.database, str) or not self.database.strip():
            raise ValueError('WithDatabase requires a nonblank database.')

    def to_syntax(self):
        key, value = ('databasePattern', self.database_pattern) if self.database_pattern is not None else ('database', self.database)
        return f'WithDatabase({key}={json.dumps(value, ensure_ascii=False)})'

    def execute(self, schema, *, context=None):
        return ExecutionResult.error(None, 'WithDatabase requires a connected database directory.', 'database_unavailable')


def parse_with_database(text, call):
    values = parse_literal_parameters(text, call, 'WithDatabase',
                                      types={'database': str, 'databasePattern': str}, required=())
    return WithDatabaseCommand(values.get('database'), values.get('databasePattern'))


WITH_DATABASE = CommandCreatorDefinition(
    key='with_database', label='With Database', create_command=lambda values: WithDatabaseCommand(**values),
    parameters=(ParameterDefinition('database', 'Database Name', 'combobox',
                                    required=True, option_source='database_names'),),
)
COMMAND = CommandDefinition('WithDatabase', parse_with_database, WITH_DATABASE)
