from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any, Literal

from nicegui import events, ui

from .combobox import FreeTextCombobox


Condition = tuple[str, tuple[Any, ...]] | Callable[[Mapping[str, Any]], bool]
ControlType = Literal['text', 'select', 'combobox', 'single_select', 'multi_select', 'boolean', 'toggle', 'group', 'repeat']


@dataclass(frozen=True)
class ParameterDefinition:
    key: str
    label: str
    control: ControlType
    required: bool = False
    options: tuple[str, ...] = ()
    option_source: str | None = None
    options_when: Callable[[Mapping[str, Any]], tuple[str, ...]] | None = None
    visible_when: Condition | None = None
    required_when: Condition | None = None
    path: str | None = None
    value_type: Literal['string', 'integer', 'number', 'boolean'] = 'string'
    multiline: bool = False
    blank_action: Literal['empty', 'unset'] = 'empty'
    children: tuple['ParameterDefinition', ...] = ()
    default: Any = None
    initially_active: bool = False
    validate: Callable[[Any], str | None] | None = None
    enabled: bool = True
    help: str | None = None
    option_labels: Mapping[str, str] | None = None
    inline_rows: bool = False
    fixed_when: Callable[[Mapping[str, Any]], Any] | None = None
    parse_text: Callable[[str], Any] | None = None


@dataclass(frozen=True)
class CommandCreatorDefinition:
    key: str
    label: str
    parameters: tuple[ParameterDefinition, ...]
    create_command: Callable[[Mapping[str, Any]], Any]
    target_defaults: Callable[[dict[str, Any] | None, str], Mapping[str, Any]] | None = None


def field_codes_from_schema(schema: dict[str, Any] | None) -> list[str]:
    """Return unique field codes from a form schema's elements."""
    codes: list[str] = []
    elements = schema.get('elements', []) if schema else []
    if not isinstance(elements, list):
        return codes
    for element in elements:
        if not isinstance(element, dict):
            continue
        code = element.get('code')
        if isinstance(code, str) and code and code not in codes:
            codes.append(code)
    return codes


