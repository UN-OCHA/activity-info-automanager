from nicegui import ui

import json

from nicegui import context

from .api import DEFAULT_BASE_URL, ActivityInfoService
from .listbox import DatabaseListbox


def connection_page():
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

    with ui.column().classes('w-full max-w-xl mx-auto'):
        with ui.card().classes('w-full'):
            ui.label('Connection').classes('text-lg')
            base_url = ui.input('Base URL', value=DEFAULT_BASE_URL,
                                on_change=credentials_changed).classes('w-full')
            token = ui.input('API token', password=True, password_toggle_button=True,
                             on_change=credentials_changed).classes('w-full')
            button = ui.button('Connect', on_click=connect)
        with ui.card().classes('w-full'):
            ui.label('Databases').classes('text-lg')
            databases = DatabaseListbox(on_change=load_forms)
            status = ui.label('Connect to load databases.')
        with ui.card().classes('w-full'):
            ui.label('Forms').classes('text-lg')
            forms = DatabaseListbox(label='Forms', placeholder='Select a form', on_change=load_schema)
            form_status = ui.label('')
            schema_status = ui.label('')
            schema = ui.code('', language='json').classes('w-full')
            schema.props['aria-label'] = 'Form schema'
            schema.set_visibility(False)

    context.client.on_delete(close_service)
