"""UI-independent command evaluation and atomic per-application publication."""

import asyncio
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from uuid import uuid4

from .application_results import (
    AuthoredCommand, DiagnosticAttachment, DisplaySnapshotBuilder, adapt_authored_result,
)
from .command_execution import (
    CommandExecution, Diagnostic, ExecutionResult, EXECUTABLE_STATUSES,
    apply_command, apply_command_locally, with_prepared_command,
)
from .command_iteration import IterativeCommand, PreparedCommand
from .immutable import freeze, command_parameters_snapshot
from .multi_form import FormWorkspace, FormSelectionError
from .resource_state import ResourceKey, ResourceState, DatabaseResourcesPayload, BaselineEvidence, Existence, LoadStatus, Lifecycle, FormPayload
from .planning import PlanBuilder, SchemaEffect, ResourcePatchEffect, FormDeleteEffect, MemberRef, ResourceProjection, Footprint
from .schema_names import readable_schema, load_default_record_labels

ACTIVE_DRAFT = ContextVar('activity_manager_application_draft', default=None)
FORM_READ_CONCURRENCY = 8


async def _read_form_scope(items, read):
    limit = asyncio.Semaphore(FORM_READ_CONCURRENCY)

    async def bounded(item):
        async with limit:
            return await read(item)

    return await asyncio.gather(*(bounded(item) for item in items), return_exceptions=True)


@dataclass
class ApplicationDraft:
    owner: object
    workspace: FormWorkspace
    related_schemas: dict
    database_names: dict
    reference_defaults: dict
    resolution_cache: dict
    snapshot_builder: DisplaySnapshotBuilder
    effects: tuple
    variables: dict
    plan_builder: PlanBuilder


