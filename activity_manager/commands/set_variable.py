"""Text variables sourced from literals or server-evaluated data queries."""

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass, replace

from ..command_creator import CommandCreatorDefinition, ParameterDefinition as P
from ..command_execution import Diagnostic, ExecutionResult
from ..command_parser import parse_literal_parameters
from ..field_parameters import FIELD_PARAMETERS
from ..schema_names import resolve_form_names
from .models import CommandDefinition


def variable_text(value):
    if type(value) not in (str, int, float, bool) or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError('Variable value must be nonblank text, a finite number, or a boolean.')
    text = str(value)
    if not text.strip():
        raise ValueError('Variable value cannot be empty or whitespace only.')
    return text


def name_error(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
        return 'Variable name must start with a letter or underscore and contain only ASCII letters, digits, and underscores.'


@dataclass(frozen=True)
class SetVariableCommand:
    name: str
    method: str
    value: str | None = None
    referenced_form: str | None = None
    filter: str | None = None
    return_formula: str | None = None

    def __post_init__(self):
        error = name_error(self.name)
        if error:
            raise ValueError(error)
        if not isinstance(self.method, str) or self.method.casefold() not in ('literal', 'lookup'):
            raise ValueError('SetVariable method must be literal or lookup.')
        object.__setattr__(self, 'method', self.method.casefold())
        if self.method == 'literal':
            object.__setattr__(self, 'value', variable_text(self.value))
            if any(value is not None for value in (self.referenced_form, self.filter, self.return_formula)):
                raise ValueError('Literal method only accepts value.')
        else:
            if self.value is not None:
                raise ValueError('Lookup method does not accept value.')
            for label, value in [('referencedForm', self.referenced_form), ('filter', self.filter), ('returnFormula', self.return_formula)]:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f'Lookup method requires nonblank {label}.')

    def to_syntax(self):
        values = {'name': self.name, 'method': self.method}
        if self.method == 'literal':
            values['value'] = self.value
        else:
            values.update(referencedForm=self.referenced_form, filter=self.filter, returnFormula=self.return_formula)
        return 'SetVariable(' + ', '.join(f'{key}={json.dumps(value, ensure_ascii=False)}' for key, value in values.items()) + ')'

    def store(self, schema, context, value, diagnostics=()):
        text = variable_text(value)
        if context is None or context.variables is None:
            raise ValueError('SetVariable requires a variable execution context.')
        context.variables[f'vars.{self.name}'] = text
        return ExecutionResult(deepcopy(schema), diagnostics,
            success_message=f'Variable vars.{self.name} set to {json.dumps(text, ensure_ascii=False)}.')

    def execute(self, schema, *, context=None):
        if self.method == 'lookup':
            return ExecutionResult.error(schema, 'Lookup requires a connected query service.', 'lookup_unavailable')
        return self.store(schema, context, self.value)

    async def execute_lookup(self, schema, context, lookup):
        if lookup is None:
            return self.execute(schema, context=context)
        rows = await lookup(self, schema)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError('Lookup returned an invalid row response.')
        if not rows:
            raise ValueError(f'Lookup for variable {self.name!r} returned no records.')
        diagnostics = (Diagnostic('warning',
            f'Lookup for variable {self.name!r} matched multiple records; the first record in API order was used.',
            code='multiple_lookup_matches'),) if len(rows) > 1 else ()
        return self.store(schema, context, rows[0].get('value'), diagnostics)


async def query_variable(service, command, schema, related_schemas=None):
    # Use exactly the reference-form resolution used for AddField.
    probe = {**schema, 'elements': [{'type': 'reference', 'typeParameters': {
        'range': [{'formId': command.referenced_form}]}}]}
    resolved = await resolve_form_names(service, probe, related_schemas, load_related=False)
    form_id = resolved['elements'][0]['typeParameters']['range'][0]['formId']
    return await service.query_rows({
        'rowSources': [{'rootFormId': form_id}],
        'columns': [{'id': 'value', 'formula': command.return_formula}],
        'filter': command.filter,
    })


def create_set_variable(values):
    return SetVariableCommand(**values)


def parse_set_variable(text, call):
    values = parse_literal_parameters(text, call, 'SetVariable', types={
        'name': str, 'method': str, 'value': (str, int, float, bool),
        'referencedForm': str, 'filter': str, 'returnFormula': str,
    }, required={'name', 'method'})
    if 'referencedForm' in values:
        values['referenced_form'] = values.pop('referencedForm')
    if 'returnFormula' in values:
        values['return_formula'] = values.pop('returnFormula')
    return SetVariableCommand(**values)


LOOKUP = ('method', ('lookup',))
LITERAL = ('method', ('literal',))
SET_VARIABLE = CommandCreatorDefinition(
    key='set_variable', label='Set variable', create_command=create_set_variable,
    parameters=(
        P('name', 'Variable name', 'text', required=True, validate=name_error),
        P('method', 'Method', 'select', required=True, options=('literal', 'lookup'), default='literal'),
        P('value', 'Value', 'text', visible_when=LITERAL, required_when=LITERAL),
        replace(next(p for p in FIELD_PARAMETERS if p.key == 'referenced_form'),
                visible_when=LOOKUP, required_when=LOOKUP),
        P('filter', 'Filter formula', 'text', visible_when=LOOKUP, required_when=LOOKUP),
        P('return_formula', 'Return formula', 'text', visible_when=LOOKUP, required_when=LOOKUP),
    ),
)
COMMAND = CommandDefinition('SetVariable', parse_set_variable, SET_VARIABLE)
