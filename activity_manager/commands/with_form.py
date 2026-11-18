"""Select the active form for subsequent commands."""

import json
from dataclasses import dataclass, replace

from ..command_creator import CommandCreatorDefinition
from ..command_execution import ExecutionResult
from ..command_parser import parse_literal_parameters
from ..field_parameters import FIELD_PARAMETERS
from .models import CommandDefinition


@dataclass(frozen=True)
class WithFormCommand:
    form: str | None = None
    form_pattern: str | None = None

    def __post_init__(self):
        if self.form_pattern is not None:
            if self.form is not None:
                raise ValueError('Specify exactly one of form or formPattern.')
            from ..command_iteration import validate_pattern
            validate_pattern(self.form_pattern, kind='form')
        elif not isinstance(self.form, str) or not self.form.strip():
            raise ValueError('WithForm requires a nonblank form.')

    def to_syntax(self):
        if self.form_pattern is not None:
            return f'WithForm(formPattern={json.dumps(self.form_pattern, ensure_ascii=False)})'
        return f'WithForm(form={json.dumps(self.form, ensure_ascii=False)})'

    def execute(self, schema, *, context=None):
        return ExecutionResult.error(schema, 'WithForm requires a connected form loader.', 'form_unavailable')


def parse_with_form(text, call):
    values = parse_literal_parameters(text, call, 'WithForm', types={'form': str, 'formPattern': str}, required=())
    return WithFormCommand(**{('form_pattern' if key == 'formPattern' else key): value for key, value in values.items()})


WITH_FORM = CommandCreatorDefinition(
    key='with_form', label='With form', create_command=lambda values: WithFormCommand(**values),
    parameters=(replace(next(p for p in FIELD_PARAMETERS if p.key == 'referenced_form'),
                        key='form', label='Form Name', visible_when=None, required=True),),
)
COMMAND = CommandDefinition('WithForm', parse_with_form, WITH_FORM)
