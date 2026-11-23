from copy import deepcopy
import asyncio
from typing import Any, Protocol
from uuid import uuid4

from .immutable import command_parameters_snapshot
from .application_results import (
    AuthoredCommand,
)

from nicegui import ui

from .command_card import render_command_card
from .applier import Applier
from .command_execution import (
    CommandExecution, EXECUTABLE_STATUSES,
)
from .command_parser import CommandTextError, parse_command_text


class Command(Protocol):
    """An authored command; iterative commands expand before execution."""
    def to_syntax(self) -> str: ...


class CommandListManager:
    """Own ordered command objects and their editable text representation."""

    def __init__(
        self,
        *,
        on_selection_change=None,
        on_execute=None,
        on_commands_change=None,
        on_text_mode_change=None,
        resolve_schema=None,
        lookup_variable=None,
        form_service=None,
    ) -> None:
        self.form_service = form_service
        self._context_owner = object()
        self.applier = Applier(owner=self._context_owner)
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
        self._evaluation_task = None
        self.commands: list[Command] = []
        self.executions: list[CommandExecution] = []
        self.source_schema: dict[str, Any] | None = None
        self.initial_database_id = None
        self.selected_index: int | None = None
        self.selected_iteration: int | None = None
        self.expanded_commands: set[int] = set()
        self.expanded_text_commands: set[int] = set()
        self.advanced_draft = ''
        self.stored_text = ''
        self.on_selection_change = on_selection_change
        self.on_commands_change = on_commands_change
        self.on_execute = on_execute
        self.on_text_mode_change = on_text_mode_change
        self.busy = False
        self.current_view = 'list'

        with ui.row().classes('w-full items-center gap-3'):
            ui.label('Command List').classes('text-lg font-medium')
            self.view_toggle = ui.toggle(
                {'list': 'List', 'text': 'Text'},
                value='list',
                on_change=self._view_changed,
            )
            self.count_badge = ui.badge('0 commands', color='grey').classes('ml-auto')
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
            with ui.row().classes('w-full items-center justify-between'):
                self.step_checkbox = ui.checkbox('Execute step by step').props('dense')
                self.clear_button = ui.button('Clear', icon='delete_sweep', on_click=self.clear_commands).props('flat dense')
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


    workspace = property(lambda self: self.applier.workspace, lambda self, value: setattr(self.applier, 'workspace', value))

    related_schemas = property(lambda self: self.applier.related_schemas, lambda self, value: setattr(self.applier, 'related_schemas', value))

    database_names = property(lambda self: self.applier.database_names, lambda self, value: setattr(self.applier, 'database_names', value))

    reference_defaults = property(lambda self: self.applier.reference_defaults, lambda self, value: setattr(self.applier, 'reference_defaults', value))

    resolution_cache = property(lambda self: self.applier.resolution_cache, lambda self, value: setattr(self.applier, 'resolution_cache', value))

    snapshot_builder = property(lambda self: self.applier.snapshot_builder, lambda self, value: setattr(self.applier, 'snapshot_builder', value))

    executions = property(lambda self: self.applier.executions, lambda self, value: setattr(self.applier, 'executions', value))

    def add_command(self, command: Command) -> None:
        self.commands.append(command)
        self._regenerate_advanced_draft()
        self._evaluate_commands()
        self._update_count()

    def replace_commands(self, commands) -> None:
        """Publish a fully generated list and apply it once through the normal path."""
        if self.busy:
            return
        self.commands = list(commands)
        self.selected_index = None
        self.selected_iteration = None
        self.expanded_commands.clear()
        self.expanded_text_commands.clear()
        self._regenerate_advanced_draft()
        self.stored_text = self.advanced_draft
        self._evaluate_commands()
        self._update_count()

    def clear_commands(self) -> None:
        if self.busy:
            return
        self.commands.clear()
        self.selected_index = None
        self.selected_iteration = None
        self.expanded_commands.clear()
        self.expanded_text_commands.clear()
        self._regenerate_advanced_draft()
        self.stored_text = self.advanced_draft
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


    def target_schema(self) -> dict[str, Any] | None:
        if not self.executions or not all(
            execution.status in EXECUTABLE_STATUSES for execution in self.executions
        ):
            return None
        return self.workspace.active_schema

    def target_forms(self):
        return tuple(self.results[-1].display_state.form_payloads().values()) if self.target_schema() is not None else None


    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for control in (self.view_toggle, self.advanced_editor, self.paste_button, self.step_checkbox, self.clear_button):
            (control.disable if busy else control.enable)()
        self._render_simple_view()
        self._update_execute_button()
        self._update_text_action_buttons()

    def _evaluate_commands(self) -> None:
        if self._evaluation_task is not None:
            self.applier.cancel()
            self._evaluation_task.cancel()
            self._evaluation_task = None
        self._evaluation_version += 1
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
        engine = Applier(self.source_schema, tuple(self.commands), authored_commands=self.authored_commands,
            related_schemas=self._base_related, database_names=self.database_names,
            reference_defaults=self.reference_defaults, resolution_cache=self.resolution_cache,
            resolve_schema=self.resolve_schema, lookup_variable=self.lookup_variable,
            form_service=self.form_service, generation=self._evaluation_version,
            owner=self._context_owner, on_progress=self._evaluation_updated,
            initial_database_id=self.initial_database_id)
        self.applier = engine
        if self.resolve_schema is not None:
            self._publish_evaluation()
            self._evaluation_task = asyncio.create_task(engine.evaluate())
        else:
            engine.evaluate_local()

    def _evaluation_updated(self, engine):
        if engine is self.applier:
            self._publish_evaluation()

    @property
    def candidate_plan(self):
        return self.applier.candidate_plan

    @property
    def results(self):
        return self.applier.results

    def enrich_current_display(self, snapshot_id, generation, labels):
        return self.applier.enrich_current_display(snapshot_id, generation, labels)

    def _publish_evaluation(self):
        self._render_simple_view()
        self._notify_selection()
        self._update_execute_button()


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
        self.count_badge.set_visibility(False)
        self.clear_button.set_visibility(False)
        self.step_checkbox.set_visibility(False)
        self.simple_view.set_visibility(False)
        self.advanced_view.set_visibility(True)
        self.execute_button.set_visibility(False)
        self.text_actions.set_visibility(True)
        self._update_text_action_buttons()
        if self.on_text_mode_change:
            self.on_text_mode_change(True)

    def _show_list_view(self) -> None:
        self.current_view = 'list'
        self.count_badge.set_visibility(True)
        self.clear_button.set_visibility(True)
        self.step_checkbox.set_visibility(True)
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
        enabled = self.current_view == 'text' and not self.busy
        for button in (self.apply_button, self.discard_button):
            (button.enable if enabled else button.disable)()

    def _open_discard_dialog(self) -> None:
        if self._text_is_dirty():
            self.discard_dialog.open()
        else:
            self._show_list_view()

    def _discard_text(self) -> None:
        self.discard_dialog.close()
        self.advanced_draft = self.stored_text
        self.advanced_editor.set_value(self.stored_text)
        self._show_list_view()

    def _apply_text(self) -> None:
        """Atomically replace the command list with valid edited text."""
        if not self._text_is_dirty():
            self._show_list_view()
            return
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
        if self.on_commands_change:
            self.on_commands_change()

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

