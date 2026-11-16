from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from nicegui import ui

from .combobox import FreeTextCombobox


ControlType = Literal['combobox', 'single_select']


@dataclass(frozen=True)
class ParameterDefinition:
    key: str
    label: str
    control: ControlType
    required: bool = False
    options: tuple[str, ...] = ()
    option_source: str | None = None
    visible_when: tuple[str, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class CommandCreatorDefinition:
    key: str
    label: str
    parameters: tuple[ParameterDefinition, ...]
    create_command: Callable[[Mapping[str, Any]], Any]


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


class CommandCreatorRenderer:
    """Render command parameter definitions using shared NiceGUI controls."""

    def __init__(
        self,
        definitions: tuple[CommandCreatorDefinition, ...],
        *,
        on_add: Callable[[Any], None] | None = None,
    ) -> None:
        self.definitions = {definition.key: definition for definition in definitions}
        self.on_add = on_add
        self.controls: dict[str, Any] = {}
        self.parameter_containers: dict[str, ui.element] = {}
        self.schema: dict[str, Any] | None = None

        first_key = next(iter(self.definitions), None)
        with ui.row().classes('w-full items-end gap-3 flex-wrap'):
            self.command_select = ui.select(
                {key: definition.label for key, definition in self.definitions.items()},
                value=first_key,
                label='Command',
                on_change=self._command_changed,
            ).classes('w-64')
            self.parameters = ui.row().classes('grow items-end gap-3 flex-wrap')
            self.add_button = ui.button(
                'Add command', icon='add', on_click=self._add_command,
            )
            self.add_button.disable()

        self._render_selected_command()

    def _command_changed(self, event) -> None:
        self._render_selected_command()

    def _render_selected_command(self) -> None:
        self.parameters.clear()
        self.controls.clear()
        self.parameter_containers.clear()
        definition = self.definitions.get(self.command_select.value)
        if not definition:
            return

        with self.parameters:
            for parameter in definition.parameters:
                with ui.column().classes('gap-1') as container:
                    if parameter.control == 'combobox':
                        options = (
                            field_codes_from_schema(self.schema)
                            if parameter.option_source == 'field_codes'
                            else parameter.options
                        )
                        control = FreeTextCombobox(
                            parameter.label,
                            options,
                            on_change=self._parameter_changed,
                        )
                    else:
                        ui.label(parameter.label).classes('text-sm text-slate-600')
                        control = ui.radio(
                            list(parameter.options),
                            on_change=self._parameter_changed,
                        ).props('inline')
                    control.props['data-parameter'] = parameter.key
                self.controls[parameter.key] = control
                self.parameter_containers[parameter.key] = container

        self._update_conditional_visibility()
        self._update_add_button()

    def _parameter_changed(self, event) -> None:
        self._update_conditional_visibility()
        self._update_add_button()

    def _update_conditional_visibility(self) -> None:
        definition = self.definitions.get(self.command_select.value)
        if not definition:
            return
        for parameter in definition.parameters:
            if not parameter.visible_when:
                continue
            dependency, accepted_values = parameter.visible_when
            visible = self.controls[dependency].value in accepted_values
            self.parameter_containers[parameter.key].set_visibility(visible)
            if not visible:
                self.controls[parameter.key].set_value(None)

    def _update_add_button(self) -> None:
        definition = self.definitions.get(self.command_select.value)
        if not definition:
            self.add_button.disable()
            return
        for parameter in definition.parameters:
            relevant = self.parameter_containers[parameter.key].visible
            value = self._control_value(self.controls[parameter.key])
            has_value = value.strip() != '' if isinstance(value, str) else value is not None
            if parameter.required and relevant and not has_value:
                self.add_button.disable()
                return
        self.add_button.enable()

    def _add_command(self) -> None:
        definition = self.definitions.get(self.command_select.value)
        if not definition or self.add_button.props.get('disable', False):
            return
        values = {
            key: self._control_value(control)
            for key, control in self.controls.items()
        }
        command = definition.create_command(values)
        if self.on_add:
            self.on_add(command)

    @staticmethod
    def _control_value(control):
        return control.entered_value if isinstance(control, FreeTextCombobox) else control.value

    def set_schema(self, schema: dict[str, Any] | None) -> None:
        self.schema = schema
        definition = self.definitions.get(self.command_select.value)
        if not definition:
            return
        codes = field_codes_from_schema(schema)
        for parameter in definition.parameters:
            if parameter.option_source == 'field_codes':
                control = self.controls.get(parameter.key)
                if isinstance(control, FreeTextCombobox):
                    control.set_suggestions(codes)

    def set_enabled(self, enabled: bool) -> None:
        method = 'enable' if enabled else 'disable'
        getattr(self.command_select, method)()
        for control in self.controls.values():
            getattr(control, method)()
        if enabled:
            self._update_add_button()
        else:
            self.add_button.disable()