class _ParameterEditor:
    """One recursive parameter node; activation is independent of its value."""

    def __init__(self, owner, definition, *, state=None):
        self.owner = owner
        self.definition = definition
        self.children: list[_ParameterEditor] = []
        self.rows: list[list[_ParameterEditor]] = []
        self.buttons = []
        self.control = None
        self.current_options = definition.options
        self.relevant = True
        self.required = definition.required
        self._fixed_value = None
        state = state or {}
        self.add_row_button = None
        with ui.column().classes('creator-parameter gap-1 rounded bg-slate-100 px-2 py-1') as self.container:
            if definition.control in ('group', 'repeat'):
                self.container.classes('creator-expanding-parameter')
            if definition.inline_rows:
                self.container.classes('creator-inline-rows')
            with ui.row().classes('items-center gap-2 flex-nowrap'):
                self.activation = ui.checkbox(
                    value=state.get('active', definition.required or definition.initially_active),
                    on_change=owner._parameter_changed,
                ).props('dense size=sm').classes('shrink-0')
                self.activation.tooltip('Include this parameter')
                self.activation.props['aria-label'] = f'Include {definition.label}'
                self.activation.props['data-activation'] = definition.key
                if definition.help:
                    self.container.tooltip(definition.help)
                with ui.column().classes('gap-1 min-w-0'):
                    self._render_control(state.get('value', definition.default))
            if definition.control in ('group', 'repeat'):
                self.body = ui.column().classes('w-full pl-4 gap-2')
                if definition.control == 'group':
                    with self.body:
                        self.children = self._make_children(state.get('children', []))
                else:
                    self._render_rows(state.get('rows', []))
            if self.control is not None:
                self.control.props['data-parameter'] = definition.key

    def _render_control(self, value):
        definition = self.definition
        on_change = self.owner._parameter_changed
        if definition.control in ('group', 'repeat'):
            with ui.row().classes('items-center gap-2 flex-nowrap'):
                ui.label(definition.label).classes('font-medium whitespace-nowrap')
                if definition.inline_rows:
                    self.add_row_button = ui.button(
                        icon='add', on_click=lambda: self._change_rows(None, 0),
                    ).props('flat dense round')
                    self.add_row_button.props['aria-label'] = f'Add {definition.label} row'
        elif definition.control == 'combobox':
            self.control = FreeTextCombobox(
                definition.label, self._options(), value=value, on_change=on_change,
            )
            if definition.multiline:
                self.control.props('type=textarea autogrow')
        elif definition.control == 'select':
            self.control = ui.select(
                list(definition.options), label=definition.label, value=value, on_change=on_change,
            ).classes('min-w-28')
        elif definition.control == 'single_select':
            ui.label(definition.label).classes('text-sm text-slate-600')
            self.control = ui.radio(
                definition.option_labels or list(definition.options), value=value, on_change=on_change,
            ).props('inline dense size=sm')
        elif definition.control == 'multi_select':
            self.control = ui.select(
                list(definition.options), label=definition.label, multiple=True,
                value=list(value or []), on_change=on_change,
            ).props('dense use-chips').classes('min-w-56')
        elif definition.control == 'toggle':
            self.control = ui.switch(definition.label, value=value == definition.options[1], on_change=on_change)
        elif definition.control == 'boolean':
            with ui.row().classes('items-center gap-2 flex-nowrap'):
                ui.label(f'{definition.label}:').classes('text-sm text-slate-700 whitespace-nowrap')
                self.control = ui.checkbox(value=bool(value), on_change=on_change).props('dense size=md')
                self.control.props['aria-label'] = definition.label
        else:
            factory = ui.textarea if definition.multiline else ui.input
            self.control = factory(
                definition.label, value='' if value is None else str(value), on_change=on_change,
            )

    def _options(self):
        if self.definition.option_source == 'field_codes':
            return field_codes_from_schema(self.owner.schema)
        if self.definition.option_source in self.owner.option_sources:
            return self.owner.option_sources[self.definition.option_source]
        return self.definition.options

    def _make_children(self, states):
        return [
            _ParameterEditor(self.owner, child, state=states[index] if index < len(states) else None)
            for index, child in enumerate(self.definition.children)
        ]

    def snapshot(self):
        return {
            'active': self.activation.value,
            'value': self.control.value if self.control is not None else None,
            'children': [child.snapshot() for child in self.children],
            'rows': [[child.snapshot() for child in row] for row in self.rows],
        }

    def _render_rows(self, states):
        self.body.clear()
        self.rows = []
        self.buttons = [self.add_row_button] if self.add_row_button is not None else []
        with self.body:
            for index, state in enumerate(states):
                row_factory = ui.row if self.definition.inline_rows else ui.column
                with row_factory().classes(
                    'items-center flex-nowrap gap-2' if self.definition.inline_rows
                    else 'border rounded p-2 gap-2'
                ):
                    self.rows.append(self._make_children(state))
                    with ui.row().classes('flex-nowrap gap-0'):
                        for label, icon, action in [('Up', 'keyboard_arrow_up', -1),
                                                    ('Down', 'keyboard_arrow_down', 1),
                                                    ('Remove', 'delete_outline', 0)]:
                            button = ui.button(
                                None if self.definition.inline_rows else label,
                                icon=icon if self.definition.inline_rows else None,
                                on_click=lambda i=index, a=action: self._change_rows(i, a),
                            ).props('flat dense')
                            button.props['aria-label'] = f'{label} row'
                            if (action == -1 and index == 0) or (action == 1 and index == len(states) - 1):
                                button.props['data-boundary'] = True
                            self.buttons.append(button)
            if not self.definition.inline_rows:
                self.buttons.append(ui.button('Add row', on_click=lambda: self._change_rows(None, 0)))

    def _change_rows(self, index, offset):
        states = self.snapshot()['rows']
        if index is None:
            states.append([])
        elif offset:
            other = index + offset
            if not 0 <= other < len(states):
                return
            states[index], states[other] = states[other], states[index]
        else:
            states.pop(index)
        self.owner._updating = True
        try:
            self._render_rows(states)
        finally:
            self.owner._updating = False
        self.owner._parameter_changed(None)

    def refresh(self, siblings, enabled):
        definition = self.definition
        enabled = enabled and definition.enabled
        was_relevant = self.relevant
        self.relevant = self._matches(definition.visible_when, siblings)
        self.required = definition.required or (
            definition.required_when is not None and self._matches(definition.required_when, siblings)
        )
        if definition.options_when:
            options = definition.options_when(siblings)
            if options != self.current_options:
                self.current_options = options
                self.control.set_options(list(options), value=self.control.value if self.control.value in options else None)
        self.container.set_visibility(self.relevant)
        fixed = definition.fixed_when(siblings) if definition.fixed_when else None
        if self._fixed_value is not None and fixed is None:
            self.control.set_value(definition.default if definition.default is not None else False)
            self.activation.set_value(definition.required or definition.initially_active)
        self._fixed_value = fixed
        if fixed is not None and self.relevant:
            self.control.set_value(fixed)
        if self.required or fixed is not None:
            self.activation.set_value(True)
        self.activation.set_visibility(not self.required and fixed is None)
        self.activation.set_enabled(enabled and self.relevant and not self.required and fixed is None)
        active = enabled and self.relevant and self.activation.value
        self.container.classes(
            add='bg-blue-100' if self.activation.value else 'bg-slate-100',
            remove='bg-slate-100' if self.activation.value else 'bg-blue-100',
        )
        if self.control is not None:
            if was_relevant and not self.relevant:
                self.control.set_value(
                    [] if definition.control == 'multi_select'
                    else False if definition.control in ('boolean', 'toggle') else None
                )
            getattr(self.control, 'enable' if active and fixed is None else 'disable')()
            if isinstance(self.control, FreeTextCombobox):
                options = list(self._options())
                if self.control.options != options:
                    self.control.set_suggestions(options)
        for button in self.buttons:
            button.set_enabled(active and not button.props.get('data-boundary', False))
        for children in [self.children, *self.rows]:
            self.owner._refresh_editors(children, active, siblings)

    @staticmethod
    def _matches(condition, values):
        if callable(condition):
            return condition(values)
        return condition is None or values.get(condition[0]) in condition[1]

    def raw_value(self):
        if self.definition.control == 'toggle' and self.activation.value and self.relevant:
            return self.definition.options[bool(self.control.value)]
        if self.definition.control == 'group' and self.activation.value and self.relevant:
            return {child.definition.key: child.raw_value() for child in self.children}
        return (
            self.control.value
            if self.control is not None and self.activation.value and self.relevant
            and self.definition.enabled else None
        )

    def value(self):
        definition = self.definition
        if definition.control == 'group':
            value = self.owner._collect(self.children)
        elif definition.control == 'repeat':
            value = [self.owner._collect(row) for row in self.rows]
        else:
            value = self.raw_value()
            blank = value is None or isinstance(value, str) and not value.strip()
            if definition.control == 'multi_select':
                if not isinstance(value, list) or any(item not in definition.options for item in value):
                    raise ValueError(f'{definition.label} must contain only listed choices.')
                if self.required and not value:
                    raise ValueError(f'{definition.label} requires at least one choice.')
            if self.required and blank:
                raise ValueError(f'{definition.label} is required.')
            if definition.control in ('single_select', 'select') and blank and definition.blank_action != 'unset':
                raise ValueError(f'{definition.label} requires a choice.')
            if blank:
                value = None if definition.blank_action == 'unset' else ''
            elif definition.control == 'boolean' or definition.value_type == 'boolean':
                if not isinstance(value, bool):
                    raise ValueError(f'{definition.label} must be a boolean.')
            elif definition.value_type in ('integer', 'number'):
                try:
                    value = int(value) if definition.value_type == 'integer' else float(value)
                    if not isfinite(value):
                        raise ValueError
                except (ValueError, TypeError, OverflowError):
                    raise ValueError(f'{definition.label} must be a finite {definition.value_type}.') from None
            if definition.control in ('single_select', 'select') and value not in self.current_options and not blank:
                raise ValueError(f'{definition.label} must be one of the listed choices.')
        if self.required and definition.control == 'repeat' and not value:
            raise ValueError(f'{definition.label} requires at least one row.')
        if definition.blank_action == 'unset' and not value and definition.control in ('repeat', 'group', 'multi_select'):
            value = None
        if definition.parse_text and isinstance(value, str):
            value = definition.parse_text(value)
        if definition.validate and value is not None:
            error = definition.validate(value)
            if error:
                raise ValueError(error)
        return value


