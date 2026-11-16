from nicegui import ui
from nicegui.elements.mixins.text_element import TextElement


class DatabaseListbox(ui.element):
    """Native listbox: database IDs are values; names are displayed.

    A native select also supports multiple selection if needed in the future.
    """

    def __init__(self, label='Databases', placeholder='Select a database', on_change=None):
        super().__init__('select')
        self.placeholder = placeholder
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
        self.value = None
        self.clear()
        self.props['disabled'] = not bool(options)
        with self:
            placeholder = TextElement(tag='option', text=self.placeholder if options else '')
            placeholder.props.update(value='', disabled=True, selected=True)
            for database_id, name in options.items():
                TextElement(tag='option', text=name).props['value'] = database_id
