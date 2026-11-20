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
from .resource_state import ResourceKey, ResourceState, DatabaseResourcesPayload, BaselineEvidence, Existence, LoadStatus
from .planning import PlanBuilder, SchemaEffect, ResourcePatchEffect, MemberRef, ResourceProjection, Footprint
from .schema_names import readable_schema, load_default_record_labels

ACTIVE_DRAFT = ContextVar('activity_manager_application_draft', default=None)


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
        self._snapshot_builder = DisplaySnapshotBuilder(generation)
        self._effects = ()
        self._plan_builder = PlanBuilder(active_form=self._workspace.state.active_form_key)
        self._plan_builder.observe(self._workspace.state.baselines.entries.values())
        self._variables = {}
        self.scope = None
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
                           if name != 'variables'}
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
                            on_success=None, scope_context=None):
        draft = self._new_draft(variables, form_key)
        self._attempt_id = self._application_id()
        self._position += 1
        result = None
        token = ACTIVE_DRAFT.set(draft)
        try:
            result = await self._apply_candidate(application, deepcopy(current), draft.variables,
                                                 scope_member=scope_member, scope_context=scope_context)
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
            self.executions[index] = execution
            current = execution.schema
            stopped = execution.status == 'error'
        self._publish_evaluation()
        return self.executions

    async def evaluate(self):
        await self._load_initial_context()
        if not self.is_current():
            return
        if self.resolve_schema is None:
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
        from .commands.with_form import WithFormCommand
        from .commands.modify_form import ModifyFormCommand
        from .commands.move_form import MoveFormCommand
        from .commands.set_variable import SetVariableCommand
        command = getattr(command, 'command', command)
        if isinstance(command, (WithFormCommand, ModifyFormCommand)) or (
                isinstance(command, MoveFormCommand) and not command.current_form):
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
            return replace(CommandExecution.from_result(ExecutionResult.warning(
                current, f'No field codes matched pattern {command.field_pattern!r}. Schema unchanged.', 'no_matches')),
                field_iterations=True)
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


    @staticmethod
    def _summarize_iterations(children, current):
        statuses = [child.status for child in children]
        status = next((value for value in ('error', 'resolving', 'warning', 'not_executed')
                       if value in statuses), 'applied')
        diagnostics = tuple(Diagnostic(item.severity,
            f'Iteration {index + 1} ({child.application_label}): {item.message}', code=item.code)
            for index, child in enumerate(children) for item in child.diagnostics)
        message = '\n'.join(f'Iteration {index + 1} ({child.application_label}): {child.message}'
                            for index, child in enumerate(children))
        if 'error' in statuses:
            message = f'Stopped at iteration {statuses.index("error") + 1}.\n' + message
        reached = [child for child in children if child.schema is not None]
        final = reached[-1] if reached else None
        return CommandExecution(status, current, message, final.names_schema if final else None,
            diagnostics, tuple(children), snapshot_issues=final.issues if final else (), form_snapshots=final.form_snapshots if final else (),
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


    async def _apply_candidate(self, application, current, variables, *, scope_member=False, scope_context=None):
        from .commands.with_form import WithFormCommand
        from .commands.modify_form import ModifyFormCommand
        from .commands.move_form import MoveFormCommand
        from .commands.set_variable import SetVariableCommand
        from .command_templates import command_parameters
        from .command_iteration import PreparedCommand
        try:
            prepared = application.command if isinstance(application, PreparedCommand) else command_parameters(application, variables)
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
                    schema = (await self.workspace.select(prepared.form, self.form_service) if scope_member else
                              await self.workspace.switch(prepared.form, self.form_service, current, self.related_schemas))
                except FormSelectionError as exc:
                    return ExecutionResult.warning(current, str(exc), 'form_switch_failed', skipped=True)
                self.related_schemas.update(self.workspace.latest)
                if scope_context is not None:
                    scope_id, keys = scope_context
                    self.plan_builder.begin_scope(scope_id, keys)
                    self.plan_builder.select_form(self.workspace.key_for(schema), scope_id=scope_id)
                else:
                    self.plan_builder.select_form(self.workspace.key_for(schema))
                context_warnings = await self._load_context_database(schema['databaseId'])
                if isinstance(prepared, MoveFormCommand):
                    if context_warnings:
                        return ExecutionResult(current, context_warnings, skipped=True)
                    result = await self._move_form(replace(prepared, form=schema['id']), schema)
                    return replace(result, diagnostics=context_warnings + result.diagnostics)
                if isinstance(prepared, ModifyFormCommand):
                    result = apply_command_locally(PreparedCommand(replace(prepared, form=schema['id'])), schema,
                        self.related_schemas, self.database_names, variables=variables)
                    if not result.has_errors and prepared.new_name is not None:
                        projection = await self._project_resource_label(schema, result.schema)
                        result = replace(result, resource_projections=(projection,) if projection is not None else ())
                    return replace(result, diagnostics=context_warnings + result.diagnostics)
                return with_prepared_command(ExecutionResult(schema, context_warnings, names_schema=readable_schema(
                        schema, self.related_schemas, self.database_names),
                    snapshot_issues=self.workspace.issues.get(schema.get('id'), ()),
                    success_message=f'Active form: {schema.get("label") or schema["id"]}'), prepared)
        except Exception as exc:
            return ExecutionResult.error(current, str(exc), 'form_switch_failed')
        async def resolve(schema):
            self.related_schemas.update(self.workspace.latest)
            self.related_schemas[schema.get('id')] = schema
            return await self.resolve_schema(schema)
        async def lookup(command, schema):
            return await self.lookup_variable(command, schema if schema is not None else
                                              {'databaseId': self.workspace.state.active_database_id})
        result = await apply_command(PreparedCommand(prepared), current, resolve,
            related_schemas=self.related_schemas, database_names=self.database_names,
            variables=variables, lookup_variable=lookup if self.lookup_variable is not None else None,
            reserved_identities=self.workspace.reserved.get((current or {}).get("id")))
        if isinstance(prepared, SetVariableCommand):
            # Variables do not run the schema resolver or change the schema.
            result = replace(result, snapshot_issues=self.workspace.issues.get((current or {}).get('id'), ()))
        return result


    async def _resolve_commands(self, version, current, commands):
        from .commands.with_form import WithFormCommand
        from .commands.modify_form import ModifyFormCommand
        from .commands.move_form import MoveFormCommand
        for index, command in enumerate(commands):
            if not self.is_current():
                return
            self._index, self._position = index, 0
            self.executions[index] = CommandExecution('resolving', None, 'Resolving names...')
            self._publish_evaluation()
            if (isinstance(command, (WithFormCommand, ModifyFormCommand, MoveFormCommand))
                    and not (isinstance(command, MoveFormCommand) and command.current_form)):
                next_variables = deepcopy(self.variables)
                for name in self.assigned:
                    next_variables.pop(name, None)
                transitioned = False
                def commit_scope(scope):
                    nonlocal transitioned
                    if not transitioned:
                        self.variables.clear()
                        self.variables.update(next_variables)
                        self.scope = scope
                        self.assigned.clear()
                        transitioned = True
                if command.form_pattern is not None:
                    _, execution = await self._start_form_scope(command, current, next_variables, version, commit_scope)
                else:
                    execution = await self._run_connected_command(index, current, next_variables, version,
                        on_success=lambda: commit_scope(None))
            elif self.scope is not None:
                execution = await self._run_form_scope(index, self.scope, current, version)
            else:
                execution = await self._run_connected_command(index, current, self.variables, version)
            if not self.is_current():
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

    async def _start_form_scope(self, command, current, variables, version, commit_scope):
        from .command_iteration import compile_pattern
        from .command_templates import substitute_text
        scope = []
        draft = self._new_draft(variables)
        failure = None
        token = ACTIVE_DRAFT.set(draft)
        try:
            database_id = self.workspace.state.active_database_id
            if not database_id:
                failure = ExecutionResult.warning(current, 'An active database is required for a form pattern.',
                                                  'missing_database', skipped=True)
            if failure is not None:
                return scope, self._snapshot_execution(CommandExecution.from_result(failure))
            if self.form_service is None:
                raise ValueError('WithForm requires a connected form loader.')
            pattern = compile_pattern(substitute_text(command.form_pattern, variables, namespaces={'vars'}), kind='form')
            names = dict(await self.form_service.get_form_names(database_id))
            if not self.is_current():
                return scope, None
            names.update({identity: schema.get('label') or identity
                          for identity, schema in self.workspace.latest.items()
                          if schema.get('databaseId') == database_id})
            for identity, name in sorted(names.items(), key=lambda item: (item[1].casefold(), item[0])):
                match = pattern.fullmatch(name)
                if match is None:
                    continue
                local = {**variables, 'form.name': name, 'form.index': len(scope) + 1}
                local.update({f'form.{i}': value for i, value in enumerate(match.groups(), 1)})
                local.update({f'form.{key}': value for key, value in match.groupdict().items()})
                scope.append(dict(identity=identity, name=name, variables=local, form_key=None))
        except Exception as exc:
            failure = ExecutionResult.error(current, str(exc), 'form_selector_failed')
        finally:
            ACTIVE_DRAFT.reset(token)
        if failure is not None:
            return scope, self._snapshot_execution(CommandExecution.from_result(failure))
        scope_id = self.authored_commands[self._index].authored_id
        scope_keys = tuple(ResourceKey.form(target['identity']) for target in scope)
        if not scope:
            builder = deepcopy(self.plan_builder)
            builder.begin_scope(scope_id, scope_keys)
            self.plan_builder = builder
            commit_scope(scope)
            self._sync_context()
        children = self._pending_forms(scope)
        for i, target in enumerate(scope):
            from .command_templates import command_parameters
            concrete = command_parameters(replace(command, form=target['identity'], form_pattern=None), target['variables'])
            result = await self._apply_actual(PreparedCommand(concrete), current, target['variables'],
                                             scope_member=True, on_success=lambda: commit_scope(scope),
                                             scope_context=(scope_id, scope_keys))
            if result is None or not self.is_current():
                return scope, None
            if not result.application_failed:
                target['form_key'] = self.workspace.state.active_form_key
            children[i] = replace(CommandExecution.from_result(result), field_code=target['name'],
                                  target_keys=(ResourceKey.form(target['identity']),))
            current = result.schema
            if result.application_failed:
                break
        return scope, self._form_summary(children, current)

    @staticmethod
    def _pending_forms(scope):
        return [CommandExecution('not_executed', None, 'Did not execute: an earlier form failed.',
                                 field_code=target['name'],
                                 target_keys=(ResourceKey.form(target['identity']),)) for target in scope]


    def _form_summary(self, children, current):
        if not children:
            return replace(self._snapshot_execution(CommandExecution.from_result(ExecutionResult.warning(
                current, 'No form names matched. No applications in this scope.', 'no_matches'))), form_iterations=True)
        summary = self._summarize_iterations(children, current)
        def flatten(items, context=(), targets=()):
            for child in items:
                path = context + (child.application_context or ((child.field_code,) if child.field_code else ()))
                keys = child.target_keys or targets
                if not keys and child.schema is not None:
                    keys = (ResourceKey.form(child.schema['id']),)
                if child.iterations or child.field_iterations or child.form_iterations:
                    yield from flatten(child.iterations, path, keys)
                else:
                    yield replace(child, application_context=path, target_keys=keys)
        return replace(self._snapshot_execution(summary), form_iterations=True, iterations=tuple(flatten(children)))


    async def _run_form_scope(self, index, scope, current, version):
        from .commands.set_variable import SetVariableCommand
        children = self._pending_forms(scope)
        plans = [None] * len(scope)
        command = self.commands[index]
        for i, target in enumerate(scope):
            context = (target['name'],)
            if not isinstance(command, IterativeCommand) and isinstance(getattr(command, 'field_code', None), str):
                from .command_templates import substitute_selector
                try:
                    context += (substitute_selector(command.field_code, target['variables']),)
                except ValueError:
                    context += (command.field_code,)
            children[i] = replace(children[i], application_context=context)
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
            current = deepcopy(self.workspace.state.resources[target['form_key']].payload.to_dict())
            execution = await self._run_connected_command(index, current, target['variables'], version,
                                                          publish=False, targets=plans[i], form_key=target['form_key'],
                on_success=(lambda: self.assigned.add(f'vars.{command.name}'))
                if isinstance(command, SetVariableCommand) else None)
            if not self.is_current():
                return None
            children[i] = replace(execution, field_code=target['name'],
                                  application_context=children[i].application_context,
                                  target_keys=children[i].target_keys)
            current = execution.schema
            self.executions[index] = self._form_summary(children, current)
            self._publish_evaluation()
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
        # endpoint writes it; the database change is a verified projection.
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

    async def _move_form(self, command, schema):
        from .form_folders import form_location, resolve_folder
        before = await self._database(schema['databaseId'])
        entries = {identity: dict(entry) for identity, entry in before.payload.entries.items()}
        location = form_location(schema['databaseId'], entries, schema['id'])
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
        effect = ResourcePatchEffect(self._attempt_id + ':resource', self._attempt_id, before.key, before, after,
            ((schema['id'], 'parentId'),), subjects=(ResourceKey.form(schema['id']),), prerequisites=prerequisites)
        self.plan_builder.add(effect)
        self.effects += (effect,)
        return with_prepared_command(ExecutionResult(schema, success_message=f'Moved form to {path}'), command)
