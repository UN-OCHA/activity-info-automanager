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


DATA_LOOKUP = 'data lookup'
LOOKUP_MODES = ('first', 'all', 'unique')
DEFAULT_SEPARATOR = '|'
MAX_SEPARATOR_LENGTH = 3


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
    mode: str | None = None
    separator: str | None = None

    def __post_init__(self):
        error = name_error(self.name)
        if error:
            raise ValueError(error)
        method = self.method.casefold() if isinstance(self.method, str) else None
        if method == 'lookup':
            method = DATA_LOOKUP
        if method not in ('literal', DATA_LOOKUP):
            raise ValueError('SetVariable method must be literal or data lookup.')
        object.__setattr__(self, 'method', method)
        if method == DATA_LOOKUP:
            mode = 'first' if self.mode is None else self.mode
            if not isinstance(mode, str) or mode.casefold() not in LOOKUP_MODES:
                raise ValueError('Mode must be First, All, or Unique.')
            object.__setattr__(self, 'mode', mode.casefold())
        elif self.mode is not None:
            raise ValueError('Mode requires the data lookup method.')
        if self.separator is not None:
            if self.mode not in ('all', 'unique'):
                raise ValueError('Separator requires All or Unique mode.')
            if not isinstance(self.separator, str) or len(self.separator) > MAX_SEPARATOR_LENGTH:
                raise ValueError(f'Separator must be text of at most {MAX_SEPARATOR_LENGTH} characters.')
        if self.method == 'literal':
            object.__setattr__(self, 'value', variable_text(self.value))
            if any(value is not None for value in (self.referenced_form, self.filter, self.return_formula)):
                raise ValueError('Literal method only accepts value.')
        else:
            if self.value is not None:
                raise ValueError('Data lookup method does not accept value.')
            for label, value in [('referencedForm', self.referenced_form), ('filter', self.filter), ('returnFormula', self.return_formula)]:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f'Data lookup method requires nonblank {label}.')

    def to_syntax(self):
        values = {'name': self.name, 'method': self.method}
        if self.method == 'literal':
            values['value'] = self.value
        else:
            values.update(referencedForm=self.referenced_form, filter=self.filter,
                          returnFormula=self.return_formula, mode=self.mode)
        if self.separator is not None:
            values['separator'] = self.separator
        return 'SetVariable(' + ', '.join(f'{key}={json.dumps(value, ensure_ascii=False)}' for key, value in values.items()) + ')'

    def store(self, schema, context, value, diagnostics=()):
        text = variable_text(value)
        if context is None or context.variables is None:
            raise ValueError('SetVariable requires a variable execution context.')
        context.variables[f'vars.{self.name}'] = text
        return ExecutionResult(deepcopy(schema), diagnostics,
            success_message=f'Variable vars.{self.name} set to {json.dumps(text, ensure_ascii=False)}.')

    def execute(self, schema, *, context=None):
        if self.method == DATA_LOOKUP:
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
        if self.mode in ('all', 'unique'):
            separator = DEFAULT_SEPARATOR if self.separator is None else self.separator
            values = [variable_text(row.get('value')) for row in rows]
            if self.mode == 'unique':
                values = list(dict.fromkeys(values))
            return self.store(schema, context, separator.join(values))
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
    if values.get('separator') == DEFAULT_SEPARATOR:
        values = {key: value for key, value in values.items() if key != 'separator'}
    return SetVariableCommand(**values)


def parse_set_variable(text, call):
    values = parse_literal_parameters(text, call, 'SetVariable', types={
        'name': str, 'method': str, 'value': (str, int, float, bool),
        'referencedForm': str, 'filter': str, 'returnFormula': str,
        'mode': str, 'separator': str, 'multiple': bool,
    }, required={'name', 'method'}, allow_empty={'separator'})
    if 'referencedForm' in values:
        values['referenced_form'] = values.pop('referencedForm')
    if 'returnFormula' in values:
        values['return_formula'] = values.pop('returnFormula')
    if 'multiple' in values:
        multiple = values.pop('multiple')
        if 'mode' in values:
            raise ValueError('Specify only mode when replacing the previous multiple parameter.')
        if multiple:
            values['mode'] = 'all'
    return SetVariableCommand(**values)


LOOKUP = ('method', (DATA_LOOKUP,))
LITERAL = ('method', ('literal',))
SET_VARIABLE = CommandCreatorDefinition(
    key='set_variable', label='Set variable', create_command=create_set_variable,
    parameters=(
        P('name', 'Variable name', 'text', required=True, validate=name_error),
        P('method', 'Method', 'select', required=True, options=('literal', DATA_LOOKUP), default='literal'),
        P('value', 'Value', 'text', visible_when=LITERAL, required_when=LITERAL),
        replace(next(p for p in FIELD_PARAMETERS if p.key == 'referenced_form'),
                visible_when=LOOKUP, required_when=LOOKUP),
        P('filter', 'Filter formula', 'text', visible_when=LOOKUP, required_when=LOOKUP),
        P('return_formula', 'Return formula', 'text', visible_when=LOOKUP, required_when=LOOKUP),
        P('mode', 'Mode', 'single_select', options=LOOKUP_MODES, default='first',
          option_labels={mode: mode.title() for mode in LOOKUP_MODES}, visible_when=LOOKUP, required_when=LOOKUP),
        P('separator', 'Separator', 'text', default=DEFAULT_SEPARATOR, initially_active=True,
          visible_when=lambda values: values.get('method') == DATA_LOOKUP and values.get('mode') in ('all', 'unique'),
          max_length=MAX_SEPARATOR_LENGTH, preserve_whitespace=True),
    ),
)
COMMAND = CommandDefinition('SetVariable', parse_set_variable, SET_VARIABLE)
