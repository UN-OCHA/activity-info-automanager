"""Shared command/resolver results and ordered console diagnostics."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from .schema_names import readable_schema, unresolved_formula_references


ExecutionStatus = Literal['applied', 'warning', 'error', 'not_executed', 'resolving']
EXECUTABLE_STATUSES = frozenset({'applied', 'warning'})


@dataclass(frozen=True)
class Diagnostic:
    severity: Literal['error', 'warning']
    message: str
    path: tuple | None = None
    code: str | None = None

    def console_line(self):
        return f'{self.severity.title()}: {self.message}'


@dataclass(frozen=True)
class CommandContext:
    """Cached schema context and the current evaluation's text variables."""

    related_schemas: dict | None = None
    database_names: dict | None = None
    variables: dict | None = None
    reserved_identities: set | None = None


@dataclass(frozen=True)
class ExecutionResult:
    schema: dict | None
    diagnostics: tuple[Diagnostic, ...] = ()
    names_schema: dict | None = None
    success_message: str | None = None
    opschema: tuple[dict, ...] = ()
    snapshot_issues: tuple[Diagnostic, ...] | None = None
    form_issues: dict | None = None

    @property
    def has_errors(self):
        return any(item.severity == 'error' for item in self.diagnostics)

    @property
    def status(self) -> ExecutionStatus:
        return 'error' if self.has_errors else 'warning' if self.diagnostics else 'applied'

    @property
    def message(self):
        lines = [self.success_message] if self.success_message and not self.has_errors else []
        lines.extend(item.console_line() for item in self.diagnostics)
        return '\n'.join(lines) or 'Command successfully applied to schema.'

    @property
    def issues(self):
        if self.snapshot_issues is not None:
            return self.snapshot_issues
        return tuple(item for item in self.diagnostics if item.path is not None)

    @classmethod
    def error(cls, schema, message, code=None):
        return cls(deepcopy(schema), (Diagnostic('error', message, code=code),))

    @classmethod
    def warning(cls, schema, message, code=None):
        return cls(deepcopy(schema), (Diagnostic('warning', message, code=code),))


def transform_command(command, schema, context=None):
    """Catch unexpected failures at the application boundary, never in the UI."""
    try:
        from .command_iteration import PreparedCommand
        from .command_templates import command_parameters
        if not isinstance(command, PreparedCommand):
            try:
                command = command_parameters(command, context.variables if context and context.variables is not None else {})
            except ValueError as exc:
                return ExecutionResult.error(schema, str(exc), 'invalid_parameter')
        result = command.execute(schema, context=context)
        if not isinstance(result, ExecutionResult):
            raise TypeError('Command.execute must return ExecutionResult.')
        return result
    except Exception as exc:
        return ExecutionResult.error(schema, f'{type(exc).__name__}: {exc}', 'unexpected_command_error')


def apply_command_locally(command, schema, related_schemas=None, database_names=None, *, variables=None, reserved_identities=None):
    """Application without a connected resolver still uses the same contract."""
    result = transform_command(command, schema, CommandContext(related_schemas, database_names, variables if variables is not None else {}, reserved_identities))
    if result.has_errors:
        return result
    diagnostics = tuple(Diagnostic('warning', f'Unresolved formula reference {token!r}.', path)
        for path, token, _ in unresolved_formula_references(result.schema, related_schemas))
    return ExecutionResult(result.schema, result.diagnostics + diagnostics,
        readable_schema(result.schema, related_schemas, database_names), result.success_message)


async def apply_command(command, schema, resolver, *, related_schemas=None, database_names=None, variables=None, lookup_variable=None, reserved_identities=None):
    """Run transformation then resolution, preserving diagnostics in order."""
    from .commands.set_variable import SetVariableCommand
    from .command_iteration import PreparedCommand
    from .command_templates import command_parameters
    context = CommandContext(related_schemas, database_names, variables if variables is not None else {}, reserved_identities)
    try:
        prepared = command.command if isinstance(command, PreparedCommand) else command_parameters(command, context.variables)
    except ValueError as exc:
        return ExecutionResult.error(schema, str(exc), 'invalid_parameter')
    except Exception as exc:
        return ExecutionResult.error(schema, f'{type(exc).__name__}: {exc}', 'unexpected_command_error')
    if isinstance(prepared, SetVariableCommand):
        try:
            result = (await prepared.execute_lookup(schema, context, lookup_variable)
                      if prepared.method == 'lookup' else prepared.execute(schema, context=context))
            return ExecutionResult(result.schema, result.diagnostics,
                readable_schema(result.schema, related_schemas, database_names), result.success_message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ExecutionResult.error(schema, str(exc), 'invalid_variable')
    result = transform_command(PreparedCommand(prepared), schema, context)
    if result.has_errors:
        return result
    try:
        resolved = await resolver(result.schema)
        if not isinstance(resolved, ExecutionResult):
            raise TypeError('The name resolver must return ExecutionResult.')
        return ExecutionResult(resolved.schema, result.diagnostics + resolved.diagnostics,
                               resolved.names_schema, result.success_message)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return ExecutionResult(result.schema, result.diagnostics + (
            Diagnostic('error', f'{type(exc).__name__}: {exc}', code='unexpected_resolution_error'),))
