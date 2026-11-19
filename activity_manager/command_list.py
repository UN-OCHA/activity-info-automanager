from copy import deepcopy
import asyncio
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import uuid4

from .immutable import FrozenMap, command_parameters_snapshot
from .resource_state import ResourceKey
from .application_results import (
    AuthoredCommand, DisplayState, DisplaySnapshotBuilder, adapt_authored_result,
)

from nicegui import ui

from .command_card import render_command_card
from .multi_form import FormWorkspace
from .schema_names import readable_schema
from .command_execution import (
    Diagnostic, ExecutionResult, ExecutionStatus, EXECUTABLE_STATUSES, apply_command, apply_command_locally,
    with_prepared_command,
)
from .command_parser import CommandTextError, parse_command_text
from .command_iteration import IterativeCommand, IterationTarget


class Command(Protocol):
    """An authored command; iterative commands expand before execution."""
    def to_syntax(self) -> str: ...


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
    opschema: tuple[dict, ...] = ()
    form_issues: dict | None = None
    form_iterations: bool = False
    field_iterations: bool = False
    application_context: tuple[str, ...] = ()
    display_state: DisplayState | None = None
    prepared_syntax: str | None = None
    prepared_parameters: FrozenMap | None = None
    target_keys: tuple[ResourceKey, ...] = ()
    target_member_ids: tuple[str, ...] = ()

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
                   opschema=result.opschema, snapshot_issues=result.snapshot_issues, form_issues=result.form_issues,
                   display_state=result.display_state, prepared_syntax=result.prepared_syntax,
                   prepared_parameters=result.prepared_parameters, target_member_ids=result.target_member_ids)


