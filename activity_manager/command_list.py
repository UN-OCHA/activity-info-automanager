from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from nicegui import ui


class Command(Protocol):
    def to_syntax(self) -> str: ...

    def execute(self, schema: dict[str, Any]) -> dict[str, Any]: ...


ExecutionStatus = Literal['applied', 'error', 'not_executed']


@dataclass(frozen=True)
class CommandExecution:
    status: ExecutionStatus
    schema: dict[str, Any] | None
    message: str


class CommandListManager:
    """Own ordered command objects and their editable text representation."""

    def __init__(self, *, on_selection_change=None, on_execute=None) -> None:
        self.commands: list[Command] = []
        self.executions: list[CommandExecution] = []
        self.source_schema: dict[str, Any] | None = None
        self.selected_index: int | None = None
        self.advanced_draft = ''
        self.on_selection_change = on_selection_change
        self.on_execute = on_execute
        self.busy = False

        with ui.row().classes('w-full items-center justify-between gap-2'):
            self.view_toggle = ui.toggle(
                {'simple': 'Simple', 'advanced': 'Advanced'},
                value='simple',
                on_change=self._view_changed,
            )
            self.count_badge = ui.badge('0 commands', color='grey')
        ui.separator()

        self.simple_view = ui.column().classes('panel-body w-full grow gap-2')
        with self.simple_view:
            self.simple_content = ui.column().classes('w-full gap-2')

        self.advanced_view = ui.column().classes('panel-body w-full grow')
        with self.advanced_view:
            self.advanced_editor = ui.textarea(
                'Commands',
                placeholder='One command per line',
                on_change=self._advanced_changed,
            ).props('outlined rows=12').classes('w-full')

        self.execute_button = ui.button(
            'Execute', icon='play_arrow', on_click=on_execute,
        ).classes('w-full')
        self.execute_button.disable()

        self.advanced_view.set_visibility(False)
        self._render_simple_view()

    def add_command(self, command: Command) -> None:
        self.commands.append(command)
        self._regenerate_advanced_draft()
        self._evaluate_commands()
        self._update_count()

    def move_command(self, old_index: int, new_index: int) -> None:
        """Reorder discrete commands without changing the Advanced draft."""
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
        """Delete a discrete command without changing the Advanced draft."""
        if not 0 <= index < len(self.commands):
            raise IndexError('command index out of range')
        self.commands.pop(index)
        if self.selected_index == index:
            self.selected_index = None
        elif self.selected_index is not None and self.selected_index > index:
            self.selected_index -= 1
        self._regenerate_advanced_draft()
        self._evaluate_commands()
        self._update_count()

    def set_schema(self, schema: dict[str, Any] | None) -> None:
        self.source_schema = deepcopy(schema)
        self._evaluate_commands()

    def select_command(self, index: int) -> None:
        if not 0 <= index < len(self.commands):
            raise IndexError('command index out of range')
        self.selected_index = None if self.selected_index == index else index
        self._render_simple_view()
        self._notify_selection()

    def clear_selection(self) -> None:
        self.selected_index = None
        self._render_simple_view()
        self._notify_selection()

    def target_schema(self) -> dict[str, Any] | None:
        if not self.executions or not all(
            execution.status == 'applied' for execution in self.executions
        ):
            return None
        return deepcopy(self.executions[-1].schema)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for control in (self.view_toggle, self.advanced_editor):
            (control.disable if busy else control.enable)()
        self._render_simple_view()
        self._update_execute_button()

    def _evaluate_commands(self) -> None:
        self.executions.clear()
        current_schema = deepcopy(self.source_schema)
        stopped = current_schema is None

        for command in self.commands:
            if stopped:
                message = (
                    'Did not execute: no form schema selected.'
                    if self.source_schema is None
                    else 'Did not execute.'
                )
                self.executions.append(CommandExecution('not_executed', None, message))
                continue
            try:
                current_schema = command.execute(current_schema)
                self.executions.append(CommandExecution(
                    'applied',
                    deepcopy(current_schema),
                    'Command successfully applied to schema.',
                ))
            except Exception as exc:
                message = str(exc) or f'{type(exc).__name__} while executing command.'
                self.executions.append(CommandExecution('error', None, message))
                stopped = True

        self._render_simple_view()
        self._notify_selection()
        self._update_execute_button()

    def _render_simple_view(self) -> None:
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
                selected = index == self.selected_index
                included = self.selected_index is not None and index < self.selected_index
                background = (
                    'bg-blue-200 border-blue-500'
                    if selected
                    else 'bg-blue-100 border-blue-300'
                    if included
                    else 'bg-slate-50 border-slate-200'
                )
                with ui.row().classes(
                    f'w-full cursor-pointer items-center gap-2 rounded border p-2 {background}'
                ).on(
                    'click',
                    lambda _, selected_index=index: self.select_command(selected_index),
                ):
                    with ui.row().classes(
                        'min-w-0 grow items-center gap-2'
                    ):
                        icon, color = {
                            'applied': ('check_circle', 'text-green-600'),
                            'error': ('error', 'text-red-600'),
                            'not_executed': ('fiber_manual_record', 'text-slate-400'),
                        }[execution.status]
                        ui.icon(icon).classes(color)
                        ui.label(command.to_syntax()).classes(
                            'min-w-0 grow font-mono text-sm break-all'
                        )
                    with ui.row().classes('shrink-0 gap-0') as actions:
                        actions.on(
                            'click',
                            js_handler='(event) => event.stopPropagation()',
                        )
                        up = self._command_button(
                            'keyboard_arrow_up',
                            'Move command up',
                            lambda index=index: self.move_command(index, index - 1),
                        )
                        down = self._command_button(
                            'keyboard_arrow_down',
                            'Move command down',
                            lambda index=index: self.move_command(index, index + 1),
                        )
                        delete = self._command_button(
                            'delete_outline',
                            'Delete command',
                            lambda index=index: self.delete_command(index),
                        ).classes('text-red-600')
                        if index == 0:
                            up.disable()
                        if index == len(self.commands) - 1:
                            down.disable()
                        if self.busy:
                            for button in (up, down, delete):
                                button.disable()

    @staticmethod
    def _command_button(icon: str, label: str, handler):
        button = ui.button(
            icon=icon, on_click=handler,
        ).props('flat dense round color=grey-7')
        button.props['aria-label'] = label
        button.tooltip(label)
        return button

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
            and all(execution.status == 'applied' for execution in self.executions)
        )
        if ready:
            self.execute_button.enable()
        else:
            self.execute_button.disable()

    def _view_changed(self, event) -> None:
        advanced = event.value == 'advanced'
        self.simple_view.set_visibility(not advanced)
        self.advanced_view.set_visibility(advanced)

    def _advanced_changed(self, event) -> None:
        self.advanced_draft = event.value

    def _regenerate_advanced_draft(self) -> None:
        """Replace Advanced text with the canonical Simple-view serialization.

        Advanced edits are disposable until an explicit save-and-parse workflow
        is implemented; every discrete command-list mutation regenerates them.
        """
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
        self.on_selection_change(self.selected_index, execution)

