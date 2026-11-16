from nicegui import ui

import json

from nicegui import context

from .api import DEFAULT_BASE_URL, ActivityInfoService
from .listbox import DatabaseListbox


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
        }
        .result-panel {
            grid-column: 2;
            grid-row: 2;
        }
        .command-list-panel {
            grid-column: 3;
            grid-row: 2;
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
            .command-list-panel { grid-column: 2; grid-row: 3; }
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
    form_request = 0
    schema_request = 0

    async def close_service():
        nonlocal service
        current, service = service, None
        if current:
            await current.aclose()

    def clear_schema():
        nonlocal schema_request
        schema_request += 1
        schema.set_content('')
        schema.set_visibility(False)
        schema_placeholder.set_visibility(True)
        schema_status.set_text('')

    async def load_schema():
        clear_schema()
        request = schema_request
        if not forms.value or not service:
            return
        schema_status.set_text('Loading schema…')
        try:
            data = await service.get_form_schema(forms.value)
            if request == schema_request:
                schema.set_content(json.dumps(data, indent=2, ensure_ascii=False))
                schema.set_visibility(True)
                schema_placeholder.set_visibility(False)
                schema_status.set_text('')
        except ValueError as exc:
            if request == schema_request:
                schema_status.set_text(str(exc))

    def clear_forms():
        nonlocal form_request
        form_request += 1
        clear_schema()
        forms.set_options({})
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
                form_status.set_text('' if options else 'No forms available.')
        except ValueError as exc:
            if request == form_request:
                form_status.set_text(str(exc))

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
            status.set_text(str(exc))
        finally:
            for control in (base_url, token, button):
                control.enable()

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
            with ui.row().classes('w-full items-end gap-3'):
                ui.select(
                    ['Select a command'], value='Select a command', label='Command',
                ).props('disable').classes('w-64')
                ui.input('Parameters will appear here').props('disable').classes('grow')
                ui.button('Add command', icon='add').props('disable')

        with ui.card().classes('workbench-panel result-panel w-full no-shadow flex flex-col'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label('Result').classes('text-lg font-medium')
                ui.label('JSON').classes('text-xs text-slate-500 uppercase')
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
            with ui.row().classes('w-full items-center justify-between'):
                ui.label('Command List').classes('text-lg font-medium')
                ui.badge('0 commands', color='grey')
            ui.separator()
            with ui.column().classes('panel-body w-full grow items-center justify-center gap-2'):
                ui.icon('format_list_numbered', size='2rem').classes('text-slate-400')
                ui.label('No commands added yet.').classes('text-sm text-slate-500')
                ui.label('Commands will be reorderable here.').classes('text-xs text-slate-400')

        with ui.card().classes('workbench-panel console-panel w-full no-shadow flex flex-col'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label('Console').classes('text-lg font-medium')
                ui.button('Clear', icon='delete_sweep').props('flat dense disable')
            ui.separator()
            with ui.column().classes('panel-body w-full grow'):
                ui.label('No messages.').classes('font-mono text-sm text-slate-500')

    context.client.on_delete(close_service)
