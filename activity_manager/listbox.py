from nicegui import ui
from nicegui.elements.mixins.text_element import TextElement


class DatabaseListbox(ui.element):
    """Native listbox: database IDs are values; names are displayed.

    A native select also supports multiple selection if needed in the future.
    """

    def __init__(self, label='Databases', placeholder='Select a database', on_change=None):
        super().__init__('select')
        self.placeholder = placeholder
        self.options: dict[str, str] = {}
        self.value: str | None = None
        self.props('size=10 disabled').classes('w-full border rounded p-2')
        self.props['aria-label'] = label

        async def change(event):
            self.value = event.args or None
            if on_change:
                await on_change()

        self.on('change', change,
                js_handler='(e) => emit(e.target.value)')

    def set_options(self, options: dict[str, str]) -> None:
        self.options = dict(options)
        self.value = None
        self.clear()
        self.props['disabled'] = not bool(options)
        with self:
            placeholder = TextElement(tag='option', text=self.placeholder if options else '')
            placeholder.props.update(value='', selected=True)
            for database_id, name in options.items():
                TextElement(tag='option', text=name).props['value'] = database_id

    def set_value(self, value: str | None) -> None:
        self.value = value if value in self.options else None
        for option in self.default_slot.children:
            if option.props.get('value') == (self.value or ''):
                option.props['selected'] = True
            else:
                option.props.pop('selected', None)
            option.update()
        self.update()

    def disable(self) -> None:
        self.props['disabled'] = True
        self.update()

    def enable(self) -> None:
        self.props['disabled'] = False
        self.update()

    @property
    def selected_label(self) -> str | None:
        return self.options.get(self.value) if self.value else None
