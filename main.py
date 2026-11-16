from nicegui import ui

from activity_manager.page import connection_page

ui.page('/')(connection_page)

if __name__ == '__main__':
    ui.run(title='ActivityInfo Manager', host='127.0.0.1', reload=False, show=False)