class CommandCreatorRenderer:
    """Render declarative, typed parameters, including nested and repeated groups.

    Inactive parameters are omitted. Active blank values are '' or None according
    to blank_action, and an active unchecked boolean is explicitly False.
    Conditions refer to sibling keys; dotted paths control the output mapping.
    """

    def __init__(self, definitions: tuple[CommandCreatorDefinition, ...], *, on_add=None, on_parameter_change=None):
        self.definitions = {definition.key: definition for definition in definitions}
        self.on_add = on_add
        self.on_parameter_change = on_parameter_change
        self.controls: dict[str, Any] = {}
        self.activations: dict[str, Any] = {}
        self.parameter_containers: dict[str, ui.element] = {}
        self.editors: list[_ParameterEditor] = []
        self.schema: dict[str, Any] | None = None
        self.option_sources: dict[str, list[str]] = {}
        self.enabled = True
        self._updating = False
        ui.add_css('''
            .creator-parameter {
                height: 64px;
                flex-shrink: 0;
                flex-wrap: nowrap;
                justify-content: center;
                overflow: auto;
            }
            .creator-parameter > .nicegui-row { flex-shrink: 0; }
            .creator-parameter textarea.q-field__native {
                min-height: 0;
                height: 40px;
                overflow-y: auto;
            }
            .creator-parameter .q-radio { min-height: 28px; }
            .creator-expanding-parameter { height: auto; min-height: 64px; }
            .creator-inline-rows .creator-parameter {
                background: transparent;
                padding: 0;
            }
        ''')
        with ui.row().classes('w-full items-start gap-3 flex-nowrap'):
            with ui.column().classes('w-48 shrink-0 gap-2'):
                self.command_select = ui.select(
                    {key: definition.label for key, definition in self.definitions.items()},
                    value=next(iter(self.definitions), None), label='Command',
                    on_change=self._command_changed,
                ).classes('w-full bg-white')
                self.target_mode = ui.select(
                    ['Single field', 'Multiple fields'], value='Single field', label='Target',
                    on_change=self._target_mode_changed,
                ).classes('w-full bg-white')
                self.target_help = ui.label(
                    'Whole-code regex; captures: (?P<year>...). '
                    'Values: ${field.year}. Literal escape: $${'
                ).classes('text-xs text-slate-500')
            with ui.column().classes('min-w-0 flex-1 gap-3'):
                self.parameters = ui.row().classes('w-full items-start gap-3 flex-wrap')
                self.add_button = ui.button('Add command', icon='add', on_click=self._add_command)
        self.error = ui.label('').classes('text-sm text-red-700')
        self._render_selected_command()

    def _command_changed(self, event):
        self._render_selected_command()

    def _target_mode_changed(self, event):
        self._last_target = None
        self._parameter_changed(None)

    def _multiple_fields(self):
        from .command_iteration import ITERABLE_CREATORS
        return self.command_select.value in ITERABLE_CREATORS and self.target_mode.value == 'Multiple fields'

    def _render_selected_command(self):
        self._updating = True
        try:
            self._last_target = None
            self.parameters.clear()
            self.controls.clear()
            self.activations.clear()
            self.parameter_containers.clear()
            definition = self.definitions.get(self.command_select.value)
            from .command_iteration import ITERABLE_CREATORS
            self.target_mode.set_visibility(self.command_select.value in ITERABLE_CREATORS)
            with self.parameters:
                self.editors = [
                    _ParameterEditor(self, parameter) for parameter in definition.parameters
                ] if definition else []
            for editor in self.editors:
                key = editor.definition.key
                self.controls[key] = editor.control
                self.activations[key] = editor.activation
                self.parameter_containers[key] = editor.container
        finally:
            self._updating = False
        self._parameter_changed(None)

    def _parameter_changed(self, event):
        if self._updating:
            return
        self._populate_target_defaults()
        self._update_conditional_visibility()
        self.target_help.set_visibility(self._multiple_fields())
        target = self.controls.get('field_code')
        if target is not None:
            label = next(editor.definition.label for editor in self.editors if editor.definition.key == 'field_code')
            target.set_label('Field code pattern' if self._multiple_fields() else label)
            if self._multiple_fields():
                target.set_options([])
        self._update_add_button()
        events.handle_event(self.on_parameter_change, self)

    def _populate_target_defaults(self):
        definition = self.definitions.get(self.command_select.value)
        if definition is None or definition.target_defaults is None or self._multiple_fields():
            return
        target = self.controls['field_code'].value
        if target == self._last_target:
            return
        self._last_target = target
        defaults = definition.target_defaults(self.schema, target)
        self._updating = True
        try:
            if 'field_type' in defaults:
                self.controls['field_type'].set_value(defaults['field_type'])
                # Refresh variant choices before assigning the detected variant.
                self._refresh_editors(self.editors, self.enabled)
                self.controls['field_variant'].set_value(defaults.get('field_variant'))
        finally:
            self._updating = False

    def _update_conditional_visibility(self):
        self._updating = True
        try:
            self._refresh_editors(self.editors, self.enabled)
        finally:
            self._updating = False

    @staticmethod
    def _refresh_editors(editors, enabled, context=None):
        # Settle chained sibling conditions in the same event, including when
        # a hidden dependency has just had its value cleared.
        for _ in range(len(editors) + 1):
            values = {editor.definition.key: editor.raw_value() for editor in editors}
            for editor in editors:
                editor.refresh({**(context or {}), **values}, enabled)
            if values == {editor.definition.key: editor.raw_value() for editor in editors}:
                break

    @staticmethod
    def _collect(editors):
        values = {}
        for editor in editors:
            if not editor.relevant or not editor.activation.value or not editor.definition.enabled:
                continue
            target = values
            parts = (editor.definition.path or editor.definition.key).split('.')
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = editor.value()
        return values

    def values(self):
        values = self._collect(self.editors)
        if self._multiple_fields():
            from .command_iteration import validate_pattern
            validate_pattern(values.get('field_code'))
        from .command_templates import substitute_selector, substitute_value
        if isinstance(values.get('field_code'), str):
            substitute_selector(values['field_code'])
        substitute_value({key: value for key, value in values.items() if key != 'field_code'})
        return values

    def _update_add_button(self):
        try:
            self.values()
        except ValueError as exc:
            self.error.set_text(str(exc))
            self.add_button.disable()
            return
        self.error.set_text('')
        self.add_button.set_enabled(self.enabled and self.command_select.value in self.definitions)

    def _add_command(self):
        if self.add_button.props.get('disable', False):
            return
        definition = self.definitions.get(self.command_select.value)
        if definition:
            try:
                command = definition.create_command(self.values())
                if self._multiple_fields():
                    from dataclasses import replace
                    from .command_iteration import IterativeCommand
                    command = IterativeCommand(replace(command, field_code='__iteration_target__'),
                                               self.values()['field_code'])
            except ValueError as exc:
                self.error.set_text(str(exc))
                return
            if self.on_add:
                self.on_add(command)

    def set_schema(self, schema):
        self.schema = schema
        self._parameter_changed(None)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.command_select.set_enabled(enabled)
        self.target_mode.set_enabled(enabled)
        self._parameter_changed(None)

    def set_suggestions(self, source: str, values):
        self.option_sources[source] = list(values)
        self._update_conditional_visibility()
        self._update_add_button()
