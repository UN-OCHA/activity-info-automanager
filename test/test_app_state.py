import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from nicegui import ui
from nicegui.client import Client
from activity_manager.app_state import AppStateStore
from activity_manager.command_list import CommandListManager
from activity_manager.listbox import DatabaseListbox
from activity_manager.page import connection_page


class StoreTests(unittest.TestCase):
    def test_dpapi_round_trip_and_failed_decryption_preserves_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AppStateStore(Path(directory) / 'state.json')
            store.save(api_token='secret-test-token', dbid='db', formid='form', commands='text')
            self.assertNotIn('secret-test-token', store.path.read_text())
            saved, warning = store.load()
            self.assertEqual(saved['api_token'], 'secret-test-token')
            self.assertEqual(warning, '')
            data = json.loads(store.path.read_text())
            data['api_token_dpapi'] = 'invalid'
            store.path.write_text(json.dumps(data))
            saved, warning = store.load()
            self.assertEqual(saved['commands'], 'text')
            self.assertEqual(saved['api_token'], '')
            self.assertTrue(warning)


class RestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_order_missing_selections_and_connection_failure(self):
        for databases, forms, failure in [({'db': 'DB'}, {'form': 'Form'}, False),
                                           ({'other': 'Other'}, {}, False),
                                           ({'db': 'DB'}, {'other': 'Other'}, False),
                                           ({}, {}, True)]:
            with self.subTest(databases=databases, forms=forms, failure=failure):
                store = AsyncMock()
                store.load = unittest.mock.Mock(return_value=(dict(api_token='token', dbid='db', formid='form', commands='DeleteField(fieldCode="A")'), ''))
                store.save = unittest.mock.Mock()
                service = AsyncMock()
                service.get_databases.return_value = databases
                if failure:
                    service.get_databases.side_effect = ValueError('offline')
                service.get_forms.return_value = forms
                service.get_form_tree.return_value = {'form': {'id': 'form', 'elements': []}}
                service.get_database_names.return_value = {'db': 'DB'}
                managers = []
                class Manager(CommandListManager):
                    def __init__(self, **kwargs):
                        super().__init__(**kwargs)
                        managers.append(self)
                with patch('activity_manager.page.AppStateStore', return_value=store), patch('activity_manager.page.ActivityInfoService', return_value=service), patch('activity_manager.page.CommandListManager', Manager), patch('activity_manager.page.ui.timer') as timer:
                    with Client(ui.page('/')), ui.column() as page:
                        connection_page()
                        restore = next(call.args[1] for call in timer.call_args_list if getattr(call.args[1], '__name__', '') == 'restore_state')
                        with patch.object(managers[0], '_apply_text', wraps=managers[0]._apply_text) as apply:
                            await restore()
                            apply.assert_called_once()
                    selections = [e for e in page.descendants() if isinstance(e, DatabaseListbox)]
                    self.assertEqual(selections[0].value, 'db' if 'db' in databases else None)
                    self.assertEqual(selections[1].value, 'form' if 'db' in databases and 'form' in forms else None)
                    if 'db' not in databases:
                        service.get_forms.assert_not_awaited()
                    self.assertEqual(len(managers[0].commands), 1)
                    store.save.assert_not_called()
                    managers[0].delete_command(0)
                    store.save.assert_called_once_with(api_token='token',
                                                       dbid=selections[0].value,
                                                       formid=selections[1].value, commands='', execute_step_by_step=False)
