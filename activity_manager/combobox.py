from collections.abc import Iterable

from nicegui import events, ui


_KEEP_VALUE = object()


class FreeTextCombobox(ui.input):
    """A text input with a separate dropdown of suggested values."""

    def __init__(
        self,
        label: str,
        options: Iterable[str] = (),
        *,
        value: str | None = None,
        on_change=None,
    ) -> None:
        self.options = list(dict.fromkeys(options))
        self._external_change_handler = on_change
        super().__init__(
            label=label,
            value=value or '',
            on_change=on_change,
            autocomplete=self.options,
        )
        self.props('clearable').classes('w-56')
        with self.add_slot('append'):
            with ui.button(icon='arrow_drop_down').props('flat dense round') as button:
                self.dropdown_button = button
                self.dropdown = ui.menu()
        self._render_options()

    @property
    def entered_value(self) -> str | None:
        return self.value

    def set_options(self, options: Iterable[str], *, value=_KEEP_VALUE):
        """Update suggestions, optionally replacing the current value."""
        self.options = list(dict.fromkeys(options))
        self.set_autocomplete(self.options)
        self._render_options()
        if value is not _KEEP_VALUE:
            self.set_value(value)
        return self

    def set_suggestions(self, options: Iterable[str]) -> None:
        """Replace dropdown suggestions without discarding entered text."""
        self.set_options(options)

    def _render_options(self) -> None:
        self.dropdown.clear()
        with self.dropdown:
            if not self.options:
                ui.item('No suggestions').props('disable')
                return
            for option in self.options:
                ui.item(
                    option,
                    on_click=lambda option=option: self._select_option(option),
                )

    def _select_option(self, option: str) -> None:
        self.set_value(option)
        self.dropdown.close()
        events.handle_event(self._external_change_handler, None)

    def disable(self):
        super().disable()
        self.dropdown_button.disable()
        return self

    def enable(self):
        super().enable()
        self.dropdown_button.enable()
        return self

