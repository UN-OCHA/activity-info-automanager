"""Display command cards and iteration rows; state and actions belong to the list."""

import ast
from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING

from nicegui import ui

from .command_iteration import IterativeCommand

if TYPE_CHECKING:
    from .command_list import Command, CommandExecution


def render_command_card(
    command: 'Command',
    execution: 'CommandExecution',
    *,
    index: int,
    command_count: int,
    selected_index: int | None,
    selected_iteration: int | None,
    expanded: bool,
    busy: bool,
    text_expanded: bool,
    on_toggle_text: Callable[[int], None],
    on_select: Callable[[int], None],
    on_select_iteration: Callable[[int, int], None],
    on_toggle_iterations: Callable[[int], None],
    on_move: Callable[[int, int], None],
    on_delete: Callable[[int], None],
) -> None:
    """Render expanded iterations above their command without owning list state."""
    if expanded:
        with ui.column().classes('w-full gap-1 pl-5 border-l-2 border-slate-300'):
            for iteration, child in enumerate(execution.iterations):
                selected_child = index == selected_index and iteration == selected_iteration
                included_child = selected_index is not None and (index < selected_index or (
                    index == selected_index and (selected_iteration is None or iteration < selected_iteration)))
                shade = 'bg-blue-200 border-blue-500' if selected_child else (
                    'bg-blue-100 border-blue-300' if included_child else 'bg-slate-50 border-slate-200')
                with ui.row().classes(
                    f'w-full cursor-pointer items-center gap-2 rounded border px-2 py-1 {shade}'
                ).on('click', lambda _, i=index, n=iteration: on_select_iteration(i, n)):
                    _status_icon(child.status)
                    ui.label(f'Iteration {iteration + 1} ({child.application_label})').classes('text-xs font-mono break-all')
    selected = index == selected_index and selected_iteration is None
    included = selected_index is not None and index < selected_index
    background = (
        'bg-blue-200 border-blue-500'
        if selected
        else 'bg-blue-100 border-blue-300'
        if included
        else 'bg-slate-50 border-slate-200'
    )
    with ui.column().classes(
        f'w-full min-w-0 cursor-pointer gap-1 rounded border p-2 {background}'
    ).on('click', lambda _, i=index: on_select(i)):
        with ui.row().classes('w-full items-center justify-between gap-2 flex-nowrap'):
            with ui.row().classes('items-center gap-2 flex-wrap'):
                if isinstance(command, IterativeCommand) or execution.form_iterations:
                    count = len(execution.iterations)
                    ui.badge(f'{count}x', color='grey').classes('rounded-full text-xs')
                    counts = Counter(
                        'not_executed' if child.status == 'resolving' else child.status
                        for child in execution.iterations
                    )
                    for status in ('applied', 'warning', 'error', 'not_executed'):
                        if counts[status]:
                            with ui.row().classes('items-center gap-1 flex-nowrap'):
                                _status_icon(status)
                                ui.label(str(counts[status])).classes('text-xs')
                    if execution.status in ('warning', 'error') and not counts[execution.status]:
                        _status_icon(execution.status)
                else:
                    _status_icon(execution.status)
            if isinstance(command, IterativeCommand) or execution.form_iterations:
                with ui.row().classes('items-center shrink-0 gap-0') as iteration_action:
                    _stop_click(iteration_action)
                    toggle = ui.button(
                        on_click=lambda i=index: on_toggle_iterations(i),
                    ).props('flat dense color=grey-7').classes('text-xs')
                    toggle.props['aria-label'] = 'Hide iterations' if expanded else 'Show iterations'
                    with toggle:
                        ui.label('Hide' if expanded else 'Show').classes('text-xs normal-case')
                        ui.icon('keyboard_arrow_down' if expanded else 'keyboard_arrow_up')
                    if not count:
                        toggle.disable()

        syntax = command.to_syntax()
        preview, can_expand = command_preview(syntax)
        with ui.row().classes('w-full items-start gap-1 flex-nowrap'):
            with ui.row().classes('shrink-0 gap-0') as text_action:
                _stop_click(text_action)
                toggle = _command_button(
                    'arrow_drop_down' if text_expanded and can_expand else 'arrow_right',
                    'Collapse command text' if text_expanded else 'Expand command text',
                    lambda i=index: on_toggle_text(i),
                )
                if not can_expand:
                    toggle.disable()
            ui.label(syntax if text_expanded else preview).classes(
                'min-w-0 flex-1 font-mono text-sm whitespace-pre-wrap'
            ).style('overflow-wrap: anywhere')

        with ui.row().classes('w-full items-center gap-0'):
            up = _command_button(
                'keyboard_arrow_up', 'Move command up',
                lambda i=index: on_move(i, i - 1),
            )
            down = _command_button(
                'keyboard_arrow_down', 'Move command down',
                lambda i=index: on_move(i, i + 1),
            )
            delete = _command_button(
                'delete_outline', 'Delete command', lambda i=index: on_delete(i),
            ).classes('text-red-600')
            if index == 0:
                up.disable()
            if index == command_count - 1:
                down.disable()
            if busy:
                for button in (up, down, delete):
                    button.disable()


def command_preview(syntax: str) -> tuple[str, bool]:
    """Shorten at an AST argument boundary, preserving commas inside values."""
    call = ast.parse(syntax, mode='eval').body
    arguments = sorted([*call.args, *call.keywords], key=lambda item: (item.lineno, item.col_offset))
    if len(arguments) <= 2:
        return syntax, False
    preview = ', '.join(ast.get_source_segment(syntax, argument) for argument in arguments[:2])
    return f'{syntax.split("(", 1)[0]}({preview}, ...', True


def _stop_click(element):
    element.on('click', js_handler='(event) => event.stopPropagation()')


def _status_icon(status):
    icon, color = {
        'applied': ('check_circle', 'text-green-600'),
        'error': ('error', 'text-red-600'),
        'warning': ('warning', 'text-yellow-600'),
        'resolving': ('hourglass_top', 'text-blue-600'),
        'not_executed': ('fiber_manual_record', 'text-slate-400'),
    }[status]
    return ui.icon(icon).classes(color)

def _command_button(icon: str, label: str, handler):
    button = ui.button(
        icon=icon, on_click=handler,
    ).props('flat dense round color=grey-7').classes('w-8 h-8 shrink-0')
    _stop_click(button)
    button.props['aria-label'] = label
    button.tooltip(label)
    return button

