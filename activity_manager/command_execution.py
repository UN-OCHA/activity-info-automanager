"""Shared command/resolver results and ordered console diagnostics."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, Any

from .immutable import FrozenMap, command_parameters_snapshot

if TYPE_CHECKING:
    from .application_results import DisplayState, DiagnosticAttachment
    from .command_iteration import IterationTarget
from .schema_names import readable_schema, unresolved_formula_references


ExecutionStatus = Literal['applied', 'warning', 'error', 'not_executed', 'resolving']
EXECUTABLE_STATUSES = frozenset({'applied', 'warning'})


@dataclass(frozen=True)
class Diagnostic:
    severity: Literal['error', 'warning']
    message: str
    path: tuple | None = None
    code: str | None = None

    def __post_init__(self):
        if self.path is not None:
            path = tuple(self.path)
            if any(type(part) not in (str, int) for part in path):
                raise TypeError('Diagnostic paths contain only property names and indices.')
            object.__setattr__(self, 'path', path)

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
    form_snapshots: tuple[dict, ...] = ()
    snapshot_issues: tuple[Diagnostic, ...] | None = None
    form_issues: dict | None = None
    display_state: DisplayState | None = None
    record_labels: FrozenMap | None = None
    prepared_syntax: str | None = None
    prepared_parameters: FrozenMap | None = None
    target_member_ids: tuple[str, ...] = ()
    rejected_candidate: DiagnosticAttachment | None = None
    effect_ids: tuple[str, ...] = ()

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


@dataclass(frozen=True)
class CommandExecution:
    status: ExecutionStatus
    schema: dict[str, Any] | None
    message: str
    names_schema: dict[str, Any] | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    iterations: tuple['CommandExecution', ...] = ()
    field_code: str | None = None
    concrete_command: Any = None
    snapshot_issues: tuple[Diagnostic, ...] | None = None
    target: IterationTarget | None = None
    form_snapshots: tuple[dict, ...] = ()
    form_issues: dict | None = None
    form_iterations: bool = False
    field_iterations: bool = False
    application_context: tuple[str, ...] = ()
    display_state: DisplayState | None = None
    prepared_syntax: str | None = None
    prepared_parameters: FrozenMap | None = None
    target_keys: tuple[ResourceKey, ...] = ()
    target_member_ids: tuple[str, ...] = ()
    rejected_candidate: DiagnosticAttachment | None = None
    effect_ids: tuple[str, ...] = ()

    @property
    def application_label(self):
        return ' / '.join(self.application_context) or self.field_code or ''

    @property
    def issues(self):
        if self.snapshot_issues is not None:
            return self.snapshot_issues
        return tuple(item for item in self.diagnostics if item.path is not None)

    @classmethod
    def from_result(cls, result):
        return cls(result.status, result.schema, result.message, result.names_schema, result.diagnostics,
                   form_snapshots=result.form_snapshots, snapshot_issues=result.snapshot_issues, form_issues=result.form_issues,
                   display_state=result.display_state, prepared_syntax=result.prepared_syntax,
                   prepared_parameters=result.prepared_parameters, target_member_ids=result.target_member_ids, rejected_candidate=result.rejected_candidate,
                   effect_ids=result.effect_ids)


def with_prepared_command(result, command, source_schema=None):
    concrete = getattr(command, 'command', command)
    code = getattr(concrete, 'field_code', None)
    members = ()
    if isinstance(code, str):
        for schema in (source_schema, result.schema):
            members = tuple(field['id'] for field in (schema or {}).get('elements', [])
                            if field.get('code') == code and field.get('id'))
            if members:
                break
    return replace(result, prepared_syntax=concrete.to_syntax() if hasattr(concrete, 'to_syntax') else None,
                   prepared_parameters=command_parameters_snapshot(concrete), target_member_ids=members)


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
        return with_prepared_command(result, command, schema)
    except Exception as exc:
        return ExecutionResult.error(schema, f'{type(exc).__name__}: {exc}', 'unexpected_command_error')


def apply_command_locally(command, schema, related_schemas=None, database_names=None, *, variables=None, reserved_identities=None):
    """Application without a connected resolver still uses the same contract."""
    result = transform_command(command, schema, CommandContext(related_schemas, database_names, variables if variables is not None else {}, reserved_identities))
    if result.has_errors:
        return result
    diagnostics = tuple(Diagnostic('warning', f'Unresolved formula reference {token!r}.', path)
        for path, token, _ in unresolved_formula_references(result.schema, related_schemas))
    return replace(result, diagnostics=result.diagnostics + diagnostics,
                   names_schema=readable_schema(result.schema, related_schemas, database_names))


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
            return with_prepared_command(replace(result,
                names_schema=readable_schema(result.schema, related_schemas, database_names)), prepared)
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
        return replace(resolved, diagnostics=result.diagnostics + resolved.diagnostics,
                       success_message=result.success_message, prepared_syntax=result.prepared_syntax,
                       prepared_parameters=result.prepared_parameters, target_member_ids=result.target_member_ids)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return ExecutionResult(result.schema, result.diagnostics + (
            Diagnostic('error', f'{type(exc).__name__}: {exc}', code='unexpected_resolution_error'),))