class Applier:
    """Own one evaluation; callbacks receive progress only after atomic boundaries."""

    _CONTEXT_FIELDS = ('workspace', 'related_schemas', 'database_names',
                       'reference_defaults', 'resolution_cache', 'snapshot_builder',
                       'effects', 'variables', 'plan_builder')

    def __init__(self, source_schema=None, commands=(), *, authored_commands=(),
                 related_schemas=None, database_names=None, reference_defaults=None,
                 resolution_cache=None, resolve_schema=None, lookup_variable=None,
                 form_service=None, generation=0, owner=None, on_progress=None,
                 is_current=None, initial_database_id=None):
        self.owner = owner if owner is not None else object()
        self._workspace = FormWorkspace(source_schema, initial_database_id)
        self.source_schema = deepcopy(source_schema)
        self.commands = deepcopy(tuple(commands))
        self.authored_commands = tuple(authored_commands) or tuple(
            AuthoredCommand(uuid4().hex, index, type(command).__name__, command.to_syntax(),
                            command_parameters_snapshot(command))
            for index, command in enumerate(self.commands))
        self._related_schemas = {**deepcopy(related_schemas or {}), **self._workspace.latest}
        self._database_names = deepcopy(database_names or {})
        self._reference_defaults = deepcopy(reference_defaults or {})
        self._resolution_cache = deepcopy(resolution_cache or {})
        self._resolution_cache.pop('deleted_form_ids', None)
        self._snapshot_builder = DisplaySnapshotBuilder(generation)
        self._effects = ()
        self._plan_builder = PlanBuilder(active_form=self._workspace.state.active_form_key)
        self._plan_builder.observe(self._workspace.state.baselines.entries.values())
        self._variables = {}
        self.scope = None
        self.database_scope = ()
        self.assigned = set()
        self.resolve_schema = resolve_schema
        self.lookup_variable = lookup_variable
        self.form_service = form_service
        self._evaluation_version = generation
        self._on_progress = on_progress
        self._current = is_current or (lambda: True)
        self._cancelled = False
        self._position = 0
        self._index = 0
        self.initial_diagnostics = ()
        self._initial_context_loaded = False
        self.initial_display_state = self._capture_context()
        self.executions = [CommandExecution('not_executed', None, 'Waiting for the previous command.')
                           for _ in self.commands]
        if self.executions and resolve_schema is not None and source_schema is not None:
            self.executions[0] = CommandExecution('resolving', None, 'Resolving names...')

    def _capture_context(self, labels=None):
        return self.snapshot_builder.capture(self.workspace.state, self.related_schemas,
            self.database_names, self.workspace.issues, active=self.workspace.state.active_form_key, labels=labels)

    async def _load_initial_context(self):
        if self._initial_context_loaded:
            return
        self._initial_context_loaded = True
        self.initial_diagnostics = await self._load_context_database(self.workspace.state.active_database_id)
        labels = {}
        if self.source_schema is not None and self.form_service is not None:
            labels = await load_default_record_labels(self.form_service, self.source_schema, self.related_schemas, {})
        if self.is_current():
            self.initial_display_state = self._capture_context(labels)
            self._publish_evaluation()

    async def _load_context_database(self, identity):
        if not identity or self.form_service is None:
            return ()
        key = ResourceKey.database(identity)
        try:
            await self._database(identity)
            self.workspace.issues.pop(identity, None)
            return ()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            warning = Diagnostic('warning', f'Database {identity!r}: resources could not be loaded. {exc}',
                                 code='database_context_unavailable')
            self.workspace.state.note_unavailable(key)
            self.workspace.state.mark_affected(key)
            self.workspace.issues[identity] = (warning,)
            return (warning,)

    def _get(self, name):
        draft = ACTIVE_DRAFT.get()
        return getattr(draft, name) if draft is not None and draft.owner is self.owner else getattr(self, '_' + name)

    def _set(self, name, value):
        draft = ACTIVE_DRAFT.get()
        if draft is not None and draft.owner is self.owner:
            setattr(draft, name, value)
        else:
            setattr(self, '_' + name, value)

    def is_current(self):
        return not self._cancelled and self._current()

    def cancel(self):
        self._cancelled = True

    def _publish_evaluation(self):
        if self.is_current() and self._on_progress:
            self._on_progress(self)

    def _new_draft(self, variables, form_key=None):
        draft = ApplicationDraft(
            self.owner, deepcopy(self.workspace), deepcopy(self.related_schemas),
            deepcopy(self.database_names), deepcopy(self.reference_defaults),
            self.resolution_cache, deepcopy(self.snapshot_builder), self.effects, deepcopy(variables),
            deepcopy(self.plan_builder))
        if form_key is not None:
            draft.workspace.state.activate(form_key)
        return draft

    def _sync_context(self):
        state = self.workspace.state
        state.variables = freeze(self.variables)
        state.scoped_variables = freeze({target['identity']: target['variables'] for target in self.scope or ()})
        state.active_scope = tuple(ResourceKey.form(target['identity']) for target in self.scope or ())
        state.reserved_identities = freeze({identity: sorted(value for value in values if isinstance(value, str)) for identity, values in self.workspace.reserved.items()})

    def _clear_form_context(self):
        self.workspace.state.active_form_key = None
        self.scope = None
        self.assigned.clear()
        self._sync_context()

    @staticmethod
    def _replaces_form_context(command):
        from .commands.add_form import AddFormCommand
        from .commands.add_modify_form import AddModifyFormCommand
        from .commands.modify_form import ModifyFormCommand
        from .commands.move_form import MoveFormCommand
        from .commands.with_form import WithFormCommand

        return isinstance(command, (AddFormCommand, AddModifyFormCommand, WithFormCommand)) or (
            isinstance(command, (ModifyFormCommand, MoveFormCommand)) and not command.current_form)

    def _without_active_form(self, execution):
        return self._snapshot_execution(replace(execution, schema=None, names_schema=None,
                                               snapshot_issues=(), display_state=None))

    def _application_id(self):
        authored_id = (self.authored_commands[self._index].authored_id
                       if self.authored_commands else str(self._index))
        return f'{self._evaluation_version}:{authored_id}:{self._position}'

    def _failed_result(self, result, current):
        candidate = result.rejected_candidate
        if candidate is None and result.schema is not None:
            names = result.names_schema
            if names is None:
                try:
                    names = readable_schema(result.schema, self.related_schemas, self.database_names)
                except Exception:
                    names = None
            try:
                candidate = DiagnosticAttachment(result.schema, result.diagnostics, names)
            except (TypeError, ValueError):
                candidate = None
        previous = self.workspace.active_schema if self.workspace.form_snapshots else current
        return replace(result, schema=deepcopy(previous), names_schema=None,
                       record_labels=None, snapshot_issues=self.workspace.issues.get((previous or {}).get('id'), ()),
                       rejected_candidate=candidate, effect_ids=())

    def _commit(self, draft, result, current, variables, on_success):
        if not self.is_current():
            return None
        if result.application_failed:
            return self._save_result(self._failed_result(result, current))
        try:
            publication = {name: deepcopy(getattr(draft, name)) for name in self._CONTEXT_FIELDS
                           if name not in ('variables', 'resolution_cache')}
            # Cached reads return detached copies. Share the stored payloads so
            # each form selection does not copy every prefetched tree.
            publication['resolution_cache'] = draft.resolution_cache.copy()
            committed_variables = deepcopy(draft.variables)
        except Exception as exc:
            failure = ExecutionResult.error(result.schema,
                f'Could not publish application: {type(exc).__name__}: {exc}', 'publication_error')
            return self._save_result(self._failed_result(failure, current))
        for name, value in publication.items():
            setattr(self, '_' + name, value)
        variables.clear()
        variables.update(committed_variables)
        if on_success:
            on_success()
        self._sync_context()
        return result

    def _stage_result(self, result, current):
        if result.application_failed:
            return self._failed_result(result, current)
        freeze(self.variables)
        self.plan_builder.observe(self.workspace.state.baselines.entries.values())
        self.plan_builder.record_application(self._attempt_id)
        return self._save_result(result)

    def _apply_local(self, application, current, variables):
        draft = self._new_draft(variables)
        self._attempt_id = self._application_id()
        self._position += 1
        result = None
        token = ACTIVE_DRAFT.set(draft)
        try:
            result = apply_command_locally(application, deepcopy(current), self.related_schemas,
                self.database_names, variables=draft.variables,
                reserved_identities=self.workspace.reserved.get((current or {}).get('id')))
            result = self._stage_result(result, current)
        except Exception as exc:
            candidate = result.schema if isinstance(result, ExecutionResult) else current
            result = self._failed_result(ExecutionResult.error(
                candidate, f'{type(exc).__name__}: {exc}', 'unexpected_application_error'), current)
        finally:
            ACTIVE_DRAFT.reset(token)
        return self._commit(draft, result, current, variables, None)

    async def _apply_actual(self, application, current, variables, *, form_key=None, scope_member=False,
                            on_success=None, scope_context=None, tree_read=None):
        draft = self._new_draft(variables, form_key)
        self._attempt_id = self._application_id()
        self._position += 1
        result = None
        token = ACTIVE_DRAFT.set(draft)
        try:
            result = await self._apply_candidate(application, deepcopy(current), draft.variables,
                                                 scope_member=scope_member, scope_context=scope_context,
                                                 tree_read=tree_read)
            if not self.is_current():
                return None
            result = self._stage_result(result, current)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            candidate = result.schema if isinstance(result, ExecutionResult) else current
            result = self._failed_result(ExecutionResult.error(
                candidate, f'{type(exc).__name__}: {exc}', 'unexpected_application_error'), current)
        finally:
            ACTIVE_DRAFT.reset(token)
        return self._commit(draft, result, current, variables, on_success)

    def evaluate_local(self):
        from .commands.delete_form import DeleteFormCommand
        from .commands.with_database import WithDatabaseCommand

        current = deepcopy(self.source_schema)
        stopped = False
        for index, command in enumerate(self.commands):
            if not self.is_current():
                break
            self._index, self._position = index, 0
            if stopped:
                message = 'Did not execute: no form schema selected.' if current is None else 'Did not execute.'
                self.executions[index] = CommandExecution('not_executed', None, message)
                continue
            if isinstance(command, WithDatabaseCommand):
                self._exit_database_scope()
                self.executions[index] = self._snapshot_execution(CommandExecution.from_result(command.execute(None)))
                current = None
                stopped = True
                continue
            replacing = self._replaces_form_context(command)
            if replacing:
                self._clear_form_context()
                current = None
            steps = self._application_steps(index, current, self.variables)
            try:
                application = next(steps)
                while True:
                    result = self._apply_local(application, current, self.variables)
                    if result is None:
                        return self.executions
                    current = result.schema
                    application = steps.send(result)
            except StopIteration as finished:
                execution = self._snapshot_execution(finished.value)
            if isinstance(command, DeleteFormCommand):
                self._clear_form_context()
                self.plan_builder.exit_deleted_scope()
                execution = self._without_active_form(execution)
            elif replacing and execution.schema is None:
                self.plan_builder.select_form(None)
            self.executions[index] = execution
            current = execution.schema
            stopped = execution.status == 'error'
        self._publish_evaluation()
        return self.executions

    async def evaluate(self):
        await self._load_initial_context()
        if not self.is_current():
            return
        if self.resolve_schema is None and self.form_service is None:
            return self.evaluate_local()
        await self._resolve_commands(self._evaluation_version, deepcopy(self.source_schema), self.commands)

    workspace = property(lambda self: self._get('workspace'), lambda self, value: self._set('workspace', value))

    related_schemas = property(lambda self: self._get('related_schemas'), lambda self, value: self._set('related_schemas', value))

    database_names = property(lambda self: self._get('database_names'), lambda self, value: self._set('database_names', value))

    reference_defaults = property(lambda self: self._get('reference_defaults'), lambda self, value: self._set('reference_defaults', value))

    resolution_cache = property(lambda self: self._get('resolution_cache'), lambda self, value: self._set('resolution_cache', value))

    snapshot_builder = property(lambda self: self._get('snapshot_builder'), lambda self, value: self._set('snapshot_builder', value))

    effects = property(lambda self: self._get('effects'), lambda self, value: self._set('effects', value))

    variables = property(lambda self: self._get('variables'), lambda self, value: self._set('variables', value))

    plan_builder = property(lambda self: self._get('plan_builder'), lambda self, value: self._set('plan_builder', value))

    @property
    def candidate_plan(self):
        return self.plan_builder.snapshot()

    @property
    def results(self):
        plan = self.candidate_plan
        return tuple(adapt_authored_result(authored, execution, self._evaluation_version, plan=plan)
                     for authored, execution in zip(self.authored_commands, self.executions))


    def enrich_current_display(self, snapshot_id, generation, labels):
        """Publish a new presentation revision only for the still-current result."""
        if generation != self._evaluation_version or not self.executions:
            return None
        current = self.executions[-1]
        display = current.display_state
        if current.status not in EXECUTABLE_STATUSES or display is None or display.snapshot_id != snapshot_id:
            return None
        enriched = display.enrich(labels)
        metadata = enriched.tab_metadata()
        identity = (current.schema or {}).get('id')
        children = tuple(replace(child, display_state=enriched,
                                 names_schema=metadata.get((child.schema or {}).get('id'), (None, ()))[0])
                         if child.display_state is display else child for child in current.iterations)
        self.executions[-1] = replace(current, display_state=enriched, iterations=children,
                                     names_schema=metadata.get(identity, (None, ()))[0])
        return enriched


    def _context_warning(self, command, current, variables):
        from .commands.add_form import AddFormCommand
        from .commands.add_modify_form import AddModifyFormCommand
        from .commands.with_form import WithFormCommand
        from .commands.modify_form import ModifyFormCommand
        from .commands.move_form import MoveFormCommand
        from .commands.delete_form import DeleteFormCommand
        from .commands.set_variable import SetVariableCommand
        command = getattr(command, 'command', command)
        if isinstance(command, (AddFormCommand, AddModifyFormCommand)):
            if not self.workspace.state.active_database_id:
                return ExecutionResult.warning(current, 'An active database is required. Command skipped.', 'missing_database', skipped=True)
            return None
        if isinstance(command, WithFormCommand) or (
                isinstance(command, (ModifyFormCommand, MoveFormCommand, DeleteFormCommand)) and not command.current_form):
            return None
        if isinstance(command, SetVariableCommand):
            from .command_templates import command_parameters
            try:
                command = command_parameters(command, variables or {})
            except ValueError:
                return None  # Normal parameter validation reports this failure.
            if command.method == 'literal' or current is not None:
                return None
            if self.workspace.state.active_database_id or '/' in (command.referenced_form or ''):
                return None
            return ExecutionResult.warning(current, 'An active database or a database-qualified referenced form is required.',
                                           'missing_database', skipped=True)
        if current is None:
            return ExecutionResult.warning(current, 'An active form is required. Command skipped.',
                                           'missing_form', skipped=True)
        return None

    def _application_steps(self, index, current, variables=None, targets=None, *, record_progress=True):
        """One orchestration path shared by synchronous and connected evaluation.

        Yield concrete applications; callers send back their normal results.
        """
        command = self.commands[index]
        missing = self._context_warning(command, current, variables)
        if missing is not None:
            return self._snapshot_execution(CommandExecution.from_result(missing))
        target_key = self.workspace.key_for(current) if current is not None else None
        if not isinstance(command, IterativeCommand):
            result = yield command
            return CommandExecution.from_result(result)
        try:
            if targets is None:
                targets = command.targets(current, variables)
        except ValueError as exc:
            return CommandExecution.from_result(self._save_result(self._failed_result(
                ExecutionResult.error(current, str(exc), 'invalid_selector'), current)))
        if not targets:
            warning = ExecutionResult.warning(
                current, f'No field codes matched pattern {command.field_pattern!r}. Schema unchanged.', 'no_matches')
            execution = CommandExecution.from_result(warning)
            return replace(execution, field_iterations=True,
                           message=self._iteration_line(self._console_form_path(current), execution.message))
        children = [CommandExecution('not_executed', None, 'Waiting for the previous iteration.',
                                     field_code=target.original_code, target=target,
                                     target_keys=(target_key,)) for target in targets]
        previous = None
        for iteration, target in enumerate(targets):
            children[iteration] = CommandExecution('resolving', None, 'Applying iteration...',
                                                    field_code=target.original_code, target=target)
            if record_progress:
                self.executions[index] = self._summarize_iterations(children, current)
            try:
                concrete = target.prepare(command, current, previous, variables)
            except ValueError as exc:
                concrete = None
                result = self._save_result(self._failed_result(
                    ExecutionResult.error(current, str(exc), 'invalid_iteration'), current))
            else:
                result = yield concrete
            execution = self._snapshot_execution(CommandExecution.from_result(result))
            children[iteration] = CommandExecution(
                execution.status, execution.schema, execution.message, execution.names_schema,
                execution.diagnostics, field_code=target.original_code,
                concrete_command=concrete.command if concrete else None, target=target, form_snapshots=execution.form_snapshots,
                snapshot_issues=execution.issues, form_issues=execution.form_issues,
                display_state=execution.display_state, prepared_syntax=execution.prepared_syntax,
                prepared_parameters=execution.prepared_parameters, target_member_ids=execution.target_member_ids,
                target_keys=(target_key,), rejected_candidate=execution.rejected_candidate,
                effect_ids=execution.effect_ids)
            current = execution.schema
            if execution.status == 'error':
                for later in range(iteration + 1, len(children)):
                    children[later] = CommandExecution('not_executed', None,
                        'Did not execute: an earlier iteration failed.', field_code=targets[later].original_code,
                        target=targets[later], target_keys=(target_key,))
                break
            previous = concrete.command.field_code if concrete else previous
        return replace(self._summarize_iterations(children, current), field_iterations=True)


    def _console_form_path(self, schema):
        identity = (schema or {}).get('databaseId') or self.workspace.state.active_database_id
        path = (f'Database ({self.database_names.get(identity, identity)})',) if identity else ()
        if schema:
            path += (f'Form ({schema.get("label") or schema["id"]})',)
        return path

    @staticmethod
    def _iteration_line(path, message, iteration=None):
        prefix = ' / '.join(path)
        parts = (prefix, f'Iteration {iteration}' if iteration is not None else '')
        return '\n'.join(' — '.join((*filter(None, parts), line)) for line in message.splitlines())

    def _summarize_iterations(self, children, current, *, nested=False):
        statuses = [child.status for child in children]
        status = next((value for value in ('error', 'resolving', 'warning', 'not_executed')
                       if value in statuses), 'applied')
        leaves, lines, diagnostics = [], [], []

        def visit(items, context=(), targets=(), console_path=()):
            for index, child in enumerate(items, 1):
                path = context + (child.application_context or ((child.field_code,) if child.field_code else ()))
                keys = child.target_keys or targets
                if not keys and child.schema is not None:
                    keys = (ResourceKey.form(child.schema['id']),)
                segment = child.console_path or (f'Field {index} ({child.application_label})',)
                full_path = console_path + segment
                if child.iterations:
                    visit(child.iterations, path, keys, full_path)
                    continue
                is_application = not (child.field_iterations or child.form_iterations)
                if not is_application and not child.diagnostics:
                    continue
                iteration = len(leaves) + 1 if is_application else None
                message = child.message if is_application else '\n'.join(
                    item.console_line() for item in child.diagnostics)
                line = self._iteration_line(full_path, message, iteration)
                lines.append(line)
                if is_application:
                    leaf = replace(child, application_context=path, target_keys=keys) if nested else child
                    leaves.append(replace(leaf, console_message=line))
                diagnostics.extend(Diagnostic(item.severity,
                    self._iteration_line(full_path, item.message, iteration), code=item.code)
                    for item in child.diagnostics)

        visit(children, console_path=() if nested else self._console_form_path(current))
        message = '\n'.join(lines)
        reached = [child for child in children if child.schema is not None]
        final = reached[-1] if reached else None
        return CommandExecution(status, current, message, final.names_schema if final else None,
            tuple(diagnostics), tuple(leaves), snapshot_issues=final.issues if final else (), form_snapshots=final.form_snapshots if final else (),
            form_issues=final.form_issues if final else None,
            display_state=final.display_state if final else None,
            rejected_candidate=final.rejected_candidate if final else None,
            effect_ids=tuple(effect for child in children for effect in child.effect_ids))


    def _save_result(self, result):
        effects = tuple(effect.effect_id for effect in self.effects if effect.application_id == self._attempt_id)
        if result.schema is not None and not result.application_failed:
            identity = result.schema.get('id')
            key = self.workspace.key_for(result.schema)
            before = self.workspace.state.resources.get(key)
            deletion = any(isinstance(effect, FormDeleteEffect) and effect.application_id == self._attempt_id
                           for effect in self.effects)
            if not deletion:
                self.workspace.update(result.schema)
            after = self.workspace.state.resources[key]
            if before is not None and (before.payload, before.lifecycle) != (after.payload, after.lifecycle):
                effect = SchemaEffect(self._attempt_id + ':schema', self._attempt_id, key, before, after,
                                      projections=result.resource_projections)
                self.plan_builder.add(effect)
                self.effects += (effect,)
                effects += (effect.effect_id,)
            self.workspace.issues[identity] = result.issues
            self.related_schemas.update(self.workspace.latest)
        display = self.snapshot_builder.capture(self.workspace.state, self.related_schemas,
            self.database_names, self.workspace.issues,
            active=next((effect.key for effect in reversed(self.effects) if effect.application_id == self._attempt_id),
                        self.workspace.key_for(result.schema) if result.schema else None),
            labels=result.record_labels)
        return replace(result, form_snapshots=tuple(deepcopy(self.workspace.form_snapshots)),
                       form_issues=deepcopy(self.workspace.issues), display_state=display, effect_ids=effects)


    def _snapshot_execution(self, execution):
        issues = self.workspace.issues.get((execution.schema or {}).get('id'), execution.issues)
        display = execution.display_state or self.snapshot_builder.capture(
            self.workspace.state, self.related_schemas, self.database_names, self.workspace.issues,
            active=self.workspace.key_for(execution.schema) if execution.schema else None)
        return replace(execution, form_snapshots=tuple(deepcopy(self.workspace.form_snapshots)),
                       snapshot_issues=issues, form_issues=deepcopy(self.workspace.issues), display_state=display)


    async def _apply_candidate(self, application, current, variables, *, scope_member=False, scope_context=None,
                               tree_read=None):
        from .commands.add_form import AddFormCommand
        from .commands.add_modify_form import AddModifyFormCommand
        from .commands.with_form import WithFormCommand
        from .commands.modify_form import ModifyFormCommand
        from .commands.move_form import MoveFormCommand
        from .commands.delete_form import DeleteFormCommand
        from .commands.set_variable import SetVariableCommand
        from .command_templates import command_parameters
        from .command_iteration import PreparedCommand
        try:
            prepared = application.command if isinstance(application, PreparedCommand) else command_parameters(application, variables)
            if not isinstance(prepared, DeleteFormCommand):
                self.plan_builder.finish_deletions()
            if isinstance(prepared, DeleteFormCommand):
                return await self._delete_form(prepared, current, scope_member=scope_member)
            if isinstance(prepared, AddFormCommand):
                return await self._add_form(prepared, current, scope_context=scope_context)
            if isinstance(prepared, AddModifyFormCommand):
                return await self._add_modify_form(prepared, current, scope_context=scope_context)
            if isinstance(prepared, MoveFormCommand) and prepared.current_form:
                if self.form_service is None:
                    return prepared.execute(current)
                warnings = await self._load_context_database(current['databaseId'])
                if warnings:
                    return ExecutionResult(current, warnings, skipped=True)
                return await self._move_form(prepared, current)
            if isinstance(prepared, (WithFormCommand, ModifyFormCommand, MoveFormCommand)):
                if self.form_service is None:
                    return prepared.execute(current)
                try:
                    if scope_member or (isinstance(prepared, ModifyFormCommand) and prepared.current_form):
                        identity = prepared.form if scope_member else self.workspace.key_for(current).identity
                        schema, tree_warnings = await self.workspace.select_with_tree(
                            identity, self.form_service, self.related_schemas, tree_read=tree_read)
                    else:
                        schema, tree_warnings = await self._switch_form(prepared.form, self.related_schemas)
                except FormSelectionError as exc:
                    return ExecutionResult.warning(current, str(exc), 'form_switch_failed', skipped=True)
                if scope_context is not None:
                    self._plan_form_selection(self.workspace.key_for(schema), scope_context)
                elif not (isinstance(prepared, ModifyFormCommand) and prepared.current_form):
                    self.plan_builder.select_form(self.workspace.key_for(schema))
                context_warnings = await self._load_context_database(schema['databaseId'])
                if isinstance(prepared, MoveFormCommand):
                    if context_warnings:
                        return with_prepared_command(ExecutionResult(schema, context_warnings + tree_warnings), prepared)
                    result = await self._move_form(replace(prepared, form=schema['id']), schema)
                    # A destination warning leaves the successfully selected form active.
                    return replace(result, diagnostics=tree_warnings + result.diagnostics, skipped=False)
                if isinstance(prepared, ModifyFormCommand):
                    result = apply_command_locally(PreparedCommand(replace(prepared, form=None if prepared.current_form else schema['id'], form_pattern=None)), schema,
                        self.related_schemas, self.database_names, variables=variables)
                    if not result.has_errors and prepared.new_name is not None:
                        projection = await self._project_resource_label(schema, result.schema)
                        result = replace(result, resource_projections=(projection,) if projection is not None else ())
                    return replace(result, diagnostics=context_warnings + tree_warnings + result.diagnostics)
                return with_prepared_command(ExecutionResult(schema, context_warnings + tree_warnings, names_schema=readable_schema(
                        schema, self.related_schemas, self.database_names),
                    snapshot_issues=self.workspace.issues.get(schema.get('id'), ()),
                    success_message=f'Active form: {schema.get("label") or schema["id"]}'), prepared)
        except Exception as exc:
            return ExecutionResult.error(current, str(exc), 'form_switch_failed')
        async def resolve(schema):
            self.related_schemas.update(self.workspace.latest)
            self.related_schemas[schema.get('id')] = schema
            return await self.resolve_schema(schema) if self.resolve_schema is not None else ExecutionResult(schema)
        async def lookup(command, schema):
            return await self.lookup_variable(command, schema if schema is not None else
                                              {'databaseId': self.workspace.state.active_database_id})
        application = application if isinstance(application, PreparedCommand) else PreparedCommand(prepared)
        result = await apply_command(application, current, resolve,
            related_schemas=self.related_schemas, database_names=self.database_names,
            variables=variables, lookup_variable=lookup if self.lookup_variable is not None else None,
            reserved_identities=self.workspace.reserved.get((current or {}).get("id")))
        if isinstance(prepared, SetVariableCommand):
            # Variables do not run the schema resolver or change the schema.
            result = replace(result, snapshot_issues=self.workspace.issues.get((current or {}).get('id'), ()))
        return result


    async def _switch_form(self, selector, related=None):
        from .schema_resolution import CachedResolutionService
        service = CachedResolutionService(self.form_service, self.resolution_cache)
        return await self.workspace.switch(selector, service, related)

    def _plan_form_selection(self, key, scope_context):
        if scope_context is None:
            self.plan_builder.select_form(key)
            return
        scope_id, keys = scope_context
        if keys is None:
            self.plan_builder.add_scope_form(scope_id, key)
        else:
            self.plan_builder.begin_scope(scope_id, keys)
        self.plan_builder.select_form(key, scope_id=scope_id)

    @property
    def active_database_ids(self):
        if self.database_scope:
            return tuple(branch['id'] for branch in self.database_scope)
        identity = self.workspace.state.active_database_id
        return (identity,) if identity else ()

    @property
    def active_form_schemas(self):
        state = self.workspace.state
        if self.scope is not None:
            keys = (target['form_key'] for target in self.scope)
        elif self.database_scope:
            keys = (branch['form_key'] for branch in self.database_scope)
        else:
            keys = (state.active_form_key,)
        return tuple(resource.payload.to_dict() for key in dict.fromkeys(keys)
                     if (resource := state.resources.get(key)) is not None
                     and resource.lifecycle != Lifecycle.DELETED)

    def _select_database(self, identity):
        state = self.workspace.state
        state.active_form_key = None
        state.active_database_id = identity

    def _exit_database_scope(self):
        self.database_scope = ()
        self.variables = {key: value for key, value in self.variables.items()
                          if not key.startswith('database.') and key not in self.assigned}
        self._clear_form_context()
        self._select_database(None)
        self.plan_builder.finish_deletions()
        self.plan_builder.select_form(None)

    async def _database_directory(self):
        from .schema_resolution import CachedResolutionService
        if self.form_service is None:
            raise ValueError('WithDatabase requires a connected database directory.')
        if self.database_names:
            return self.database_names
        service = CachedResolutionService(self.form_service, self.resolution_cache)
        names = await service.get_database_names()
        if self.is_current():
            self.database_names.update(names)
        return names

    def _selector_result(self, message, code='database_selector_failed'):
        return self._snapshot_execution(CommandExecution.from_result(ExecutionResult.error(None, message, code)))

    async def _switch_database(self, command):
        from .command_iteration import compile_pattern
        from .command_templates import substitute_text
        from .commands.with_database import WithDatabaseCommand
        from .name_lookup import require_named_id
        from .schema_resolution import CachedResolutionService

        self._exit_database_scope()
        try:
            names = await self._database_directory()
            if command.database_pattern is None:
                value = substitute_text(command.database, self.variables, namespaces={'vars'})
                identity = require_named_id(value, names, kind='Database')
                matches = [(identity, names[identity], {})]
            else:
                pattern = compile_pattern(substitute_text(command.database_pattern, self.variables,
                                                          namespaces={'vars'}), kind='database')
                matches = []
                for identity, name in sorted(names.items(), key=lambda item: (item[1].casefold(), item[0])):
                    if (match := pattern.fullmatch(name)) is not None:
                        variables = self._match_variables('database', name, len(matches) + 1, match)
                        matches.append((identity, name, variables))
                if not matches:
                    raise ValueError('No database names matched the pattern.')
        except Exception as exc:
            return self._selector_result(str(exc))
        if not self.is_current():
            return None

        children, branches = [], []
        service = CachedResolutionService(self.form_service, self.resolution_cache)
        for position, (identity, name, captures) in enumerate(matches, 1):
            self._select_database(identity)
            warnings = await self._load_context_database(identity)
            try:
                await service.get_form_names(identity)
            except (AttributeError, ValueError):
                pass
            if not self.is_current():
                return None
            result = with_prepared_command(
                ExecutionResult(None, warnings, success_message=f'Active database: {name}'),
                WithDatabaseCommand(database=identity))
            key = ResourceKey.database(identity)
            display = self.snapshot_builder.capture(self.workspace.state, self.related_schemas,
                self.database_names, self.workspace.issues, active=key)
            children.append(self._snapshot_execution(replace(CommandExecution.from_result(result),
                application_context=(name,), target_keys=(key,), display_state=display,
                console_path=(f'Database {position} ({name})',))))
            branches.append(dict(id=identity, name=name, form_key=None, console_path=(f'Database {position} ({name})',),
                                 variables={**self.variables, **captures}))
        if len(branches) == 1:
            self.variables.update(matches[0][2])
            self._sync_context()
            return children[0]
        self.database_scope = tuple(branches)
        return self._form_summary(children, None)

    @staticmethod
    def _match_variables(namespace, name, index, match):
        return {f'{namespace}.name': name, f'{namespace}.index': index,
                **{f'{namespace}.{i}': value for i, value in enumerate(match.groups(), 1)},
                **{f'{namespace}.{key}': value for key, value in match.groupdict().items()}}

    @staticmethod
    def _form_selector(command):
        if isinstance(getattr(command, 'form_name', None), str):
            return 'form_name'
        if isinstance(getattr(command, 'form', None), str):
            return 'form'
        return None

    async def _qualified_form(self, command, parameter, selectors, variable_maps, deleting):
        from .command_templates import command_parameters
        from .commands.add_form import AddFormCommand
        from .name_lookup import find_named_id, require_named_id
        from .schema_resolution import CachedResolutionService

        by_database = {branch['id']: branch['variables'] for branch in self.database_scope}
        self._exit_database_scope()
        try:
            names = await self._database_directory()
            destinations = []
            for selector in selectors:
                if '/' not in selector:
                    raise ValueError('The form selector mixes qualified and unqualified destinations.')
                database, form = selector.split('/', 1)
                destinations.append((require_named_id(database, names, kind='Database'), form))
            identity, form = destinations[0]
            if any(db != identity for db, _ in destinations):
                raise ValueError('The qualified form selector resolves to multiple destinations.')
            form_selectors = {name for _, name in destinations}
            if len(form_selectors) > 1:
                directory = {}
                if not isinstance(command, AddFormCommand):
                    service = CachedResolutionService(self.form_service, self.resolution_cache)
                    directory = await self.workspace.form_directory(service, identity)
                keys = set()
                for name in form_selectors:
                    form_id = find_named_id(name, directory, kind='Form')
                    keys.add(('id', form_id) if form_id is not None else ('name', name.casefold()))
                if len(keys) > 1:
                    raise ValueError('The qualified form selector resolves to multiple destinations.')
            variables = by_database.get(identity, variable_maps[0]).copy()
            prepared = replace(command_parameters(command, variables), **{parameter: form})
        except Exception as exc:
            return self._selector_result(str(exc), 'qualified_form_selector_failed')
        if not self.is_current():
            return None
        self._select_database(identity)
        result = await self._apply_actual(PreparedCommand(prepared), None, variables)
        if result is None or not self.is_current():
            return None
        if result.application_failed:
            self._select_database(None)
        if deleting:
            self._clear_form_context()
            self.plan_builder.exit_deleted_scope()
        return self._snapshot_execution(replace(CommandExecution.from_result(result),
                                                display_state=None if result.application_failed else result.display_state))

    async def _run_database_scope(self, index, version, *, selecting=False, deleting=False):
        if isinstance(self.commands[index], IterativeCommand) and all(branch['form_key'] for branch in self.database_scope):
            targets = []
            for branch in self.database_scope:
                key = branch['form_key']
                schema = self.workspace.state.resources[key].payload.schema
                name = schema.get('label') or key.identity
                targets.append(dict(identity=key.identity, name=name, form_key=key,
                                    variables=branch['variables'], path=(branch['name'], name),
                                    console_path=branch['console_path'] + (f'Form 1 ({name})',)))
            return await self._run_form_scope(index, targets, None, version)

        children = [CommandExecution('not_executed', None, 'Did not execute: an earlier database failed.',
                                     application_context=(branch['name'],), console_path=branch['console_path'])
                    for branch in self.database_scope]
        current = None
        for position, branch in enumerate(self.database_scope):
            key = None if selecting else branch['form_key']
            self._select_database(branch['id'])
            if key is not None:
                self.workspace.state.activate(key)
            current = self.workspace.active_schema
            if selecting:
                scope_context = None if deleting else (self.authored_commands[index].authored_id, None)
                result = await self._apply_actual(self.commands[index], None, branch['variables'],
                                                  scope_context=scope_context)
                execution = CommandExecution.from_result(result) if result is not None else None
                branch['form_key'] = self.workspace.state.active_form_key
            else:
                execution = await self._run_connected_command(index, current, branch['variables'], version,
                                                               publish=False, form_key=key)
            if execution is None or not self.is_current():
                return None
            current = execution.schema
            label = (current or {}).get('label')
            target_keys = execution.target_keys
            if deleting and execution.effect_ids:
                effect = next((effect for effect in reversed(self.effects)
                               if effect.effect_id in execution.effect_ids and isinstance(effect, FormDeleteEffect)), None)
                if effect is not None:
                    target_keys = (effect.subjects[0],)
                    schema = self.workspace.state.resources[target_keys[0]].payload.schema
                    label = schema.get('label') or target_keys[0].identity
            path = (branch['name'], label) if label else (branch['name'],)
            command = self.commands[index]
            if label and not isinstance(command, IterativeCommand) and isinstance(getattr(command, 'field_code', None), str):
                from .command_templates import substitute_selector
                try:
                    path += (substitute_selector(command.field_code, branch['variables']),)
                except ValueError:
                    path += (command.field_code,)
            console_path = branch['console_path']
            if label:
                console_path += (f'Form 1 ({label})',)
            if len(path) == 3:
                console_path += (f'Field 1 ({path[-1]})',)
            children[position] = replace(execution, application_context=path, target_keys=target_keys,
                                         console_path=console_path)
            if deleting:
                branch['form_key'] = None
            self.executions[index] = self._form_summary(children, current)
            self._publish_evaluation()
            if execution.status == 'error':
                break
        self._sync_context()
        summary = self._form_summary(children, current)
        return self._finish_form_deletion(summary) if deleting else summary

    def _finish_form_deletion(self, execution):
        for branch in self.database_scope:
            branch['form_key'] = None
        self._clear_form_context()
        self.plan_builder.exit_deleted_scope()
        return self._without_active_form(execution)

    async def _run_form_selection(self, index, version, current, *, replacing, deleting):
        from .command_templates import substitute_text

        command = self.commands[index]
        variables = {name: value for name, value in self.variables.items() if name not in self.assigned}
        branches = self.database_scope
        variable_maps = [branch['variables'] for branch in branches] if branches else [variables]
        parameter = self._form_selector(command)
        if parameter:
            authored_selector = getattr(command, parameter)
            selectors = []
            try:
                for bindings in variable_maps:
                    selectors.append(substitute_text(authored_selector, bindings))
            except ValueError as exc:
                if '/' in authored_selector or any('/' in selector for selector in selectors):
                    self._exit_database_scope()
                else:
                    for branch in branches:
                        branch['form_key'] = None
                    self._clear_form_context()
                return self._selector_result(str(exc), 'invalid_selector')
            if any('/' in selector for selector in selectors):
                return await self._qualified_form(command, parameter, selectors, variable_maps, deleting)

        if replacing or branches:
            self._clear_form_context()
            current = None
        if getattr(command, 'form_pattern', None) is not None:
            scope, execution = await self._start_form_scope(command, current, variables, branches)
        elif branches:
            for branch in branches:
                branch['form_key'] = None
            return await self._run_database_scope(index, version, selecting=True, deleting=deleting)
        else:
            scope = None
            execution = await self._run_connected_command(index, current, variables, version)
        if execution is None or not self.is_current():
            return None
        if deleting:
            return self._finish_form_deletion(execution)
        if scope is None and (execution.form_iterations or execution.schema is None):
            self._clear_form_context()
            self.plan_builder.select_form(None)
            return self._without_active_form(execution)
        self.scope = scope
        self.variables.clear()
        self.variables.update(variables)
        self._sync_context()
        return execution

    async def _resolve_commands(self, version, current, commands):
        from .commands.delete_form import DeleteFormCommand
        from .commands.with_database import WithDatabaseCommand

        for index, command in enumerate(commands):
            if not self.is_current():
                return
            self._index, self._position = index, 0
            self.executions[index] = CommandExecution('resolving', None, 'Resolving names...')
            self._publish_evaluation()
            replacing = self._replaces_form_context(command)
            deleting = isinstance(command, DeleteFormCommand)
            if isinstance(command, WithDatabaseCommand):
                execution = await self._switch_database(command)
            elif replacing or (deleting and not command.current_form):
                execution = await self._run_form_selection(index, version, current,
                                                           replacing=replacing, deleting=deleting)
            elif self.scope is not None:
                execution = await self._run_form_scope(index, self.scope, current, version)
                if deleting and execution is not None:
                    execution = self._finish_form_deletion(execution)
            elif self.database_scope:
                execution = await self._run_database_scope(index, version, deleting=deleting)
            else:
                execution = await self._run_connected_command(index, current, self.variables, version)
                if deleting and execution is not None:
                    execution = self._finish_form_deletion(execution)
            if execution is None or not self.is_current():
                return
            current = execution.schema
            self.executions[index] = execution
            if execution.status == 'error':
                for later in range(index + 1, len(commands)):
                    self.executions[later] = CommandExecution('not_executed', None,
                        'Did not execute: the previous command failed.')
                self._publish_evaluation()
                return
            self._publish_evaluation()

    async def _run_connected_command(self, index, current, variables, version, *, publish=True,
                                     targets=None, form_key=None, on_success=None):
        steps = self._application_steps(index, current, variables, targets, record_progress=publish)
        try:
            application = next(steps)
            while True:
                if publish:
                    self._publish_evaluation()
                result = await self._apply_actual(application, current, variables, form_key=form_key, on_success=on_success)
                if result is None or not self.is_current():
                    return None
                current = result.schema
                application = steps.send(result)
        except StopIteration as finished:
            return self._snapshot_execution(finished.value)

    async def _start_form_scope(self, command, current, variables, branches=()):
        from .command_iteration import compile_pattern
        from .command_templates import substitute_text
        from .commands.with_form import WithFormCommand
        from .schema_resolution import CachedResolutionService

        scope, empty_branches = [], []
        databases = branches or ({'id': self.workspace.state.active_database_id,
                                   'name': None, 'variables': variables},)
        try:
            if not databases[0]['id']:
                failure = ExecutionResult.warning(current, 'An active database is required for a form pattern.',
                                                  'missing_database', skipped=True)
                return None, self._snapshot_execution(CommandExecution.from_result(failure))
            if self.form_service is None:
                raise ValueError('WithForm requires a connected form loader.')
            service = CachedResolutionService(self.form_service, self.resolution_cache)
            async def read_directory(branch):
                pattern = compile_pattern(substitute_text(command.form_pattern, branch['variables'],
                                                          namespaces={'vars', 'database'}), kind='form')
                names = await self.workspace.form_directory(service, branch['id'])
                return pattern, names

            directories = await _read_form_scope(databases, read_directory)
            if not self.is_current():
                return None, None
            for branch, directory in zip(databases, directories, strict=True):
                if isinstance(directory, BaseException):
                    raise directory
                pattern, names = directory
                bindings = branch['variables']
                count = 0
                for identity, name in sorted(names.items(), key=lambda item: (item[1].casefold(), item[0])):
                    if (match := pattern.fullmatch(name)) is None:
                        continue
                    count += 1
                    local = {**bindings, **self._match_variables('form', name, count, match)}
                    path = (branch['name'], name) if branches else (name,)
                    console_path = branch.get('console_path', self._console_form_path(None)) + (f'Form {count} ({name})',)
                    scope.append(dict(identity=identity, name=name, variables=local, form_key=None,
                                      path=path, console_path=console_path))
                if branches and not count:
                    warning = ExecutionResult.warning(None, 'No form names matched the pattern.', 'no_matches')
                    empty_branches.append((len(scope), replace(CommandExecution.from_result(warning),
                        console_path=branch['console_path'], form_iterations=True)))
        except Exception as exc:
            return None, self._selector_result(str(exc), 'form_selector_failed')

        if branches:
            for branch in branches:
                branch['form_key'] = None
        scope_id = self.authored_commands[self._index].authored_id
        scope_keys = tuple(ResourceKey.form(target['identity']) for target in scope)
        children = self._pending_forms(scope)
        tree_reads = {}
        if isinstance(command, WithFormCommand) and len(scope) > 1:
            async def read_tree(identity):
                return await self.workspace.read_form_tree(identity, self.form_service)

            identities = tuple(dict.fromkeys(target['identity'] for target in scope))
            reads = await _read_form_scope(identities, read_tree)
            if not self.is_current():
                return None, None
            for identity, read in zip(identities, reads, strict=True):
                if isinstance(read, BaseException):
                    raise read
                tree_reads[identity] = read
        for i, target in enumerate(scope):
            # Prefetched and cached reads can complete without suspending.
            # Let the UI service its connection between form applications.
            await asyncio.sleep(0)
            if not self.is_current():
                return None, None
            from .command_templates import command_parameters
            try:
                concrete = command_parameters(replace(command, form=target['identity'], form_pattern=None),
                                              target['variables'])
            except ValueError as exc:
                result = ExecutionResult.error(current, str(exc), 'invalid_parameter')
            else:
                result = await self._apply_actual(PreparedCommand(concrete), current, target['variables'],
                                                 scope_member=True, scope_context=(scope_id, scope_keys),
                                                 tree_read=tree_reads.pop(target['identity'], None))
            if result is None or not self.is_current():
                return None, None
            if not result.application_failed:
                target['form_key'] = ResourceKey.form(target['identity'])
            children[i] = replace(CommandExecution.from_result(result), field_code=target['name'],
                                     application_context=target['path'], console_path=target['console_path'],
                                     target_keys=(ResourceKey.form(target['identity']),))
            current = result.schema
            if result.has_errors or (result.application_failed and not branches):
                break
        for position, warning in reversed(empty_branches):
            children.insert(position, warning)
        summary = self._form_summary(children, current)
        if summary.status == 'error' or (not branches and scope and any(not target['form_key'] for target in scope)):
            return None, summary
        selected = [target for target in scope if target['form_key'] is not None]
        return (selected if branches else selected or None), summary

    @staticmethod
    def _pending_forms(scope):
        return [CommandExecution('not_executed', None, 'Did not execute: an earlier form failed.',
                                 field_code=target['name'], application_context=target['path'], console_path=target['console_path'],
                                 target_keys=(ResourceKey.form(target['identity']),)) for target in scope]


    def _form_summary(self, children, current):
        if not children:
            warning = ExecutionResult.warning(current, 'No form names matched. No applications in this scope.', 'no_matches')
            execution = self._snapshot_execution(CommandExecution.from_result(warning))
            return replace(execution, form_iterations=True,
                           message=self._iteration_line(self._console_form_path(None), execution.message))
        summary = self._summarize_iterations(children, current, nested=True)
        return replace(self._snapshot_execution(summary), form_iterations=True)

    async def _run_form_scope(self, index, scope, current, version):
        from .commands.set_variable import SetVariableCommand
        children = self._pending_forms(scope)
        plans = [None] * len(scope)
        command = self.commands[index]
        for i, target in enumerate(scope):
            context = target['path']
            if not isinstance(command, IterativeCommand) and isinstance(getattr(command, 'field_code', None), str):
                from .command_templates import substitute_selector
                try:
                    context += (substitute_selector(command.field_code, target['variables']),)
                except ValueError:
                    context += (command.field_code,)
            console_path = target['console_path']
            if len(context) > len(target['path']):
                console_path += (f'Field 1 ({context[-1]})',)
            children[i] = replace(children[i], application_context=context, console_path=console_path)
        if isinstance(command, IterativeCommand):
            # Plan every form before applying any field, so an early failure still
            # leaves the complete set of unexecuted applications visible.
            for i, target in enumerate(scope):
                try:
                    plans[i] = command.targets(self.workspace.state.resources[target['form_key']].payload.to_dict(), target['variables'])
                except ValueError:
                    continue  # Report selector errors when their form is reached.
                children[i] = replace(children[i], field_iterations=True, iterations=tuple(
                    CommandExecution('not_executed', None, 'Did not execute: an earlier application failed.',
                                     field_code=field.original_code, target=field) for field in plans[i]))
        self.executions[index] = self._form_summary(children, current)
        self._publish_evaluation()
        for i, target in enumerate(scope):
            await asyncio.sleep(0)
            if not self.is_current():
                return None
            state = self.workspace.state.resources.get(target['form_key'])
            if state is None or state.lifecycle == Lifecycle.DELETED:
                warning = self._snapshot_execution(CommandExecution.from_result(ExecutionResult.warning(
                    self.workspace.active_schema, 'Form deleted. Command skipped.', 'deleted_form', skipped=True)))
                children[i] = replace(warning, application_context=children[i].application_context,
                                      console_path=children[i].console_path, target_keys=children[i].target_keys)
                continue
            current = deepcopy(state.payload.to_dict())
            execution = await self._run_connected_command(index, current, target['variables'], version,
                                                          publish=False, targets=plans[i], form_key=target['form_key'],
                on_success=(lambda: self.assigned.add(f'vars.{command.name}'))
                if isinstance(command, SetVariableCommand) else None)
            if not self.is_current():
                return None
            children[i] = replace(execution, field_code=target['name'],
                                  application_context=children[i].application_context,
                                  console_path=children[i].console_path,
                                  target_keys=children[i].target_keys)
            current = execution.schema
            if execution.status == 'error':
                break
        return self._form_summary(children, current)

    async def _database(self, identity):
        key = ResourceKey.database(identity)
        existing = self.workspace.state.resources.get(key)
        if existing is None or existing.load_status != LoadStatus.LOADED:
            try:
                state = await self.form_service.get_database_state(identity)
            except AttributeError:
                entries = await self.form_service.get_database_resources(identity)
                state = ResourceState(key, DatabaseResourcesPayload(entries), load_status=LoadStatus.LOADED)
            if not isinstance(state, ResourceState) or state.key != key or state.load_status != LoadStatus.LOADED:
                raise ValueError('The database resource loader returned an invalid state.')
            self.workspace.state.load(BaselineEvidence(key, Existence.PRESENT, state, state.server_version))
        self.workspace.state.mark_affected(key)
        self.plan_builder.observe(self.workspace.state.baselines.entries.values())
        return self.workspace.state.resources[key]

    async def _project_resource_label(self, original, schema):
        # One label is exposed through two API representations. Only the schema
        # endpoint writes it; the database change is a local projection.
        # Existing schema-only preview services may not expose database resources.
        if not hasattr(self.form_service, 'get_database_resources'):
            return
        try:
            before = await self._database(schema['databaseId'])
        except AttributeError:
            return
        entries = {identity: dict(entry) for identity, entry in before.payload.entries.items()}
        if entries[schema['id']].get('label') != original.get('label'):
            raise ValueError('Schema and database representations disagree on the current form label.')
        entries[schema['id']]['label'] = schema['label']
        self.plan_builder.observe(self.workspace.state.baselines.entries.values())
        after = self.workspace.state.put(before.key, DatabaseResourcesPayload(entries, before.payload.order))
        return ResourceProjection(before, after, (Footprint(before.key, schema['id'], ('label',)),
            Footprint(before.key, schema['id'], existence=True), Footprint(before.key, schema['id'], ('type',))))

    async def _add_modify_form(self, command, current, *, scope_context=None):
        from .commands.move_form import MoveFormCommand

        if self.form_service is None:
            return command.execute(current)
        database = self.workspace.state.active_database_id
        warnings = await self._load_context_database(database)
        if warnings:
            return ExecutionResult(current, warnings, skipped=True)
        state = await self._database(database)
        from .name_lookup import find_named_id
        names = {identity: entry.get('label') for identity, entry in state.payload.entries.items()
                 if entry.get('type') == 'FORM'}
        try:
            identity = find_named_id(command.form_name, names, kind='Form')
        except ValueError as exc:
            return ExecutionResult.warning(current, str(exc), 'ambiguous_form_name', skipped=True)
        if identity is None:
            result = await self._add_form(command.as_add_form(), current, scope_context=scope_context)
            return with_prepared_command(result, command)

        try:
            schema, tree_warnings = await self.workspace.select_with_tree(
                identity, self.form_service, self.related_schemas)
            if schema.get('databaseId') != database:
                raise FormSelectionError('The selected schema does not match the form name in the active database.')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ExecutionResult.warning(current, f'Could not select form {command.form_name!r}. {exc}',
                                           'form_switch_failed', skipped=True)
        self._plan_form_selection(self.workspace.key_for(schema), scope_context)
        if command.folder is None:
            result = ExecutionResult(schema, success_message=f'Active form: {command.form_name}')
        else:
            result = await self._move_form(MoveFormCommand(False, command.folder, form=schema['id']), schema)
            if result.skipped:
                # Keep the selected form active when only the destination failed.
                result = replace(result, skipped=False)
        return with_prepared_command(replace(result, diagnostics=tree_warnings + result.diagnostics), command)

    async def _add_form(self, command, current, *, scope_context=None):
        from .form_folders import resolve_folder
        database = self.workspace.state.active_database_id
        warnings = await self._load_context_database(database)
        if warnings:
            return ExecutionResult(current, warnings, skipped=True)
        before_db = await self._database(database)
        entries = {identity: dict(entry) for identity, entry in before_db.payload.entries.items()}
        try:
            parent, _ = resolve_folder(database, entries, command.folder)
        except ValueError as exc:
            return ExecutionResult.warning(current, str(exc), 'folder_selector_failed', skipped=True)
        identity = 'pending-form:' + self._attempt_id
        schema = dict(id=identity, label=command.form_name, databaseId=database, schemaVersion=1, elements=[])
        key = ResourceKey.form(identity)
        before = ResourceState(key, FormPayload(schema), load_status=LoadStatus.LOADED, lifecycle=Lifecycle.DELETED)
        self.workspace.state.load(BaselineEvidence(key, Existence.ABSENT))
        after = self.workspace.state.put_form(schema, lifecycle=Lifecycle.NEW)
        self.workspace.state.activate(key)
        entries[identity] = dict(id=identity, label=command.form_name, type='FORM', parentId=parent, visibility='PRIVATE')
        after_db = self.workspace.state.put(before_db.key, DatabaseResourcesPayload(entries))
        projection = ResourceProjection(before_db, after_db, tuple(
            Footprint(before_db.key, identity, (prop,)) for prop in ('type', 'label', 'parentId')))
        prerequisites = (MemberRef(before_db.key, parent),) if parent != database else ()
        self.plan_builder.observe(self.workspace.state.baselines.entries.values())
        self._plan_form_selection(key, scope_context)
        effect = SchemaEffect(self._attempt_id + ':create', self._attempt_id, key, before, after,
                              prerequisites=prerequisites, projections=(projection,), creation=True)
        self.plan_builder.add(effect)
        self.effects += (effect,)
        self.related_schemas.update(self.workspace.latest)
        return with_prepared_command(ExecutionResult(schema, success_message=f'Added form: {command.form_name}'), command)

    async def _move_form(self, command, schema):
        from .form_folders import form_location, resolve_folder
        before = await self._database(schema['databaseId'])
        entries = {identity: dict(entry) for identity, entry in before.payload.entries.items()}
        try:
            location = form_location(schema['databaseId'], entries, schema['id'])
        except ValueError as exc:
            if command.current_form:
                raise
            return ExecutionResult.warning(schema, str(exc), 'form_location_unavailable', skipped=True)
        try:
            parent, path = resolve_folder(schema['databaseId'], entries, command.folder)
        except ValueError as exc:
            return ExecutionResult.warning(schema, str(exc), 'folder_selector_failed', skipped=True)
        # Normalize an omitted parent to the documented database-root identity.
        if location['parentId'] == parent:
            return with_prepared_command(ExecutionResult(schema, success_message='Form already in destination folder.'), command)
        entries[schema['id']]['parentId'] = parent
        self.plan_builder.observe(self.workspace.state.baselines.entries.values())
        after = self.workspace.state.put(before.key, DatabaseResourcesPayload(entries, before.payload.order))
        prerequisites = (MemberRef(before.key, parent),) if parent != schema['databaseId'] else ()
        if self.plan_builder.open_creation(ResourceKey.form(schema['id'])) is not None:
            state = self.workspace.state.resources[ResourceKey.form(schema['id'])]
            projection = ResourceProjection(before, after, tuple(
                Footprint(before.key, schema['id'], (prop,)) for prop in ('type', 'label', 'parentId')))
            effect = SchemaEffect(self._attempt_id + ':move', self._attempt_id, state.key, state, state,
                                  prerequisites=prerequisites, projections=(projection,))
            self.plan_builder.add(effect)
            self.effects += (effect,)
            return with_prepared_command(ExecutionResult(schema, success_message=f'Moved form to {path}'), command)
        effect = ResourcePatchEffect(self._attempt_id + ':resource', self._attempt_id, before.key, before, after,
            ((schema['id'], 'parentId'),), subjects=(ResourceKey.form(schema['id']),), prerequisites=prerequisites)
        self.plan_builder.add(effect)
        self.effects += (effect,)
        return with_prepared_command(ExecutionResult(schema, success_message=f'Moved form to {path}'), command)

    async def _delete_form(self, command, current, *, scope_member=False):
        from .schema_names import referenced_form_ids
        active_key = self.workspace.state.active_form_key
        active_db = self.workspace.state.active_database_id
        try:
            if command.current_form:
                schema = current
            elif scope_member:
                schema = await self.workspace.select(command.form, self.form_service)
            else:
                schema = await self._switch_form(command.form)
        except ValueError as exc:
            return ExecutionResult.warning(current, str(exc), 'form_selector_failed', skipped=True)
        finally:
            self.workspace.state.active_form_key = active_key
            self.workspace.state.active_database_id = active_db
        if schema is None:
            return ExecutionResult.warning(current, 'An active form is required. Command skipped.', 'missing_form', skipped=True)
        key = ResourceKey.form(schema['id'])
        before = await self._database(schema['databaseId'])
        if key.identity not in before.payload.entries:
            return ExecutionResult.warning(current, 'Form is unavailable. Command skipped.', 'missing_form', skipped=True)
        if before.payload.entries[key.identity].get('type') != 'FORM':
            return ExecutionResult.warning(current, 'Direct subform deletion is not supported. Command skipped.', 'subform_unsupported', skipped=True)
        removed = {key.identity}
        while True:
            children = {i for i, e in before.payload.entries.items() if e.get('parentId') in removed}
            if children <= removed:
                break
            removed.update(children)
        entries = {i: e for i, e in before.payload.entries.items() if i not in removed}
        after = self.workspace.state.put(before.key, DatabaseResourcesPayload(entries))
        form = self.workspace.state.resources[key]
        effect_id = self._attempt_id + ':delete_form'
        self.plan_builder.delete_form(self._attempt_id, effect_id, before, after, form)
        effect = FormDeleteEffect(effect_id, self._attempt_id, before.key, before, after, (key,))
        self.effects += (effect,)
        diagnostics = []
        for identity, candidate in {**self.related_schemas, **self.workspace.latest}.items():
            if identity not in removed and removed.intersection(referenced_form_ids(candidate)):
                diagnostics.append(Diagnostic('warning',
                    f'{candidate.get("label") or identity}: deletion breaks a reference to {schema.get("label") or key.identity}.',
                    code='deleted_form_dependency'))
        for identity in removed:
            local_key = ResourceKey.form(identity)
            local = self.workspace.state.resources.get(local_key)
            if local is not None and local.payload is not None:
                self.workspace.state.put(local_key, local.payload, lifecycle=Lifecycle.DELETED)
            self.related_schemas.pop(identity, None)
        self.resolution_cache = deepcopy(self.resolution_cache)
        self.resolution_cache.setdefault('deleted_form_ids', set()).update(removed)
        if active_key is not None and active_key.identity in removed:
            self.workspace.state.active_form_key = None
            current = None
        return with_prepared_command(ExecutionResult(current, tuple(diagnostics),
            success_message=f'Deleted form: {schema.get("label") or key.identity}'), command)
