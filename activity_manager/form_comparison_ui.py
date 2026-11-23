"""Form comparison controls and field mapping dialog."""

from nicegui import ui

from .form_comparison import (ComparisonOptions, comparison_fields, field_caption,
                              generate_commands, mapping_choices)


class FormComparisonRenderer:
    def __init__(self, *, load_comparison, replace_commands, context_key):
        self.load_comparison = load_comparison
        self.replace_commands = replace_commands
        self.context_key = context_key
        self.initial_id = None
        self.enabled = True
        self.generating = False
        with ui.column().classes('w-full gap-2') as self.container:
            with ui.row().classes('w-full items-center gap-3 flex-nowrap overflow-x-auto'):
                with ui.column().classes('creator-parameter gap-1 rounded bg-slate-100 px-2 py-1'):
                    self.model = ui.select({}, label='Model form', on_change=self.refresh).props('use-input clearable').classes('min-w-64')
                with ui.column().classes('creator-parameter gap-1 rounded bg-slate-100 px-2 py-1 shrink-0'):
                    self.mode = ui.radio({'new': 'Create a new form', 'initial': 'Create from initial form'},
                                         value='initial', on_change=self.refresh).props('inline dense')
                with ui.row().classes('items-center gap-3 flex-nowrap shrink-0') as self.comparison_settings:
                    self.delete = ui.checkbox('Delete fields not in model form', value=True)
                    self.preserve = ui.checkbox('Preserve initial form field order', value=False)
                    self.overwrite = ui.checkbox('Overwrite field properties not in Model Form', value=True)
                    for control in (self.delete, self.preserve, self.overwrite):
                        control.classes('creator-parameter rounded bg-slate-100 px-2 py-1 whitespace-nowrap').props('dense')
            self.generate = ui.button('Generate Commands', on_click=self._generate)
            self.status = ui.label('').classes('text-sm text-red-700')
        self.refresh()

    def set_forms(self, options):
        options = dict(options)
        selected = self.model.value
        if selected is not None and selected not in options:
            options[selected] = self.model.options[selected]
        self.model.set_options(options)
        self.refresh()

    def set_initial(self, identity):
        self.initial_id = identity
        self.refresh()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.refresh()

    def refresh(self, _event=None):
        available = self.enabled and not self.generating
        for control in (self.model, self.mode, self.delete, self.preserve, self.overwrite):
            control.set_enabled(available)
        self.comparison_settings.set_visibility(self.mode.value == 'initial')
        self.generate.set_enabled(bool(available and self.model.value and not (
            self.mode.value == 'initial' and self.initial_id == self.model.value)))

    async def _map_fields(self, model, initial, related):
        choices, mapping = mapping_choices(model, initial, related)
        fields = comparison_fields(initial)
        controls = []
        with self.container, ui.dialog() as dialog, ui.card().classes('w-full max-w-4xl'):
            ui.label('Map model fields to initial form fields').classes('text-lg font-medium')
            with ui.column().classes('w-full max-h-96 overflow-y-auto gap-2'):
                with ui.row().classes('w-full flex-nowrap'):
                    ui.label('Model form field').classes('w-1/2 font-medium')
                    ui.label('Initial form field').classes('w-1/2 font-medium')
                def update_choices():
                    used = {control.value for control in controls if control.value != -1}
                    for index, control in enumerate(controls):
                        control.set_options({-1: 'Create New', **{
                            i: field_caption(fields[i]) for i in choices[index]
                            if i not in used or i == control.value}})
                for index, field in enumerate(comparison_fields(model)):
                    with ui.row().classes('w-full items-center flex-nowrap'):
                        ui.label(field_caption(field)).classes('w-1/2')
                        control = ui.select({-1: 'Create New', **{i: field_caption(fields[i]) for i in choices[index]}},
                                            value=mapping[index] if mapping[index] is not None else -1,
                                            on_change=lambda _: update_choices()).classes('w-1/2')
                        controls.append(control)
                update_choices()
            with ui.row().classes('w-full justify-end'):
                ui.button('Cancel', on_click=lambda: dialog.submit(None)).props('flat')
                ui.button('Generate Commands', on_click=lambda: dialog.submit([
                    None if control.value == -1 else control.value for control in controls]))
        result = await dialog
        dialog.delete()
        return result

    async def _generate(self):
        if not self.generate.enabled:
            return
        key = self.context_key()
        model_id = self.model.value
        options = ComparisonOptions(self.mode.value == 'new', self.delete.value,
                                    self.preserve.value, self.overwrite.value)
        self.generating = True
        self.status.set_text('Loading saved forms...')
        self.refresh()
        try:
            data = await self.load_comparison(model_id, options.create_new)
            if key != self.context_key():
                raise ValueError('Sources or commands changed. Generate commands again.')
            self.status.set_text('')
            mapping = None
            if data['initial'] is not None:
                mapping = await self._map_fields(data['model'], data['initial'], data['related'])
                if mapping is None:
                    return
            commands = generate_commands(**data, options=options, mapping=mapping)
            with self.container, ui.dialog() as dialog, ui.card():
                ui.label('Replace existing commands?').classes('text-lg font-medium')
                ui.label(f'This replaces the command list with {len(commands)} generated commands.')
                with ui.row().classes('w-full justify-end'):
                    ui.button('Cancel', on_click=lambda: dialog.submit(False)).props('flat')
                    ui.button('Replace commands', on_click=lambda: dialog.submit(True))
            confirmed = await dialog
            dialog.delete()
            if not confirmed:
                return
            if key != self.context_key() or not self.enabled:
                raise ValueError('Sources or commands changed. Generate commands again.')
            self.replace_commands(commands)
        except (ValueError, KeyError, TypeError) as exc:
            self.status.set_text(f'Could not generate commands: {exc}')
        finally:
            self.generating = False
            self.refresh()