class CommandListManager:
    """Own ordered command objects and their editable text representation."""

    def __init__(
        self,
        *,
        on_selection_change=None,
        on_execute=None,
        on_text_mode_change=None,
        resolve_schema=None,
        lookup_variable=None,
        form_service=None,
    ) -> None:
        self.form_service = form_service
        self.workspace = FormWorkspace(None)
        self._base_related = {}
        self.related_schemas = {}
        self.database_names = {}
        self.resolve_schema = resolve_schema
        self.lookup_variable = lookup_variable
        self.reference_defaults = {}
        self.resolution_cache = {}
        self._evaluation_version = 0
        self._authored_bindings = []
        self.authored_commands = ()
        self.snapshot_builder = DisplaySnapshotBuilder(0)
        self._evaluation_task = None
        self.commands: list[Command] = []
        self.executions: list[CommandExecution] = []
        self.source_schema: dict[str, Any] | None = None
        self.selected_index: int | None = None
        self.selected_iteration: int | None = None
        self.expanded_commands: set[int] = set()
        self.expanded_text_commands: set[int] = set()
        self.advanced_draft = ''
        self.stored_text = ''
        self.on_selection_change = on_selection_change
        self.on_execute = on_execute
        self.on_text_mode_change = on_text_mode_change
        self.busy = False
        self.current_view = 'list'

        with ui.row().classes('w-full items-center justify-between gap-2'):
            self.view_toggle = ui.toggle(
                {'list': 'List', 'text': 'Text'},
                value='list',
                on_change=self._view_changed,
            )
            self.count_badge = ui.badge('0 commands', color='grey')
        ui.separator()

        self.simple_view = ui.column().classes('command-list-body w-full gap-2')
        with self.simple_view:
            self.simple_content = ui.column().classes('w-full shrink-0 gap-2')

        self.advanced_view = ui.column().classes('command-text-body w-full')
        with self.advanced_view:
            self.advanced_editor = ui.codemirror(
                language='Python', line_wrapping=True,
                on_change=self._advanced_changed,
            ).classes('command-text-editor w-full border rounded').props('aria-label=Commands')
            with ui.row().classes('w-full shrink-0 gap-1'):
                self.wrap_button = ui.button(
                    'Wrap', icon='wrap_text', on_click=self._toggle_word_wrap,
                ).props('flat dense color=primary aria-pressed=true')
                self.copy_button = ui.button(
                    'Copy', icon='content_copy',
                ).props('flat dense')
                # Copy the live CodeMirror document, including edits that have
                # not yet reached Python, without a server round trip.
                self.copy_button.on('click', self._copy_failed, js_handler='''async () => {
                    if (!navigator.clipboard?.writeText) {
                        emit({error: 'unavailable'});
                        return;
                    }
                    try {
                        const editor = getElement(__EDITOR_ID__).editor;
                        await navigator.clipboard.writeText(editor.state.doc.toString());
                    } catch (error) {
                        emit({error: 'failed'});
                    }
                }'''.replace('__EDITOR_ID__', str(self.advanced_editor.id)))
                self.paste_button = ui.button(
                    'Paste & replace', icon='content_paste',
                ).props('flat dense')
                # Read during the browser click, before a server round trip can
                # lose the user activation required by Firefox.
                self.paste_button.on('click', self._paste_text, js_handler='''async () => {
                    if (!navigator.clipboard?.readText) {
                        emit({error: 'unavailable'});
                        return;
                    }
                    try {
                        const text = await navigator.clipboard.readText();
                        emit({text});
                    } catch (error) {
                        emit({error: 'denied'});
                    }
                }''')

        with ui.column().classes('w-full'):
            self.execute_button = ui.button(
                'Execute', icon='play_arrow', on_click=on_execute,
            ).classes('w-full')
            self.execute_button.disable()
            with ui.row().classes('w-full gap-2') as self.text_actions:
                self.apply_button = ui.button(
                    'Apply', icon='check', on_click=self._apply_text,
                ).classes('grow')
                self.discard_button = ui.button(
                    'Discard', icon='undo', on_click=self._open_discard_dialog,
                ).props('outline').classes('grow')
                self.apply_button.disable()
                self.discard_button.disable()

        with ui.dialog() as self.discard_dialog, ui.card().classes('w-full max-w-lg'):
            self.discard_dialog.props('persistent')
            ui.label('Discard text changes?').classes('text-lg font-medium')
            ui.label(
                'Your edits to the command text will be lost. '
                'The command list will remain unchanged.'
            )
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Continue', on_click=self.discard_dialog.close).props('flat')
                ui.button(
                    'Discard', on_click=self._discard_text,
                ).props('color=negative')

        with ui.dialog() as self.parse_error_dialog, ui.card().classes('w-full max-w-lg'):
            self.parse_error_dialog.props('persistent')
            ui.label('Command text error').classes('text-lg font-medium text-red-700')
            self.parse_error_location = ui.label().classes('text-sm font-medium')
            self.parse_error_message = ui.label()
            self.parse_error_source = ui.label().classes(
                'w-full rounded bg-slate-100 p-2 whitespace-pre-wrap font-mono text-sm'
            )
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Fix', on_click=self.parse_error_dialog.close).props('flat')
                ui.button(
                    'Discard', on_click=self._discard_parse_error,
                ).props('color=negative')

        self.advanced_view.set_visibility(False)
        self.text_actions.set_visibility(False)
        self._render_simple_view()

    def add_command(self, command: Command) -> None:
        self.commands.append(command)
        self._regenerate_advanced_draft()
        self._evaluate_commands()
        self._update_count()

    def move_command(self, old_index: int, new_index: int) -> None:
        """Reorder discrete commands and refresh their text serialization."""
        if not 0 <= old_index < len(self.commands):
            raise IndexError('old command index out of range')
        if not 0 <= new_index < len(self.commands):
            raise IndexError('new command index out of range')
        if old_index == new_index:
            return
        command = self.commands.pop(old_index)
        self.commands.insert(new_index, command)
        if self.selected_index == old_index:
            self.selected_index = new_index
        elif self.selected_index is not None:
            if old_index < self.selected_index <= new_index:
                self.selected_index -= 1
            elif new_index <= self.selected_index < old_index:
                self.selected_index += 1
        self._regenerate_advanced_draft()
        self._evaluate_commands()

    def delete_command(self, index: int) -> None:
        """Delete a discrete command and refresh its text serialization."""
        if not 0 <= index < len(self.commands):
            raise IndexError('command index out of range')
        command = self.commands.pop(index)
        self.expanded_commands.discard(id(command))
        self.expanded_text_commands.discard(id(command))
        if self.selected_index == index:
            self.selected_index = None
            self.selected_iteration = None
        elif self.selected_index is not None and self.selected_index > index:
            self.selected_index -= 1
        self._regenerate_advanced_draft()
        self._evaluate_commands()
        self._update_count()

    def set_schema(self, schema: dict[str, Any] | None) -> None:
        self._base_related = deepcopy(self.related_schemas)
        self.source_schema = deepcopy(schema)
        self._evaluate_commands()

    def select_command(self, index: int) -> None:
        if not 0 <= index < len(self.commands):
            raise IndexError('command index out of range')
        self.selected_index = None if self.selected_index == index and self.selected_iteration is None else index
        self.selected_iteration = None
        self._render_simple_view()
        self._notify_selection()

    def clear_selection(self) -> None:
        self.selected_index = None
        self.selected_iteration = None
        self._render_simple_view()
        self._notify_selection()

    def select_iteration(self, index, iteration):
        if not 0 <= index < len(self.executions) or not 0 <= iteration < len(self.executions[index].iterations):
            raise IndexError('iteration index out of range')
        self.expanded_commands.add(id(self.commands[index]))
        self.selected_index = index
        self.selected_iteration = iteration
        self._render_simple_view()
        self._notify_selection()

    def toggle_iterations(self, index):
        key = id(self.commands[index])
        if key in self.expanded_commands:
            self.expanded_commands.remove(key)
            if self.selected_index == index:
                self.selected_iteration = None
        else:
            self.expanded_commands.add(key)
        self._render_simple_view()
        self._notify_selection()

    def _application_steps(self, index, current, variables=None, targets=None, *, record_progress=True):
        """One orchestration path shared by synchronous and connected evaluation.

        Yield concrete applications; callers send back their normal results.
        """
        command = self.commands[index]
        if not isinstance(command, IterativeCommand):
            result = yield command
            return CommandExecution.from_result(result)
        try:
            if targets is None:
                targets = command.targets(current, variables)
        except ValueError as exc:
            return CommandExecution.from_result(ExecutionResult.error(current, str(exc), 'invalid_selector'))
        if not targets:
            return replace(CommandExecution.from_result(ExecutionResult.warning(
                current, f'No field codes matched pattern {command.field_pattern!r}. Schema unchanged.', 'no_matches')),
                field_iterations=True)
        children = [CommandExecution('not_executed', None, 'Waiting for the previous iteration.',
                                     field_code=target.original_code, target=target,
                                     target_keys=(self.workspace.key_for(current),)) for target in targets]
        previous = None
        for iteration, target in enumerate(targets):
            children[iteration] = CommandExecution('resolving', None, 'Applying iterationÃ¢â‚¬Â¦',
                                                    field_code=target.original_code, target=target)
            if record_progress:
                self.executions[index] = self._summarize_iterations(children, current)
            try:
                concrete = target.prepare(command, current, previous, variables)
            except ValueError as exc:
                concrete = None
                result = ExecutionResult.error(current, str(exc), 'invalid_iteration')
            else:
                result = yield concrete
            execution = self._snapshot_execution(CommandExecution.from_result(result))
            children[iteration] = CommandExecution(
                execution.status, execution.schema, execution.message, execution.names_schema,
                execution.diagnostics, field_code=target.original_code,
                concrete_command=concrete.command if concrete else None, target=target, opschema=execution.opschema,
                snapshot_issues=execution.issues, form_issues=execution.form_issues,
                display_state=execution.display_state, prepared_syntax=execution.prepared_syntax,
                prepared_parameters=execution.prepared_parameters, target_member_ids=execution.target_member_ids,
                target_keys=(self.workspace.key_for(current),))
            current = execution.schema
            if execution.status == 'error':
                for later in range(iteration + 1, len(children)):
                    children[later] = CommandExecution('not_executed', None,
                        'Did not execute: an earlier iteration failed.', field_code=targets[later].original_code,
                        target=targets[later], target_keys=(self.workspace.key_for(current),))
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
            diagnostics, tuple(children), snapshot_issues=final.issues if final else (), opschema=final.opschema if final else (),
            form_issues=final.form_issues if final else None,
            display_state=final.display_state if final else None)

    @property
    def results(self):
        return tuple(adapt_authored_result(authored, execution, self._evaluation_version)
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
        metadata = enriched.legacy_metadata()
        identity = (current.schema or {}).get('id')
        children = tuple(replace(child, display_state=enriched,
                                 names_schema=metadata.get((child.schema or {}).get('id'), (None, ()))[0])
                         if child.display_state is display else child for child in current.iterations)
        self.executions[-1] = replace(current, display_state=enriched, iterations=children,
                                     names_schema=metadata.get(identity, (None, ()))[0])
        return enriched

    def target_schema(self) -> dict[str, Any] | None:
        if not self.executions or not all(
            execution.status in EXECUTABLE_STATUSES for execution in self.executions
        ):
            return None
        return deepcopy(self.executions[-1].schema)

    def target_opschema(self):
        return deepcopy(self.executions[-1].opschema) if self.target_schema() is not None else None

    def _save_result(self, result):
        if result.schema is not None:
            identity = result.schema.get('id')
            if result.has_errors and not result.issues and result.schema == self.workspace.latest.get(identity):
                # An atomic failure leaves the displayed schema and its existing
                # unresolved-reference annotations unchanged.
                result = replace(result, snapshot_issues=self.workspace.issues.get(identity, ()))
            self.workspace.update(result.schema)
            self.workspace.issues[identity] = result.issues
            self.related_schemas.update(self.workspace.latest)
        display = self.snapshot_builder.capture(self.workspace.state, self.related_schemas,
            self.database_names, self.workspace.issues,
            active=self.workspace.key_for(result.schema) if result.schema else None,
            labels=result.record_labels)
        return replace(result, opschema=tuple(deepcopy(self.workspace.opschema)),
                       form_issues=deepcopy(self.workspace.issues), display_state=display)

    def _snapshot_execution(self, execution):
        issues = self.workspace.issues.get((execution.schema or {}).get('id'), execution.issues)
        display = execution.display_state or self.snapshot_builder.capture(
            self.workspace.state, self.related_schemas, self.database_names, self.workspace.issues,
            active=self.workspace.key_for(execution.schema) if execution.schema else None)
        return replace(execution, opschema=tuple(deepcopy(self.workspace.opschema)),
                       snapshot_issues=issues, form_issues=deepcopy(self.workspace.issues), display_state=display)

    async def _apply_connected(self, application, current, variables):
        from .commands.with_form import WithFormCommand
        from .commands.set_variable import SetVariableCommand
        from .command_templates import command_parameters
        from .command_iteration import PreparedCommand
        try:
            prepared = application.command if isinstance(application, PreparedCommand) else command_parameters(application, variables)
            if isinstance(prepared, WithFormCommand):
                if self.form_service is None:
                    return prepared.execute(current)
                schema = await self.workspace.switch(prepared.form, self.form_service, current, self.related_schemas)
                self.related_schemas.update(self.workspace.latest)
                return with_prepared_command(ExecutionResult(schema, names_schema=readable_schema(
                        schema, self.related_schemas, self.database_names),
                    snapshot_issues=self.workspace.issues.get(schema.get('id'), ()),
                    success_message=f'Active form: {schema.get("label") or schema["id"]}'), prepared)
        except Exception as exc:
            return ExecutionResult.error(current, str(exc), 'form_switch_failed')
        async def resolve(schema):
            self.related_schemas.update(self.workspace.latest)
            self.related_schemas[schema.get('id')] = schema
            return await self.resolve_schema(schema)
        result = await apply_command(PreparedCommand(prepared), current, resolve,
            related_schemas=self.related_schemas, database_names=self.database_names,
            variables=variables, lookup_variable=self.lookup_variable,
            reserved_identities=self.workspace.reserved.get(current.get("id")))
        if isinstance(prepared, SetVariableCommand):
            # Variables do not run the schema resolver or change the schema.
            result = replace(result, snapshot_issues=self.workspace.issues.get(current.get('id'), ()))
        return result

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for control in (self.view_toggle, self.advanced_editor, self.paste_button):
            (control.disable if busy else control.enable)()
        self._render_simple_view()
        self._update_execute_button()
        self._update_text_action_buttons()

    def _evaluate_commands(self) -> None:
        self.workspace = FormWorkspace(self.source_schema)
        self.related_schemas = deepcopy(self._base_related)
        self.related_schemas.update(self.workspace.latest)
        self._evaluation_version += 1
        self.snapshot_builder = DisplaySnapshotBuilder(self._evaluation_version)
        available = list(self._authored_bindings)
        bindings, authored = [], []
        for index, command in enumerate(self.commands):
            match = next((i for i, (previous, _) in enumerate(available) if previous is command), None)
            identity = available.pop(match)[1] if match is not None else uuid4().hex
            bindings.append((command, identity))
            authored.append(AuthoredCommand(identity, index, type(command).__name__,
                                            command.to_syntax(), command_parameters_snapshot(command)))
        self._authored_bindings = bindings
        self.authored_commands = tuple(authored)
        if self._evaluation_task is not None:
            self._evaluation_task.cancel()
            self._evaluation_task = None
        if self.resolve_schema is not None and self.source_schema is not None and self.commands:
            self.executions = [CommandExecution('resolving' if index == 0 else 'not_executed', None,
                'Resolving namesÃ¢â‚¬Â¦' if index == 0 else 'Waiting for the previous command.')
                for index in range(len(self.commands))]
            self._publish_evaluation()
            self._evaluation_task = asyncio.create_task(self._resolve_commands(
                self._evaluation_version, deepcopy(self.source_schema), list(self.commands)))
            return
        self.executions.clear()
        variables = {}
        current_schema = deepcopy(self.source_schema)
        stopped = current_schema is None

        for index, command in enumerate(self.commands):
            if stopped:
                message = (
                    'Did not execute: no form schema selected.'
                    if self.source_schema is None
                    else 'Did not execute.'
                )
                self.executions.append(CommandExecution('not_executed', None, message))
                continue
            self.executions.append(CommandExecution('resolving', None, 'Applying commandÃ¢â‚¬Â¦'))
            steps = self._application_steps(index, current_schema, variables)
            try:
                application = next(steps)
                while True:
                    result = apply_command_locally(application, current_schema, self.related_schemas, self.database_names, variables=variables,
                        reserved_identities=self.workspace.reserved.get(current_schema.get("id")))
                    result = self._save_result(result)
                    current_schema = result.schema
                    application = steps.send(result)
            except StopIteration as finished:
                execution = self._snapshot_execution(finished.value)
            self.executions[index] = execution
            current_schema = execution.schema
            stopped = execution.status == 'error'

        self._render_simple_view()
        self._notify_selection()
        self._update_execute_button()

    def _publish_evaluation(self):
        self._render_simple_view()
        self._notify_selection()
        self._update_execute_button()

    async def _resolve_commands(self, version, current, commands):
        from .commands.with_form import WithFormCommand
        from .commands.set_variable import SetVariableCommand
        variables = {}
        scope = None
        assigned = set()
        for index, command in enumerate(commands):
            if version != self._evaluation_version:
                return
            self.executions[index] = CommandExecution('resolving', None, 'Resolving namesÃ¢â‚¬Â¦')
            self._publish_evaluation()
            if isinstance(command, WithFormCommand):
                for name in assigned:
                    variables.pop(name, None)
                assigned.clear()
                scope = None
                if command.form_pattern is not None:
                    scope, execution = await self._start_form_scope(command, current, variables, version)
                else:
                    execution = await self._run_connected_command(index, current, variables, version)
            elif scope is not None:
                if scope and isinstance(command, SetVariableCommand):
                    assigned.add(f'vars.{command.name}')
                execution = await self._run_form_scope(index, scope, current, version)
            else:
                execution = await self._run_connected_command(index, current, variables, version)
            if version != self._evaluation_version:
                return
            current = execution.schema
            self.executions[index] = execution
            if execution.status == 'error':
                for later in range(index + 1, len(commands)):
                    self.executions[later] = CommandExecution('not_executed', None, 'Did not execute: the previous command failed.')
                self._publish_evaluation()
                return
            self._publish_evaluation()

    async def _run_connected_command(self, index, current, variables, version, *, publish=True, targets=None):
        steps = self._application_steps(index, current, variables, targets, record_progress=publish)
        try:
            application = next(steps)
            while True:
                if publish:
                    self._publish_evaluation()
                result = await self._apply_connected(application, current, variables)
                if version != self._evaluation_version:
                    return None
                result = self._save_result(result)
                current = result.schema
                application = steps.send(result)
        except StopIteration as finished:
            return self._snapshot_execution(finished.value)

    async def _start_form_scope(self, command, current, variables, version):
        from .command_iteration import compile_pattern
        from .command_templates import substitute_text
        scope = []
        try:
            if self.form_service is None:
                raise ValueError('WithForm requires a connected form loader.')
            pattern = compile_pattern(substitute_text(command.form_pattern, variables, namespaces={'vars'}), kind='form')
            names = dict(await self.form_service.get_form_names(current['databaseId']))
            if version != self._evaluation_version:
                return scope, None
            names.update({identity: schema.get('label') or identity
                          for identity, schema in self.workspace.latest.items()
                          if schema.get('databaseId') == current['databaseId']})
            for identity, name in sorted(names.items(), key=lambda item: (item[1].casefold(), item[0])):
                match = pattern.fullmatch(name)
                if match is None:
                    continue
                local = {**variables, 'form.name': name, 'form.index': len(scope) + 1}
                local.update({f'form.{i}': value for i, value in enumerate(match.groups(), 1)})
                local.update({f'form.{key}': value for key, value in match.groupdict().items()})
                scope.append(dict(identity=identity, name=name, variables=local, slot=None))
        except Exception as exc:
            return scope, self._snapshot_execution(CommandExecution.from_result(
                ExecutionResult.error(current, str(exc), 'form_selector_failed')))
        children = self._pending_forms(scope)
        for i, target in enumerate(scope):
            try:
                schema = await self.workspace.visit(target['identity'], self.form_service)
                if version != self._evaluation_version:
                    return scope, None
                target['slot'] = self.workspace.active_slot
                self.related_schemas.update(self.workspace.latest)
                result = with_prepared_command(ExecutionResult(schema,
                    snapshot_issues=self.workspace.issues.get(schema['id'], ()),
                    success_message=f'Active form: {target["name"]}'),
                    replace(command, form=target['identity'], form_pattern=None))
            except Exception as exc:
                result = ExecutionResult.error(current, str(exc), 'form_switch_failed')
            if version != self._evaluation_version:
                return scope, None
            result = self._save_result(result)
            children[i] = replace(CommandExecution.from_result(result), field_code=target['name'])
            current = result.schema
            if result.has_errors:
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
                    plans[i] = command.targets(self.workspace.opschema[target['slot']], target['variables'])
                except ValueError:
                    continue  # Report selector errors when their form is reached.
                children[i] = replace(children[i], field_iterations=True, iterations=tuple(
                    CommandExecution('not_executed', None, 'Did not execute: an earlier application failed.',
                                     field_code=field.original_code, target=field) for field in plans[i]))
        self.executions[index] = self._form_summary(children, current)
        self._publish_evaluation()
        for i, target in enumerate(scope):
            self.workspace.active_slot = target['slot']
            current = deepcopy(self.workspace.active_schema)
            execution = await self._run_connected_command(index, current, target['variables'], version,
                                                          publish=False, targets=plans[i])
            if version != self._evaluation_version:
                return None
            children[i] = replace(execution, field_code=target['name'], application_context=children[i].application_context)
            current = execution.schema
            self.executions[index] = self._form_summary(children, current)
            self._publish_evaluation()
            if execution.status == 'error':
                break
        return self._form_summary(children, current)

    def _render_simple_view(self) -> None:
        if self.selected_iteration is not None and self.selected_index is not None:
            execution = self.executions[self.selected_index]
            if self.selected_iteration >= len(execution.iterations):
                self.selected_iteration = None
        self.simple_content.clear()
        with self.simple_content:
            if not self.commands:
                with ui.column().classes('w-full grow items-center justify-center gap-2 py-8'):
                    ui.icon('format_list_numbered', size='2rem').classes('text-slate-400')
                    ui.label('No commands added yet.').classes('text-sm text-slate-500')
                    ui.label('Commands will be reorderable here.').classes('text-xs text-slate-400')
                return
            for index, command in enumerate(self.commands):
                execution = self.executions[index]
                expanded = id(command) in self.expanded_commands
                render_command_card(
                    command, execution,
                    index=index,
                    command_count=len(self.commands),
                    selected_index=self.selected_index,
                    selected_iteration=self.selected_iteration,
                    expanded=expanded,
                    busy=self.busy,
                    text_expanded=id(command) in self.expanded_text_commands,
                    on_toggle_text=self._toggle_command_text,
                    on_select=self.select_command,
                    on_select_iteration=self.select_iteration,
                    on_toggle_iterations=self.toggle_iterations,
                    on_move=self.move_command,
                    on_delete=self.delete_command,
                )

    def _toggle_command_text(self, index: int) -> None:
        key = id(self.commands[index])
        if key in self.expanded_text_commands:
            self.expanded_text_commands.remove(key)
        else:
            self.expanded_text_commands.add(key)
        self._render_simple_view()

    def _toggle_word_wrap(self) -> None:
        enabled = not self.advanced_editor.line_wrapping
        self.advanced_editor.set_line_wrapping(enabled)
        self.wrap_button.props(f'color={"primary" if enabled else "grey-7"} aria-pressed={str(enabled).lower()}')

    def _copy_failed(self, event) -> None:
        ui.notify('Could not copy to the clipboard. Select the editor contents and press Ctrl+C.', type='warning')

    def _paste_text(self, event) -> None:
        """Replace the draft with text read by the browser's click handler."""
        if self.busy or self.current_view != 'text':
            return
        if event.args.get('error') == 'unavailable':
            ui.notify('Clipboard access is unavailable. Paste directly into the editor.', type='warning')
        elif event.args.get('error'):
            ui.notify('Clipboard read was cancelled or blocked. In Firefox, choose Paste in the browser prompt, or paste directly into the editor.', type='warning')
        elif isinstance(event.args.get('text'), str):
            self.advanced_editor.set_value(event.args['text'])

    def _update_count(self) -> None:
        count = len(self.commands)
        noun = 'command' if count == 1 else 'commands'
        self.count_badge.set_text(f'{count} {noun}')

    def _update_execute_button(self) -> None:
        ready = (
            not self.busy
            and self.on_execute is not None
            and bool(self.commands)
            and len(self.executions) == len(self.commands)
            and all(execution.status in EXECUTABLE_STATUSES for execution in self.executions)
        )
        if ready:
            self.execute_button.enable()
        else:
            self.execute_button.disable()

    def _view_changed(self, event) -> None:
        requested_view = event.value
        if requested_view == self.current_view:
            return
        if requested_view == 'text':
            self._enter_text_view()
            return
        if requested_view == 'list':
            if self._text_is_dirty():
                self.view_toggle.set_value('text')
                self._open_discard_dialog()
            else:
                self._show_list_view()

    def _advanced_changed(self, event) -> None:
        self.advanced_draft = event.value
        self._update_text_action_buttons()

    def _enter_text_view(self) -> None:
        self._regenerate_advanced_draft()
        self.stored_text = self.advanced_draft
        self.current_view = 'text'
        self.simple_view.set_visibility(False)
        self.advanced_view.set_visibility(True)
        self.execute_button.set_visibility(False)
        self.text_actions.set_visibility(True)
        self._update_text_action_buttons()
        if self.on_text_mode_change:
            self.on_text_mode_change(True)

    def _show_list_view(self) -> None:
        self.current_view = 'list'
        self.view_toggle.set_value('list')
        self.simple_view.set_visibility(True)
        self.advanced_view.set_visibility(False)
        self.execute_button.set_visibility(True)
        self.text_actions.set_visibility(False)
        self._update_execute_button()
        if self.on_text_mode_change:
            self.on_text_mode_change(False)

    def _text_is_dirty(self) -> bool:
        return self.advanced_draft != self.stored_text

    def _update_text_action_buttons(self) -> None:
        enabled = self.current_view == 'text' and self._text_is_dirty() and not self.busy
        for button in (self.apply_button, self.discard_button):
            (button.enable if enabled else button.disable)()

    def _open_discard_dialog(self) -> None:
        if self._text_is_dirty():
            self.discard_dialog.open()

    def _discard_text(self) -> None:
        self.discard_dialog.close()
        self.advanced_draft = self.stored_text
        self.advanced_editor.set_value(self.stored_text)
        self._show_list_view()

    def _apply_text(self) -> None:
        """Atomically replace the command list with valid edited text."""
        try:
            commands = parse_command_text(self.advanced_draft)
        except CommandTextError as error:
            self.parse_error_location.set_text(
                f'Line {error.line}, column {error.column}'
            )
            self.parse_error_message.set_text(error.message)
            self.parse_error_source.set_text(error.source_line or '(end of text)')
            self.parse_error_dialog.open()
            return

        self.commands = commands
        self.selected_index = None
        self.selected_iteration = None
        self.expanded_commands.clear()
        self.expanded_text_commands.clear()
        self._evaluate_commands()
        self._update_count()
        self._regenerate_advanced_draft()
        self.stored_text = self.advanced_draft
        self._show_list_view()

    def _discard_parse_error(self) -> None:
        self.parse_error_dialog.close()
        self._discard_text()

    def _regenerate_advanced_draft(self) -> None:
        """Replace Text-view content with the canonical List serialization."""
        self.advanced_draft = '\n'.join(command.to_syntax() for command in self.commands)
        self.advanced_editor.set_value(self.advanced_draft)

    def _notify_selection(self) -> None:
        if not self.on_selection_change:
            return
        execution = (
            self.executions[self.selected_index]
            if self.selected_index is not None and self.selected_index < len(self.executions)
            else None
        )
        if execution is not None and self.selected_iteration is not None:
            if self.selected_iteration < len(execution.iterations):
                execution = execution.iterations[self.selected_iteration]
            else:
                self.selected_iteration = None
        self.on_selection_change(self.selected_index, execution)

