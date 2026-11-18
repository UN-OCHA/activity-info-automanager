from nicegui import ui

import json
import asyncio
from copy import deepcopy
from dataclasses import replace

from nicegui import context

from .api import DEFAULT_BASE_URL, ActivityInfoService
from .command_creator import CommandCreatorRenderer, field_codes_from_schema
from .schema_names import readable_schema, load_default_record_labels
from .schema_display import underline_substitutions
from .schema_resolution import resolve_command_schema
from .command_list import CommandExecution, CommandListManager
from .commands import ADD_FIELD, DELETE_FIELD, MOVE_FIELD, MODIFY_FIELD
from .listbox import DatabaseListbox
from .schema_change import apply_form_schema_change


def connection_page():
    ui.add_css('''
        .nicegui-content {
            padding: 0 !important;
        }
        .workbench {
            box-sizing: border-box;
            display: grid;
            grid-template-columns: minmax(260px, 300px) minmax(0, 1fr) minmax(320px, 380px);
            grid-template-rows: auto minmax(360px, 1fr) minmax(140px, 22vh);
            gap: 0.75rem;
            min-height: 720px;
            height: 100vh;
            padding: 0.75rem;
            width: 100%;
        }
        .workbench-panel {
            min-height: 0;
            min-width: 0;
            overflow: hidden;
        }
        .sources-panel {
            grid-column: 1;
            grid-row: 1 / 4;
            overflow-y: auto;
        }
        .command-creator-panel {
            grid-column: 2 / 4;
            grid-row: 1;
            max-height: 45vh;
            overflow-y: auto;
        }
        .result-panel {
            grid-column: 2;
            grid-row: 2;
        }
        .command-list-panel {
            grid-column: 3;
            grid-row: 2;
        }
        .command-list-panel > :not(.command-list-body):not(.command-text-body) {
            flex-shrink: 0;
        }
        .command-list-body, .command-text-body {
            flex: 1 1 0;
            min-height: 0;
            flex-wrap: nowrap;
        }
        .command-list-body {
            overflow-y: auto;
        }
        .command-text-body {
            overflow: hidden;
        }
        .command-text-editor,
        .command-text-editor .q-field__inner,
        .command-text-editor .q-field__control,
        .command-text-editor .q-field__control-container {
            height: 100%;
            min-height: 0;
        }
        .command-text-editor textarea.q-field__native {
            height: 100%;
            min-height: 0;
            resize: none;
            overflow-y: auto;
        }
        .console-panel {
            grid-column: 2 / 4;
            grid-row: 3;
        }
        .panel-body {
            min-height: 0;
            overflow: auto;
        }
        .result-body {
            flex: 1 1 0;
            min-height: 0;
            overflow: hidden;
            position: relative;
            width: 100%;
        }
        .result-placeholder {
            left: 50%;
            position: absolute;
            top: 50%;
            transform: translate(-50%, -50%);
        }
        .result-code {
            height: 100%;
            margin: 0;
            min-height: 0;
            width: 100%;
        }
        .result-code u { text-decoration: underline; text-underline-offset: 3px; }
        .result-code mark {
            color: inherit;
            background-color: #fef3c7;
            text-decoration: underline wavy;
            text-underline-offset: 3px;
        }
        @media (max-width: 1100px) {
            .workbench {
                grid-template-columns: minmax(240px, 280px) minmax(0, 1fr);
                grid-template-rows: auto minmax(420px, 1fr) auto minmax(140px, 22vh);
                height: auto;
                min-height: 100vh;
            }
            .sources-panel { grid-column: 1; grid-row: 1 / 5; }
            .command-creator-panel { grid-column: 2; grid-row: 1; }
            .result-panel { grid-column: 2; grid-row: 2; }
            .command-list-panel {
                grid-column: 2;
                grid-row: 3;
                height: max(360px, 50vh);
            }
            .console-panel { grid-column: 2; grid-row: 4; }
        }
        @media (max-width: 700px) {
            .workbench {
                display: flex;
                flex-direction: column;
            }
            .workbench-panel { min-height: auto; }
            .result-panel { min-height: 60vh; }
            .console-panel { min-height: 10rem; }
        }
    ''')

    service: ActivityInfoService | None = None
    source_schema: dict | None = None
    pending_execution: dict | None = None
    text_mode = False
    form_request = 0
    schema_request = 0
    async def refresh_reference_suggestions(creator):
        control = creator.controls.get('referenced_form')
        reference = control.value if control is not None else None
        data = None
        if reference and source_schema:
            data = source_schema if reference == source_schema.get('id') else command_list.related_schemas.get(reference)
            if data is None:
                matches = []
                for identity, candidate in command_list.related_schemas.items():
                    db_id = candidate.get('databaseId')
                    name = candidate.get('label')
                    qualified = f'{command_list.database_names.get(db_id)}/{name}'
                    if (reference == name and db_id == source_schema.get('databaseId')) or reference == qualified:
                        matches.append(candidate)
                if len(matches) == 1:
                    data = matches[0]
        creator.set_suggestions('reference_field_codes', field_codes_from_schema(data))

    async def close_service():
        nonlocal service
        current, service = service, None
        if current:
            await current.aclose()

    displayed_result = None
    displayed_names = None
    record_cache = {}
    result_request = 0
    displayed_issues = ()

    async def resolve_preview(data):
        if service is None:
            raise ValueError('Connect before resolving command names.')
        return await resolve_command_schema(service, data, command_list.related_schemas,
            command_list.database_names, command_list.reference_defaults, command_list.resolution_cache)

    def show_result(data: dict | None, empty_message='No result', names=None, issues=()):
        nonlocal displayed_result, displayed_names, result_request, displayed_issues
        displayed_issues = issues
        result_request += 1
        displayed_result = data
        displayed_names = names if names is not None else (
            readable_schema(data, command_list.related_schemas, command_list.database_names)
            if data is not None else None)
        render_result(empty_message)
        if names is None and data is not None and service is not None and any(
            field.get('type') in ('reference', 'multiselectreference') and field.get('defaultValue')
            for field in data.get('elements', [])
        ):
            asyncio.create_task(refresh_record_names(data, service, result_request, schema_request))

    async def refresh_record_names(data, current_service, request, source_request):
        nonlocal displayed_names
        # A cache belongs to one selected form/connection; stale reads cannot
        # update its successor, even when both reference the same record ID.
        cache = record_cache
        labels = await load_default_record_labels(
            current_service, data, command_list.related_schemas, cache)
        if request != result_request or source_request != schema_request or service is not current_service:
            return
        displayed_names = readable_schema(data, command_list.related_schemas,
                                         command_list.database_names, record_labels=labels)
        for index, execution in enumerate(command_list.executions):
            if execution.schema is data:
                command_list.executions[index] = replace(execution, names_schema=displayed_names)
        render_result()

    def render_result(empty_message='No result'):
        unresolved_badge.set_text(f'Unresolved: {len(displayed_issues)}')
        unresolved_badge.set_visibility(bool(displayed_issues))
        data = displayed_names if result_toggle.value == 'names' else displayed_result
        if data is None:
            schema.set_content('')
            schema.set_visibility(False)
            schema_placeholder.set_text(empty_message)
            schema_placeholder.set_visibility(True)
            return
        showing_names = result_toggle.value == 'names'
        # Use Markdown's post-render hook to retain the original code control,
        # highlighting, fonts, scrolling, copy action, and client sanitization.
        schema.markdown._sanitize = (
            (lambda html: underline_substitutions(html, data,
                [issue.path for issue in displayed_issues], underline=showing_names))
            if showing_names or displayed_issues else True)
        schema.set_content(json.dumps(data, indent=2, ensure_ascii=False))
        schema.markdown._handle_content_change(schema._render_markdown())
        schema.set_visibility(True)
        schema_placeholder.set_visibility(False)

    def selection_changed(
        selected_index: int | None,
        execution: CommandExecution | None,
    ) -> None:
        if selected_index is None:
            # Background evaluation still refreshes the source display, but its
            # per-command messages belong to executions until a command is selected.
            show_result(
                source_schema,
                'Select a form to load its schema.',
            )
            return
        if execution is None:
            console_message.set_text('Did not execute.')
            show_result(None)
            return
        console_message.set_text(execution.message)
        show_result(execution.schema, names=execution.names_schema, issues=execution.issues)

    async def choose_recovery(diff_summary: str):
        with ui.dialog() as dialog, ui.card().classes('w-full max-w-3xl'):
            dialog.props('persistent')
            ui.label('Form schema application did not fully succeed.').classes(
                'text-lg font-medium text-red-700'
            )
            ui.label('Roll back changes?')
            ui.label('Differences from the expected schema:')
            ui.label(diff_summary).classes(
                'w-full max-h-80 overflow-y-auto whitespace-pre-wrap break-all '
                'font-mono text-sm bg-slate-100 rounded p-2'
            )
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Leave', on_click=lambda: dialog.submit('leave')).props('flat')
                ui.button(
                    'Roll Back', on_click=lambda: dialog.submit('rollback'),
                ).props('color=negative')
        return await dialog

    def open_execute_confirmation():
        nonlocal pending_execution
        target_schema = command_list.target_schema()
        if (
            target_schema is None
            or source_schema is None
            or service is None
            or not forms.value
        ):
            return
        pending_execution = {
            'service': service,
            'form_id': forms.value,
            'loaded_schema': deepcopy(source_schema),
            'target_schema': target_schema,
            'related_schemas': deepcopy(command_list.related_schemas),
            'reference_defaults': deepcopy(command_list.reference_defaults),
        }
        database_name = databases.selected_label or databases.value or 'the selected database'
        form_name = forms.selected_label or forms.value
        confirmation_message.set_text(
            f'This will change the form schema of {database_name}, {form_name}.'
        )
        confirmation_dialog.open()

    def cancel_execute_confirmation():
        nonlocal pending_execution
        pending_execution = None
        confirmation_dialog.close()

    def set_write_busy(busy: bool) -> None:
        for control in (base_url, token, button):
            (control.disable if busy else control.enable)()
        if busy:
            databases.disable()
            forms.disable()
        else:
            (databases.enable if databases.options else databases.disable)()
            (forms.enable if forms.options else forms.disable)()
        command_creator.set_enabled(not busy and not text_mode)
        command_list.set_busy(busy)

    def text_mode_changed(editing: bool) -> None:
        nonlocal text_mode
        text_mode = editing
        command_creator.set_enabled(not editing and not command_list.busy)

    async def execute_confirmed():
        nonlocal pending_execution, source_schema
        confirmation_dialog.close()
        execution = pending_execution
        if execution is None:
            return
        set_write_busy(True)
        outcome = None
        unexpected_message = None
        try:
            outcome = await apply_form_schema_change(
                execution['service'],
                execution['form_id'],
                execution['loaded_schema'],
                execution['target_schema'],
                choose_recovery,
                resolved=True,
                related_schemas=execution['related_schemas'],
                reference_defaults=execution['reference_defaults'],
            )
            if outcome.actual_schema is not None:
                source_schema = deepcopy(outcome.actual_schema)
                command_creator.set_schema(source_schema)
                command_list.set_schema(source_schema)
        except Exception as exc:
            unexpected_message = f'Unexpected schema change error: {exc}'
        finally:
            command_list.clear_selection()
            if outcome is not None:
                console_message.set_text('\n'.join(outcome.messages))
                show_result(outcome.actual_schema)
            elif unexpected_message is not None:
                console_message.set_text(unexpected_message)
                show_result(None)
            pending_execution = None
            set_write_busy(False)

    def clear_schema():
        nonlocal schema_request, source_schema, record_cache
        record_cache = {}
        schema_request += 1
        source_schema = None
        command_creator.set_schema(None)
        command_list.related_schemas = {}
        command_list.database_names = {}
        command_list.reference_defaults = {}
        command_list.resolution_cache = {}
        command_list.set_schema(None)
        schema_status.set_text('')

    async def load_schema():
        nonlocal source_schema
        clear_schema()
        request = schema_request
        if not forms.value or not service:
            return
        selected_form_id = forms.value
        selected_service = service
        schema_status.set_text('Loading schema…')
        try:
            related = await selected_service.get_form_tree(selected_form_id)
            data = related.get(selected_form_id)
            if not isinstance(data, dict) or not isinstance(data.get('elements'), list):
                raise ValueError('The form tree did not contain the selected form schema.')
            if request != schema_request:
                return
            db_names = {}
            warning = ''
            try:
                db_names = await selected_service.get_database_names()
            except ValueError as exc:
                warning = f'Some names could not be loaded: {exc}'
            if request == schema_request:
                command_list.related_schemas = related
                command_list.database_names = db_names
                source_schema = data
                command_creator.set_schema(data)
                command_list.set_schema(data)
                schema_status.set_text(warning)
        except ValueError as exc:
            if request == schema_request:
                schema_status.set_text(str(exc))

    def clear_forms():
        nonlocal form_request
        form_request += 1
        clear_schema()
        forms.set_options({})
        command_creator.set_suggestions('form_names', [])
        form_status.set_text('')

    async def load_forms():
        clear_forms()
        request = form_request
        if not databases.value or not service:
            return
        form_status.set_text('Loading forms…')
        try:
            options = await service.get_forms(databases.value)
            if request == form_request:
                forms.set_options(options)
                command_creator.set_suggestions('form_names', [
                    label.removesuffix(f' ({identity})') for identity, label in options.items()
                ])
                form_status.set_text('' if options else 'No forms available.')
        except ValueError as exc:
            if request == form_request:
                form_status.set_text('')
                console_message.set_text(f'Form list error:\n{exc}')

    def clear_databases():
        clear_forms()
        databases.set_options({})
        status.set_text('Connect to load databases.')

    async def credentials_changed():
        clear_databases()
        await close_service()

    async def connect():
        nonlocal service
        clear_databases()
        for control in (base_url, token, button):
            control.disable()
        status.set_text('Loading databases…')
        try:
            await close_service()
            service = ActivityInfoService(base_url.value, token.value)
            options = await service.get_databases()
            databases.set_options(options)
            status.set_text(f'{len(options)} databases loaded.' if options else 'No databases available.')
        except ValueError as exc:
            await close_service()
            status.set_text('')
            console_message.set_text(f'Database list error:\n{exc}')
        finally:
            for control in (base_url, token, button):
                control.enable()

    def add_command(command):
        command_list.add_command(command)

    with ui.element('main').classes('workbench'):
        with ui.card().classes('workbench-panel sources-panel w-full no-shadow'):
            ui.label('Sources').classes('text-xl font-medium')
            ui.separator()
            with ui.expansion('Connection', icon='link', value=True).classes('w-full'):
                base_url = ui.input(
                    'Base URL', value=DEFAULT_BASE_URL, on_change=credentials_changed,
                ).classes('w-full')
                token = ui.input(
                    'API token', password=True, password_toggle_button=True,
                    on_change=credentials_changed,
                ).classes('w-full')
                button = ui.button('Connect', on_click=connect).classes('w-full')
            ui.separator()
            with ui.column().classes('w-full gap-2'):
                ui.label('Databases').classes('text-base font-medium')
                databases = DatabaseListbox(on_change=load_forms)
                status = ui.label('Connect to load databases.').classes('text-sm text-slate-600')
            ui.separator()
            with ui.column().classes('w-full gap-2'):
                ui.label('Forms').classes('text-base font-medium')
                forms = DatabaseListbox(
                    label='Forms', placeholder='Select a form', on_change=load_schema,
                )
                form_status = ui.label('').classes('text-sm text-slate-600')

        with ui.card().classes('workbench-panel command-creator-panel w-full no-shadow'):
            ui.label('Command Creator').classes('text-lg font-medium')
            command_creator = CommandCreatorRenderer(
                (MOVE_FIELD, DELETE_FIELD, ADD_FIELD, MODIFY_FIELD), on_add=add_command,
                on_parameter_change=refresh_reference_suggestions,
            )

        with ui.card().classes('workbench-panel result-panel w-full no-shadow flex flex-col'):
            with ui.row().classes('w-full items-center gap-3'):
                ui.label('Result').classes('text-lg font-medium')
                result_toggle = ui.toggle(
                    {'ids': 'IDs', 'names': 'Names'}, value='ids',
                    on_change=lambda: render_result(),
                )
                unresolved_badge = ui.badge('Unresolved: 0', color='amber-2', text_color='amber-10')
                unresolved_badge.props['aria-label'] = 'Unresolved names'
                unresolved_badge.set_visibility(False)
            ui.separator()
            schema_status = ui.label('').classes('text-sm text-red-700')
            with ui.element('div').classes('result-body'):
                schema_placeholder = ui.label(
                    'Select a form to load its schema.'
                ).classes('result-placeholder text-sm text-slate-500')
                schema = ui.code('', language='json').classes('result-code')
                schema.props['aria-label'] = 'Form schema'
                schema.set_visibility(False)

        with ui.card().classes('workbench-panel command-list-panel w-full no-shadow flex flex-col'):
            ui.label('Command List').classes('text-lg font-medium')
            command_list = CommandListManager(
                resolve_schema=resolve_preview,
                on_selection_change=selection_changed,
                on_execute=open_execute_confirmation,
                on_text_mode_change=text_mode_changed,
            )

        with ui.card().classes('workbench-panel console-panel w-full no-shadow flex flex-col'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label('Console').classes('text-lg font-medium')
                ui.button('Clear', icon='delete_sweep').props('flat dense disable')
            ui.separator()
            with ui.column().classes('panel-body w-full grow'):
                console_message = ui.label('No messages.').classes(
                    'whitespace-pre-wrap font-mono text-sm text-slate-500'
                )

    with ui.dialog() as confirmation_dialog, ui.card().classes('w-full max-w-lg'):
        confirmation_dialog.props('persistent')
        confirmation_message = ui.label().classes('text-base')
        with ui.row().classes('w-full justify-end gap-2'):
            ui.button('Cancel', on_click=cancel_execute_confirmation).props('flat')
            ui.button('OK', on_click=execute_confirmed).props('color=primary')

    context.client.on_delete(close_service)
